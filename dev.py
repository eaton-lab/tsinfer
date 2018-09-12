
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
import pprint
import numpy as np
import json

import matplotlib as mp
# Force matplotlib to not use any Xwindows backend.
mp.use('Agg')
import matplotlib.pyplot as plt
import seaborn as sns

import tsinfer
import msprime



def plot_breakpoints(ts, map_file, output_file):
    # Read in the recombination map using the read_hapmap engine,
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

def insert_srb_ancestors(ts):
    tables = ts.dump_tables()

    srb_index = {}
    edges = sorted(ts.edges(), key=lambda e: (e.child, e.left))
    last_edge = edges[0]
    for edge in edges[1:]:
        if not ts.node(edge.child).is_sample():
            continue
        if edge.child == last_edge.child and edge.left == last_edge.right:
            key = edge.left, last_edge.parent, edge.parent
            if key in srb_index:
                count, left_bound, right_bound = srb_index[key]
                srb_index[key] = (
                    count + 1,
                    max(left_bound, last_edge.left),
                    min(right_bound, edge.right))
            else:
                srb_index[key] = 1, last_edge.left, edge.right
        last_edge = edge

    # The nodes that we want to keep are all those *except* what
    # has been marked as samples.
    samples = np.where(tables.nodes.flags != 1)[0].astype(np.int32)

    # Mark all nodes as samples
    tables.nodes.set_columns(
        flags=np.bitwise_or(tables.nodes.flags, 1),
        time=tables.nodes.time,
        population=tables.nodes.population,
        individual=tables.nodes.individual,
        metadata=tables.nodes.metadata,
        metadata_offset=tables.nodes.metadata_offset)
    # Now simplify down the tables to get rid of all sample edges.
    node_id_map = tables.simplify(samples)

    # We cannot have flags that are both samples and synthetic.
    flags = np.zeros_like(tables.nodes.flags)
    flags[tables.nodes.flags == 1] = 1
    index = np.bitwise_and(tables.nodes.flags, tsinfer.SYNTHETIC_NODE_BIT) != 0
    flags[index] = tsinfer.SYNTHETIC_NODE_BIT
    time = tables.nodes.time

    tables.nodes.set_columns(
        flags=flags,
        time=time,
        population=tables.nodes.population,
        individual=tables.nodes.individual,
        metadata=tables.nodes.metadata,
        metadata_offset=tables.nodes.metadata_offset)

    num_extra = 0
    for k, v in srb_index.items():
        if v[0] > 1:
            left, right = v[1:]
            x, pl, pr = k
            pl = node_id_map[pl]
            pr = node_id_map[pr]
            t = min(time[pl], time[pr]) - 1e-4
            node = tables.nodes.add_row(flags=1 << 17, time=t)
            tables.edges.add_row(left, x, pl, node)
            tables.edges.add_row(x, right, pr, node)
            num_extra += 1

            # print("New ancestor:", node, "t = ", t)
            # print("\te1 = ", left, x, pl)
            # print("\te2 = ", x, right, pr)
    print("Generated", num_extra)
    tables.sort()
    # print(tables)
    ancestors_ts = tables.tree_sequence()
    # for tree in ancestors_ts.trees():
    #     print(tree.interval)
    #     print(tree.draw(format="unicode"))
    return ancestors_ts


def tsinfer_dev(
        n, L, seed, num_threads=1, recombination_rate=1e-8,
        error_rate=0, engine="C", log_level="WARNING",
        debug=True, progress=False, path_compression=True):

    np.random.seed(seed)
    random.seed(seed)
    L_megabases = int(L * 10**6)

    # daiquiri.setup(level=log_level)

    ts = msprime.simulate(
            n, Ne=10**4, length=L_megabases,
            recombination_rate=recombination_rate, mutation_rate=1e-8,
            random_seed=seed)
    if debug:
        print("num_sites = ", ts.num_sites)
    assert ts.num_sites > 0

    samples = tsinfer.SampleData.from_tree_sequence(ts)

    # ancestors_ts = tsinfer.make_ancestors_ts(samples, ts, remove_leaves=False)

    ancestor_data = tsinfer.generate_ancestors(
        samples, engine=engine, num_threads=num_threads)
    ancestors_ts = tsinfer.match_ancestors(
        samples, ancestor_data, engine=engine, path_compression=False,
        extended_checks=False)

    ts = tsinfer.match_samples(samples, ancestors_ts,
            path_compression=False, engine=engine,
            simplify=False)

    # print(ts.tables.nodes)
    # print(ts.tables.edges)
    # print(ts.dump_tables())

    simplified = ts.simplify()
    print("edges before = ", simplified.num_edges)

    new_ancestors_ts = insert_srb_ancestors(ts)
    ts = tsinfer.match_samples(samples, new_ancestors_ts,
            path_compression=False, engine=engine,
            simplify=True)

    tables = ts.tables
    flags = tables.nodes.flags
    srb_parents = np.bitwise_and(flags[tables.edges.parent], 1 << 17) != 0

    print("num srb parent edges = ", np.sum(srb_parents))
    print("edges after = ", ts.num_edges)
    # print(ts.tables.nodes)

#     for tree in ts.trees():
#         print(tree.interval)
#         print(tree.draw(format="unicode"))

    # print(ts.tables.edges)
    # for tree in ts.trees():
    #     print(tree.draw(format="unicode"))

    tsinfer.verify(samples, ts)


