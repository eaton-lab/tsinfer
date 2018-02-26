import numpy as np
import random
import os
import h5py
import zarr
import sys
import pandas as pd
import daiquiri
#import bsddb3
import time
import scipy
import pickle
import collections
import itertools
import tqdm
import shutil

import matplotlib as mp
# Force matplotlib to not use any Xwindows backend.
mp.use('Agg')
import matplotlib.pyplot as plt
import seaborn as sns

import tsinfer
import msprime



def plot_breakpoints(ts, map_file, output_file):
    # Read in the recombination map using the read_hapmap method,
    recomb_map = msprime.RecombinationMap.read_hapmap(map_file)

    # Now we get the positions and rates from the recombination
    # map and plot these using 500 bins.
    positions = np.array(recomb_map.get_positions()[1:])
    rates = np.array(recomb_map.get_rates()[1:])
    num_bins = 500
    v, bin_edges, _ = scipy.stats.binned_statistic(
        positions, rates, bins=num_bins)
    x = bin_edges[:-1][np.logical_not(np.isnan(v))]
    y = v[np.logical_not(np.isnan(v))]
    fig, ax1 = plt.subplots(figsize=(16, 6))
    ax1.plot(x, y, color="blue", label="Recombination rate")
    ax1.set_ylabel("Recombination rate")
    ax1.set_xlabel("Chromosome position")

    # Now plot the density of breakpoints along the chromosome
    breakpoints = np.array(list(ts.breakpoints()))
    ax2 = ax1.twinx()
    v, bin_edges = np.histogram(breakpoints, num_bins, density=True)
    ax2.plot(bin_edges[:-1], v, color="green", label="Breakpoint density")
    ax2.set_ylabel("Breakpoint density")
    ax2.set_xlim(1.5e7, 5.3e7)
    plt.legend()
    fig.savefig(output_file)


def make_errors(v, p):
    """
    For each sample an error occurs with probability p. Errors are generated by
    sampling values from the stationary distribution, that is, if we have an
    allele frequency of f, a 1 is emitted with probability f and a
    0 with probability 1 - f. Thus, there is a possibility that an 'error'
    will in fact result in the same value.
    """
    w = np.copy(v)
    if p > 0:
        m = v.shape[0]
        frequency = np.sum(v) / m
        # Randomly choose samples with probability p
        samples = np.where(np.random.random(m) < p)[0]
        # Generate observations from the stationary distribution.
        errors = (np.random.random(samples.shape[0]) < frequency).astype(int)
        w[samples] = errors
    return w


def generate_samples(ts, error_p):
    """
    Returns samples with a bits flipped with a specified probability.

    Rejects any variants that result in a fixed column.
    """
    S = np.zeros((ts.sample_size, ts.num_mutations), dtype=np.int8)
    for variant in ts.variants():
        done = False
        # Reject any columns that have no 1s or no zeros
        while not done:
            S[:, variant.index] = make_errors(variant.genotypes, error_p)
            s = np.sum(S[:, variant.index])
            done = 0 < s < ts.sample_size
    return S.T


def check_infer(
        n, L, seed, num_threads=1, recombination_rate=1e-8,
        genotype_quality=0, method="C", log_level="WARNING",
        debug=True, progress=False):

    np.random.seed(seed)
    random.seed(seed)
    L_megabases = int(L * 10**6)

    daiquiri.setup(level=log_level)
    ts = msprime.simulate(
            n, Ne=10**4, length=L_megabases,
            recombination_rate=1e-8, mutation_rate=1e-8,
            random_seed=seed)
    if debug:
        print("num_sites = ", ts.num_sites)
    assert ts.num_sites > 0
    positions = np.array([site.position for site in ts.sites()])
    V = ts.genotype_matrix()
    # print(V)
    G = generate_samples(ts, genotype_quality)
    # print(S.T)
    # print(np.where(S.T != V))
    recombination_rate = np.zeros_like(positions) + recombination_rate

    inferred_ts = tsinfer.infer(
        G, positions, ts.sequence_length, recombination_rate,
        sample_error=genotype_quality, method="method", num_threads=num_threads,
        progress=progress)

    assert np.array_equal(G, inferred_ts.genotype_matrix())