#     for node in ts.nodes():
#         if tsinfer.is_synthetic(node.flags):
#             print("Synthetic node", node.id, node.time)
#             parent_edges = [edge for edge in ts.edges() if edge.parent == node.id]
#             child_edges = [edge for edge in ts.edges() if edge.child == node.id]
#             child_edges.sort(key=lambda e: e.left)
#             print("parent edges")
#             for edge in parent_edges:
#                 print("\t", edge)
#             print("child edges")
#             for edge in child_edges:
#                 print("\t", edge)

#     # output_ts = tsinfer.match_samples(subset_samples, ancestors_ts, engine=engine)
#     output_ts = tsinfer.match_samples(sample_data, ancestors_ts, engine=engine)
#     # dump_provenance(output_ts)


def dump_provenance(ts):
    print("dump provenance")
    for p in ts.provenances():
        print("-" * 50)
        print(p.timestamp)
        pprint.pprint(json.loads(p.record))


def build_profile_inputs(n, num_megabases):
    L = num_megabases * 10**6
    input_file = "tmp__NOBACKUP__/profile-n={}-m={}.input.trees".format(
            n, num_megabases)
    if os.path.exists(input_file):
        ts = msprime.load(input_file)
    else:
        ts = msprime.simulate(
            n, length=L, Ne=10**4, recombination_rate=1e-8, mutation_rate=1e-8,
            random_seed=10)
        print("Ran simulation: n = ", n, " num_sites = ", ts.num_sites,
                "num_trees =", ts.num_trees)
        ts.dump(input_file)
    filename = "tmp__NOBACKUP__/profile-n={}-m={}.samples".format(n, num_megabases)
    if os.path.exists(filename):
        os.unlink(filename)
    # daiquiri.setup(level="DEBUG")
    with tsinfer.SampleData(
            sequence_length=ts.sequence_length, path=filename,
            num_flush_threads=4) as sample_data:
        # progress_monitor = tqdm.tqdm(total=ts.num_samples)
        # for j in range(ts.num_samples):
        #     sample_data.add_sample(metadata={"name": "sample_{}".format(j)})
        #     progress_monitor.update()
        # progress_monitor.close()
        progress_monitor = tqdm.tqdm(total=ts.num_sites)
        for variant in ts.variants():
            sample_data.add_site(variant.site.position, variant.genotypes)
            progress_monitor.update()
        progress_monitor.close()

    print(sample_data)

#     filename = "tmp__NOBACKUP__/profile-n={}_m={}.ancestors".format(n, num_megabases)
#     if os.path.exists(filename):
#         os.unlink(filename)
#     ancestor_data = tsinfer.AncestorData.initialise(sample_data, filename=filename)
#     tsinfer.build_ancestors(sample_data, ancestor_data, progress=True)
#     ancestor_data.finalise()

def copy_1kg():
    source = "tmp__NOBACKUP__/1kg_chr22.samples"
    sample_data = tsinfer.SampleData.load(source)
    copy = sample_data.copy("tmp__NOBACKUP__/1kg_chr22_copy.samples")
    copy.finalise()
    print(sample_data)
    print("copy = ")
    print(copy)

def tutorial_samples():
    import tqdm
    import msprime
    import tsinfer

    ts = msprime.simulate(
        sample_size=10000, Ne=10**4, recombination_rate=1e-8,
        mutation_rate=1e-8, length=10*10**6, random_seed=42)
    ts.dump("tmp__NOBACKUP__/simulation-source.trees")
    print("simulation done:", ts.num_trees, "trees and", ts.num_sites,  "sites")

    progress = tqdm.tqdm(total=ts.num_sites)
    with tsinfer.SampleData(
            path="tmp__NOBACKUP__/simulation.samples",
            sequence_length=ts.sequence_length,
            num_flush_threads=2) as sample_data:
        for var in ts.variants():
            sample_data.add_site(var.site.position, var.genotypes, var.alleles)
            progress.update()
    progress.close()


def subset_sites(ts, position):
    """
    Return a copy of the specified tree sequence with sites reduced to those
    with positions in the specified list.
    """
    tables = ts.dump_tables()
    lookup = frozenset(position)
    tables.sites.clear()
    tables.mutations.clear()
    for site in ts.sites():
        if site.position in lookup:
            site_id = tables.sites.add_row(
                site.position, ancestral_state=site.ancestral_state,
                metadata=site.metadata)
            for mutation in site.mutations:
                tables.mutations.add_row(
                    site_id, node=mutation.node, parent=mutation.parent,
                    derived_state=mutation.derived_state,
                    metadata=mutation.metadata)
    return tables.tree_sequence()

def run_build():

    sample_data = tsinfer.load(sys.argv[1])
    ad = tsinfer.generate_ancestors(sample_data)
    print(ad)


if __name__ == "__main__":

    # run_build()

    # np.set_printoptions(linewidth=20000)
    # np.set_printoptions(threshold=20000000)

    # tutorial_samples()

    # build_profile_inputs(10, 10)
    # build_profile_inputs(100, 10)
    # build_profile_inputs(1000, 100)
    # build_profile_inputs(10**4, 100)
    # build_profile_inputs(10**5, 100)

    # for j in range(1, 100):
    #     tsinfer_dev(15, 0.5, seed=j, num_threads=0, engine="P", recombination_rate=1e-8)
    # copy_1kg()
    tsinfer_dev(320, 10.15, seed=4, num_threads=0, engine="C", recombination_rate=1e-8)

    # minimise_dev()

#     for seed in range(1, 10000):
#         print(seed)
#         # tsinfer_dev(40, 2.5, seed=seed, num_threads=1, genotype_quality=1e-3, engine="C")