def generate_ancestors(ts):
    A = np.zeros((ts.num_nodes, ts.num_sites), dtype=np.uint8) - 1

    for t in ts.trees():
        for site in t.sites():
            for u in t.nodes():
                A[u, site.index] = 0
            for mutation in site.mutations:
                # Every node underneath this node will have the value set
                # at this site.
                for u in t.nodes(mutation.node):
                    A[u, site.index] = 1
    # This is all nodes, but we only want the non samples. We also reverse
    # the order to make it forwards time.
    A = A[ts.num_samples:]
    return A[::-1]

def debug_real_ancestor_injection(n_samples):
    method = "C"
    path_compression = True
    rng1 = random.Random(1234)
    print("trees","sites","edges:", "tsinfer", "known_anc_orig",
        "known_anc_jerome", "known_anc_yan", sep="\t")
    for i in range(100):
        ts, full_inferred_ts, orig_anc_ts, jk_anc_ts, hyw_anc_ts = \
            single_real_ancestor_injection(method, path_compression, rng1.randint(1, 2**31))

        print(ts.num_trees, ts.num_sites, ts.num_edges, sep="\t", end="\t")
        inf_edges = np.array([[x.num_edges, x.num_trees] \
            for x in (full_inferred_ts, orig_anc_ts, jk_anc_ts, hyw_anc_ts)], dtype=np.int)
        print("\t".join(["{} {}/{}".format(x[1],x[0], ts.num_edges) + \
            ("*" if x[0]==min(inf_edges[:,0]) and np.sum(inf_edges[:,0]==min(inf_edges[:,0]))==1 else " ")\
            for x in inf_edges]))


def single_real_ancestor_injection(method, path_compression, simplify=False, **kwargs):
    """
    if no mutation rate specified, put one mutation per branch, apart from tips
    """
    # daiquiri.setup(level="DEBUG")
    ts = msprime.simulate(**kwargs)
    if 'mutation_rate' not in kwargs:
        ts = insert_perfect_mutations(ts)

    ts = strip_singletons(ts)

    positions = np.array([v.position for v in ts.variants()])
    G = ts.genotype_matrix()
    recombination_rate = np.zeros_like(positions) + 1

    input_root = zarr.group()
    tsinfer.InputFile.build(
        input_root, genotypes=G,
        position=positions,
        recombination_rate=recombination_rate, sequence_length=1,
        compress=False)

    ancestors_root = zarr.group()
    tsinfer.build_ancestors(
        input_root, ancestors_root, method=method, chunk_size=16, compress=False)
    ancestors_ts = tsinfer.match_ancestors(
        input_root, ancestors_root, method=method, path_compression=path_compression)
    full_inferred_ts = tsinfer.match_samples(
        input_root, ancestors_ts, method=method, path_compression=path_compression,
        simplify=simplify)


    ancestors_root = zarr.group()
    tsinfer.build_simulated_ancestors(input_root, ancestors_root, ts,
        guess_unknown=False)
    ancestors_ts = tsinfer.match_ancestors(
        input_root, ancestors_root, method=method, path_compression=path_compression)
    orig_anc_ts = tsinfer.match_samples(
        input_root, ancestors_ts, method=method, path_compression=path_compression,
        simplify=simplify)

    ancestors_root = zarr.group()
    tsinfer.build_simulated_ancestors(input_root, ancestors_root, ts,
        guess_unknown=None)
    ancestors_ts = tsinfer.match_ancestors(
        input_root, ancestors_root, method=method, path_compression=path_compression)
    jk_anc_ts = tsinfer.match_samples(
        input_root, ancestors_ts, method=method, path_compression=path_compression,
        simplify=simplify)

    ancestors_root = zarr.group()
    tsinfer.build_simulated_ancestors(input_root, ancestors_root, ts,
        guess_unknown=True)
    ancestors_ts = tsinfer.match_ancestors(
        input_root, ancestors_root, method=method, path_compression=path_compression)
    hyw_anc_ts = tsinfer.match_samples(
        input_root, ancestors_ts, method=method, path_compression=path_compression,
        simplify=simplify)

    return ts, full_inferred_ts, orig_anc_ts, jk_anc_ts, hyw_anc_ts

def debug_pathological():

    # daiquiri.setup(level="DEBUG")
    method = "C"
    path_compression = False
    # ts = msprime.load("pathological-small.source.hdf5")
    recomb_map = msprime.RecombinationMap.uniform_map(
            length=100, rate=0.01, num_loci=100)
    ts = msprime.simulate(6, recombination_map=recomb_map, random_seed=4)
    # ts = msprime.simulate(5, recombination_rate=1.2, random_seed=9)
    # ts = msprime.simulate(4, recombination_rate=1.5, random_seed=3)
    print(ts.num_trees)

    ts = tsinfer.insert_perfect_mutations(ts)
    print(ts.sequence_length)

    print("INPUT")
    for t in ts.trees():
        sites = [s.id for s in t.sites()]
        print(t.interval)
        print(t.draw(format="unicode"))
        # t.draw("tree_{}.svg".format(t.index))
        print("=" * 10)

    print("num_sites = ", ts.num_sites)
    print("num_edges = ", ts.num_edges)
    print("num_trees = ", ts.num_trees)
    # for t in ts.trees():
    #     print([site.index for site in t.sites()])
    #     print(t.draw(format="unicode"))

    # ts = msprime.load("pathological-small.hdf5")
    # print("inferred num_edges = ", ts.num_edges)
    # print("inferred num_trees = ", ts.num_trees)

    positions = np.array([site.position for site in ts.sites()])
    G = ts.genotype_matrix()
    recombination_rate = np.zeros_like(positions) + 1

    input_root = zarr.group()
    tsinfer.InputFile.build(
        input_root, genotypes=G,
        position=positions,
        recombination_rate=recombination_rate, sequence_length=ts.sequence_length,
        compress=False)
    ancestors_root = zarr.group()
    #tsinfer.build_ancestors(
    #    input_root, ancestors_root, method=method, chunk_size=16, compress=False)
    tsinfer.build_simulated_ancestors(input_root, ancestors_root, ts, guess_unknown=True)

    A = ancestors_root["ancestors/haplotypes"][:]

    print("ANCESTORS")
    # print(A.astype(np.int8))
    for j, a in enumerate(A):
        s = "".join(str(x) if x < 255 else "*" for x in a)
        print(j, "\t", s)
    print("samples")
    for j, s in enumerate(ts.haplotypes()):
        print(A.shape[0] + j, "\t", s)
    # A[A == 255] = 0
    # ancestors_root["ancestors/haplotypes"][:] = A
    # ancestors_root["ancestors/start"][:] = 0
    # ancestors_root["ancestors/end"][:] = ts.num_sites

    print("========")
    print("INFERRED ANCESTRAL PATHS")

    ancestors_ts = tsinfer.match_ancestors(
        input_root, ancestors_root, method=method, path_compression=path_compression)
        # output_path="tmp__NOBACKUP__/bad_tb.tsancts", output_interval=0.1)
        # output_path=None, traceback_file_pattern="tmp__NOBACKUP__/traceback_{}.pkl")
    assert ancestors_ts.sequence_length == ts.num_sites

    print(ancestors_ts.tables.edges)
    positions = list(positions)
    positions.append(ts.sequence_length)

    for t in ancestors_ts.trees():
        print(t.interval)
        print(positions[int(t.interval[0])], positions[int(t.interval[1])])
        print(t.draw(format="unicode"))
        print("=" * 10)

    A = ancestors_root["ancestors/haplotypes"][:]
    # print(A.astype(np.int8))
    A[A == 255] = 0
    for v in ancestors_ts.variants():
        assert np.array_equal(v.genotypes, A[:, v.index])

    inferred_ts = tsinfer.match_samples(
        input_root, ancestors_ts, method=method, path_compression=path_compression,
        simplify=False) #, traceback_file_pattern="tmp__NOBACKUP__/traceback_{}.pkl")

    print("unsimplified num_edges = ", inferred_ts.num_edges)
    print("unsimplified num_trees = ", inferred_ts.num_trees)

    print(inferred_ts.tables.edges)

    # print("SAMPLES")
    # for t in inferred_ts.trees():
    #     print(t.interval)
    #     print(t.draw(format="unicode"))
    #     print("=" * 10)


    flags = inferred_ts.tables.nodes.flags
    samples = np.where(flags == 1)[0][-ts.num_samples:]
    inferred_ts, node_map = inferred_ts.simplify(samples.astype(np.int32), map_nodes=True)

    assert inferred_ts.num_samples == ts.num_samples
    assert inferred_ts.num_sites == ts.num_sites
    # assert inferred_ts.sequence_length == ts.sequence_length
    assert np.array_equal(G, inferred_ts.genotype_matrix())
    print("simplified num_edges = ", inferred_ts.num_edges)
    print("inferred num_trees = ", inferred_ts.num_trees)

    # # Round the edges to the nearest integer.
    # t = inferred_ts.tables
    # t.edges.set_columns(
    #     np.round(t.edges.left), np.round(t.edges.right),
    #     t.edges.parent, t.edges.child)
    # print(t.edges)
    # inferred_ts = msprime.load_tables(**t.asdict())

    for t in inferred_ts.trees():
        print([site.position for site in t.sites()])
        print("interval = ", t.interval)
        print(t.draw(format="unicode"))

    bp, metrics = tsinfer.compare(ts, inferred_ts)
    print("METRICS")
    for x, v in zip(bp, metrics):
        print(x,"\t", v)



def tsinfer_dev(
        n, L, seed, num_threads=1, recombination_rate=1e-8,
        genotype_quality=0, method="C", log_level="WARNING",
        debug=True, progress=False, path_compression=True):

    np.random.seed(seed)
    random.seed(seed)
    L_megabases = int(L * 10**6)

    daiquiri.setup(level=log_level)

    ts = msprime.simulate(
            n, Ne=10**4, length=L_megabases,
            recombination_rate=1e-8, mutation_rate=1e-8,
            random_seed=seed)
    if debug:
        print("num_sites = ", ts.num_sites)
    assert ts.num_sites > 0

    G = generate_samples(ts, genotype_quality)
    sample_data = tsinfer.SampleData.initialise(
        num_samples=ts.num_samples, sequence_length=ts.sequence_length)
        # filename="tmp.tsis")
    for site, genotypes in zip(ts.sites(), G):
        sample_data.add_variant(site.position, ["0", "1"], genotypes)
    sample_data.finalise()

#     sample_data = tsinfer.SampleData.load("tmp.tsis")
#     print("sample data after save:")
#     print(sample_data)

    ancestor_data = tsinfer.AncestorData.initialise(sample_data)
    tsinfer.build_ancestors(sample_data, ancestor_data)
    ancestor_data.finalise()

    ancestors_ts = tsinfer.match_ancestors(sample_data, ancestor_data, method=method)
    output_ts = tsinfer.match_samples(sample_data, ancestors_ts, method=method)

    A = ancestor_data.genotypes[:].T
    A[A == 255] = 0
    for v in ancestors_ts.variants():
        assert np.array_equal(v.genotypes, A[:, v.index])

    assert output_ts.num_samples == ts.num_samples
    assert output_ts.num_sites == ts.num_sites
    assert output_ts.sequence_length == ts.sequence_length
    assert np.array_equal(G, output_ts.genotype_matrix())


def compress(filename, output_file):
    ts = msprime.load(filename)
    ts.dump(output_file, zlib_compression=True)


def analyse_file(filename):
    before = time.process_time()
    ts = msprime.load(filename)
    duration = time.process_time() - before
    print("loaded in {:.2f} seconds".format(duration))
    print("num_trees = ", ts.num_trees)
    print("size = {:.2f}MiB".format(os.path.getsize(filename) / 1024**2))

    plot_breakpoints(ts, "data/hapmap/genetic_map_GRCh37_chr22.txt",
        "chr22_breakpoints.png")

    before = time.process_time()
    j = 0
    for t in ts.trees():
        j += 1
        # if j == ts.num_trees / 2:
        #     t.draw(path="chr22_tree.svg")
    assert j == ts.num_trees
    duration = time.process_time() - before
    print("Iterated over trees in {:.2f} seconds".format(duration))



    num_children = []
    for j, e in enumerate(ts.edgesets()):
        # print(e.left, e.right, e.parent, ts.time(e.parent), e.children, sep="\t")
        num_children.append(len(e.children))

    num_children = np.array(num_children)

    print("total edges= ", ts.num_edges)
    print("non binary     = ", np.sum(num_children > 2))
    print("max children   = ", np.max(num_children))
    print("mean children  = ", np.mean(num_children))
    print("median children= ", np.median(num_children))

    plt.clf()
    sns.distplot(num_children)
    plt.savefig("chr22_num_children.png")





    # for l, r_out, r_in in ts.diffs():
    #     print(l, len(r_out), len(r_in), sep="\t")

    # for t in ts.trees():
    #     t.draw(
    #         "tree_{}.svg".format(t.index), 4000, 4000, show_internal_node_labels=False,
    #         show_leaf_node_labels=False)
    #     if t.index == 10:
    #         break



def debug_no_recombination():

    method = "P"
    num_samples = 3
    seed = 8
    ts_source = msprime.simulate(num_samples, random_seed=seed, mutation_rate=5)
    print("sim = ", num_samples, ts_source.num_sites, seed)
    nodes = set()
    for site in ts_source.sites():
        for mutation in site.mutations:
            nodes.add(mutation.node)
    assert nodes == set(range(ts_source.num_nodes - 1))


    # ts_inferred = infer_from_simulation(ts_source, method="P")
    # for t in ts_source.trees():
    #     print(t.draw(format="unicode"))
    # print(ts_inferred.num_trees)
    # for t in ts_inferred.trees():
    #     print(t.draw(format="unicode"))
    # assert ts_inferred.num_trees == 1

    input_root = zarr.group()
    tsinfer.InputFile.build(
        input_root, genotypes=ts_source.genotype_matrix(),
        recombination_rate=1,
        sequence_length=ts_source.num_sites,
        compress=False)
    ancestors_root = zarr.group()

    tsinfer.build_ancestors(
        input_root, ancestors_root, method=method, compress=False)

    ancestors_ts = tsinfer.match_ancestors(input_root, ancestors_root, method=method)
    assert ancestors_ts.sequence_length == ts_source.num_sites

    # for t in ancestors_ts.trees():
    #     print(t.draw(format="unicode"))
    # print(ancestors_ts.tables)

    print("Ancestors")
    A = ancestors_root["ancestors/haplotypes"][:]
    print(A)

    print("Samples")
    print(ts_source.genotype_matrix().T)
    A[A == 255] = 0
    for v in ancestors_ts.variants():
        assert np.array_equal(v.genotypes, A[:, v.index])

    inferred_ts = tsinfer.match_samples(input_root, ancestors_ts, method=method,
            simplify=False)

#     for t in inferred_ts.trees():
#         print(t.draw(format="unicode"))

#     print(inferred_ts.tables)

    print("num_edges = ", inferred_ts.num_edges)
    print("num_trees = ", inferred_ts.num_trees)


def build_profile_inputs(n, num_megabases):
    L = num_megabases * 10**6
    ts = msprime.simulate(
        n, length=L, Ne=10**4, recombination_rate=1e-8, mutation_rate=1e-8,
        random_seed=10)
    print("Ran simulation: n = ", n, " num_sites = ", ts.num_sites,
            "num_trees =", ts.num_trees)
    input_file = "tmp__NOBACKUP__/profile-n={}-m={}.input.hdf5".format(
            n, num_megabases)
    ts.dump(input_file)
    filename = "tmp__NOBACKUP__/profile-n={}_m={}.samples".format(n, num_megabases)
    if os.path.exists(filename):
        shutil.rmtree(filename)
    sample_data = tsinfer.SampleData.initialise(
        num_samples=ts.num_samples, sequence_length=ts.sequence_length,
        filename=filename)
    progress_monitor = tqdm.tqdm(total=ts.num_sites)
    for variant in ts.variants():
        sample_data.add_variant(
            variant.site.position, variant.alleles, variant.genotypes)
        progress_monitor.update()
    sample_data.finalise()
    progress_monitor.close()

#     filename = "tmp__NOBACKUP__/profile-n={}_m={}.ancestors".format(n, num_megabases)
#     if os.path.exists(filename):
#         os.unlink(filename)
#     ancestor_data = tsinfer.AncestorData.initialise(sample_data, filename=filename)
#     tsinfer.build_ancestors(sample_data, ancestor_data, progress=True)
#     ancestor_data.finalise()


def build_1kg_sim():
    n = 5008
    chrom = "22"
    infile = "data/hapmap/genetic_map_GRCh37_chr{}.txt".format(chrom)
    recomb_map = msprime.RecombinationMap.read_hapmap(infile)

    # ts = msprime.simulate(
    #     sample_size=n, Ne=10**4, recombination_map=recomb_map,
    #     mutation_rate=5*1e-8)

    # print("simulated chr{} with {} sites".format(chrom, ts.num_sites))

    prefix = "tmp__NOBACKUP__/sim1kg_chr{}".format(chrom)
    outfile = prefix + ".source.ts"
    # ts.dump(outfile)
    ts = msprime.load(outfile)

    V = ts.genotype_matrix()
    print("Built variant matrix: {:.2f} MiB".format(V.nbytes / (1024 * 1024)))
    positions = np.array([site.position for site in ts.sites()])
    recombination_rates = np.zeros_like(positions)
    last_physical_pos = 0
    last_genetic_pos = 0
    for site in ts.sites():
        physical_pos = site.position
        genetic_pos = recomb_map.physical_to_genetic(physical_pos)
        physical_dist = physical_pos - last_physical_pos
        genetic_dist = genetic_pos - last_genetic_pos
        scaled_recomb_rate = 0
        if genetic_dist > 0:
            scaled_recomb_rate = physical_dist / genetic_dist
        recombination_rates[site.index] = scaled_recomb_rate
        last_physical_pos = physical_pos
        last_genetic_pos = genetic_pos

    input_file = prefix + ".tsinf"
    if os.path.exists(input_file):
        os.unlink(input_file)
    input_hdf5 = zarr.DBMStore(input_file, open=bsddb3.btopen)
    root = zarr.group(store=input_hdf5, overwrite=True)
    tsinfer.InputFile.build(
        root, genotypes=V, position=positions,
        recombination_rate=recombination_rates, sequence_length=ts.sequence_length)
    input_hdf5.close()
    print("Wrote", input_file)




def large_profile(input_file, output_file, num_threads=2, log_level="DEBUG"):
    hdf5 = h5py.File(input_file, "r")
    tsp = tsinfer.infer(
        samples=hdf5["samples/haplotypes"][:],
        positions=hdf5["sites/position"][:],
        recombination_rate=hdf5["sites/recombination_rate"][:],
        sequence_length=hdf5.attrs["sequence_length"],
        num_threads=num_threads, log_level=log_level, progress=True)
    tsp.dump(output_file)

    # print(tsp.tables)
    # for t in tsp.trees():
    #     print("tree", t.index)
    #     print(t.draw(format="unicode"))

def save_ancestor_ts(
        n, L, seed, num_threads=1, recombination_rate=1e-8,
        resolve_shared_recombinations=False,
        progress=False, error_rate=0, method="C", log_level="WARNING"):
    L_megabases = int(L * 10**6)
    ts = msprime.simulate(
            n, Ne=10**4, length=L_megabases,
            recombination_rate=1e-8, mutation_rate=1e-8,
            random_seed=seed)
    print("num_sites = ", ts.num_sites)
    positions = np.array([site.position for site in ts.sites()])
    S = generate_samples(ts, 0)
    recombination_rate = np.zeros_like(positions) + recombination_rate

    # make_input_hdf5("ancestor_example.hdf5", S, positions, recombination_rate,
    #         ts.sequence_length)

    manager = tsinfer.InferenceManager(
        S, positions, ts.sequence_length, recombination_rate,
        num_threads=num_threads, method=method, progress=progress, log_level=log_level,
        resolve_shared_recombinations=resolve_shared_recombinations)
        # ancestor_traceback_file_pattern="tmp__NOBACKUP__/tracebacks/tb_{}.pkl")

    manager.initialise()
    manager.process_ancestors()
    ts_new = manager.get_tree_sequence()

    A = manager.ancestors()
    # Need to reset the unknown values to be zeros.
    A[A == -1] = 0
    B = np.zeros((manager.num_ancestors, manager.num_sites), dtype=np.int8)
    for v in ts_new.variants():
        B[:, v.index] = v.genotypes
    assert np.array_equal(A, B)
    print(ts_new.tables)
    # ts.dump("tmp__NOBACKUP__/ancestor_ts-{}.hdf5".format(ts.num_sites))
    for t in ts_new.trees():
        print(t.interval)
        print(t.draw(format="unicode"))
    new_nodes = [j for j, node in enumerate(ts_new.nodes()) if node.flags == 0]
    print(new_nodes)
    for e in ts_new.edges():
        if e.child in new_nodes or e.parent in new_nodes:
            print("{:.0f}\t{:.0f}".format(e.left, e.right), e.parent, e.child, sep="\t")

    nodes = ts_new.tables.nodes
    nodes.set_columns(flags=np.ones_like(nodes.flags), time=nodes.time)
    print(nodes)
    t = ts_new.tables
    tsp = msprime.load_tables(
            nodes=nodes, edges=t.edges,  sites=t.sites, mutations=t.mutations)
    print(tsp.tables)
    for j, h in enumerate(tsp.haplotypes()):
        print(j, "\t",h)



def examine_ancestor_ts(filename):
    ts = msprime.load(filename)
    print("num_sites = ", ts.num_sites)
    print("num_trees = ", ts.num_trees)
    print("num_edges = ", ts.num_edges)

    for (left, right), edges_in, edges_out in ts.edge_diffs():
        print("NEW TREE: {:.2f}".format(right - left), len(edges_in), len(edges_out), sep="\t")
        print("OUT")
        for e in edges_out:
            print("\t", e.parent, e.child)
        print("IN")
        for e in edges_in:
            print("\t", e.parent, e.child)

    # zero_edges = 0
    # edges = msprime.EdgeTable()
    # for e in ts.edges():
    #     if e.parent == 0:
    #         zero_edges += 1
    #     else:
    #         edges.add_row(e.left, e.right, e.parent, e.child)
    # print("zero_edges = ", zero_edges, zero_edges / ts.num_edges)
    # t = ts.tables
    # t.edges = edges
    # ts = msprime.load_tables(**t.asdict())
    # print("num_sites = ", ts.num_sites)
    # print("num_trees = ", ts.num_trees)
    # print("num_edges = ", ts.num_edges)

    # for t in ts.trees():
    #     print("Tree:", t.interval)
    #     print(t.draw(format="unicode"))
    #     print("=" * 200)

    # import pickle
    # j = 960
    # filename = "tmp__NOBACKUP__/tracebacks/tb_{}.pkl".format(j)
    # with open(filename, "rb") as f:
    #     debug = pickle.load(f)

    # tracebacks = debug["traceback"]
    # # print("focal = ", debug["focal_sites"])
    # del debug["traceback"]
    # print("debug:", debug)
    # a = debug["ancestor"]
    # lengths = [len(t) for t in tracebacks]
    # import matplotlib as mp
    # # Force matplotlib to not use any Xwindows backend.
    # mp.use('Agg')
    # import matplotlib.pyplot as plt

    # plt.clf()
    # plt.plot(lengths)
    # plt.savefig("tracebacks_{}.png".format(j))

#     start = 0
#     for j, t in enumerate(tracebacks[start:]):
#         print("TB", j, len(t))
#         for k, v in t.items():
#             print("\t", k, "\t", v)

#     site_id = 0
#     for t in ts.trees():
#         for site in t.sites():
#             L = tracebacks[site_id]
#             site_id += 1
#         # print("TREE")
#             print(L)
#             # for x1 in L.values():
#             #     for x2 in L.values():
#             #         print("\t", x1, x2, x1 == x2, sep="\t")
#             print("SITE = ", site_id)
#             print("root children = ", len(t.children(t.root)))
#             for u, v in L.items():
#                 path = []
#                 while u != msprime.NULL_NODE:
#                     path.append(u)
#                     u = t.parent(u)
#                     # if u in L and L[u] == v:
#                     #     print("ERROR", u)
#                 print(v, path)
#             print()
#             node_labels = {u: "{}:{:.2G}".format(u, L[u]) for u in L.keys()}
#             if site_id == 694:
#                 print(t.draw(format="unicode", node_label_text=node_labels))


    # for j, L in enumerate(tracebacks):
    #     print(j, L)
        # if len(L) > max_len:
        #     max_len = len(L)
        #     max_index = j
    # # print(j, "\t", L)
    # print("max len = ", max_len)
    # for k, v in tracebacks[max_index].items():
        # print(k, "\t", v)

def verify(file1, file2):
    ts1 = msprime.load(file1)
    ts2 = msprime.load(file2)
    assert ts1.num_samples == ts2.num_samples
    assert ts1.num_sites == ts2.num_sites

    for v1, v2 in zip(ts1.variants(), ts2.variants()):
        assert v1.position == v2.position
        assert np.array_equal(v1.genotypes, v2.genotypes)

def lookat(filename):

    # for j in range(1, 1000):
    #     tbfile = "tmp__NOBACKUP__/traceback_{}.pkl".format(j)
    #     with open(tbfile, "rb") as f:
    #         debug = pickle.load(f)
    #         tb = debug["traceback"]
    #         for j, row in enumerate(tb):
    #             # print(j, row)
    #             if 596 in row:
    #                 print(j)
    #                 for k, v in row.items():
    #                     print("\t", k, "\t{:.14f}".format(v))

    ts = msprime.load(filename)
    print(ts.num_edges, ts.num_trees, ts.num_sites)

    for t in ts.trees():
        nodes = set()
        site_nodes = []
        for site in t.sites():
            for mutation in site.mutations:
                nodes.add(mutation.node)
                site_nodes.append((site.index, mutation.node))
        tree_nodes = set(t.nodes())
        print("site nodes = ", nodes)
        print(site_nodes)
        # print(tree_nodes - nodes)


        # print(len(list(t.sites())(
        print(t.draw(format="unicode"))
        print("=" * 20)


    # sys.exit(0)


def asserts_fail():

    # seed = 1
    for seed in range(1, 1000):
    # for seed in [11]:
        print("seed=", seed, file=sys.stderr)
        ts = msprime.simulate(40, mutation_rate=20, recombination_rate=1,
                random_seed=seed, model="smc_prime")
        print("num_sites = ", ts.num_sites)
        if ts.num_sites == 0:
            continue

        positions = pos = np.array([v.position for v in ts.variants()])
        S = np.zeros((ts.sample_size, ts.num_mutations), dtype="u1")
        for variant in ts.variants():
            S[:,variant.index] = variant.genotypes

        G = S.astype(np.uint8).T

        #Create the ancestors
        input_root = zarr.group()
        tsinfer.InputFile.build(
            input_root, genotypes=G,
            # genotype_qualities=tsinfer.proba_to_phred(error_probability),
            position=positions,
            recombination_rate=1, sequence_length=ts.sequence_length,
            compress=False)
        ancestors_root = zarr.group()

        #tsinfer.extract_ancestors(ts, ancestors_root)
        tsinfer.build_simulated_ancestors(input_root, ancestors_root, ts)

        # A = ancestors_root["ancestors/haplotypes"][:]
        # print(S)

        # print("ANCESTORS")
        # # print(A.astype(np.int8))
        # for j, a in enumerate(A):
        #     s = "".join(str(x) if x < 255 else "*" for x in a)
        #     print(j, "\t", s)

        method = "C"
        ancestors_ts = tsinfer.match_ancestors(input_root, ancestors_root, method=method)
        assert ancestors_ts.sequence_length == ts.num_sites
        inferred_ts = tsinfer.match_samples(
            input_root, ancestors_ts, method=method,
            simplify=False)

        print("inferred num_edges = ", inferred_ts.num_edges)


if __name__ == "__main__":

    np.set_printoptions(linewidth=20000)
    np.set_printoptions(threshold=20000000)

    # lookat(sys.argv[1])

    #asserts_fail()

    # debug_pathological()
    # debug_real_ancestor_injection(10)

    # build_1kg_sim()

    # compress(sys.argv[1], sys.argv[2])
    # analyse_file(sys.argv[1])

    # verify(sys.argv[1], sys.argv[2])

    # build_profile_inputs(10, 1)

    # build_profile_inputs(1000, 10)
    # build_profile_inputs(1000, 100)
    # build_profile_inputs(10**4, 100)
    # build_profile_inputs(10**5, 100)

    # build_profile_inputs(100)

    # debug_no_recombination()

    # large_profile(sys.argv[1], "{}.inferred.hdf5".format(sys.argv[1]),
    #         num_threads=40, log_level="DEBUG")

    # save_ancestor_ts(100, 10, 1, recombination_rate=1, num_threads=2)
    # examine_ancestor_ts(sys.argv[1])

    # save_ancestor_ts(15, 0.03, 7, recombination_rate=1, method="P",
    #         resolve_shared_recombinations=False)

    tsinfer_dev(10, 0.1, seed=6, num_threads=0,
            genotype_quality=0.0, method="P") #, log_level="WARNING")

    # tsinfer_dev(400, 20, seed=84, num_threads=0, method="C",
    #         genotype_quality=0.001)
    # tsinfer_dev(4, 0.2, seed=84, num_threads=0, method="C",
    #         log_level="WARNING")

    # for seed in range(1, 10000):
    # # for seed in [2]:
    #     print(seed)
    #     # check_infer(20, 0.2, seed=seed, genotype_quality=0.0, num_threads=0, method="P")
    #     # tsinfer_dev(40, 2.5, seed=seed, num_threads=1, genotype_quality=1e-3, method="C")

    #     # tsinfer_dev(30, 0.2, seed=seed, genotype_quality=0.0, num_threads=0, method="P")
    #     tsinfer_dev(30, 1.5, seed=seed, num_threads=2, genotype_quality=0.0,
    #             method="C", path_compression=True)
    # # tsinfer_dev(60, 1000, num_threads=5, seed=1, error_rate=0.1, method="C",
    # #         log_level="INFO", progress=True)
    # for seed in range(1, 1000):
    #     print(seed)
