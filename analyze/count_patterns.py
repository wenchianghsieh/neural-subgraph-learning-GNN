import argparse
import csv
import time
import os
import json
import subprocess

import numpy as np
import torch
import torch.optim as optim
import torch.nn as nn
import torch.nn.functional as F

from torch_geometric.datasets import TUDataset
from torch_geometric.datasets import Planetoid, KarateClub, QM7b
from torch_geometric.data import DataLoader
import torch_geometric.utils as pyg_utils
from ogb.nodeproppred import PygNodePropPredDataset, Evaluator

import torch_geometric.nn as pyg_nn
from matplotlib import cm

from common import data
from common import models
from common import utils
from subgraph_mining import decoder

from tqdm import tqdm
import matplotlib.pyplot as plt

from multiprocessing import Pool
import random
from sklearn.manifold import TSNE
from sklearn.cluster import KMeans
from collections import defaultdict, Counter
from itertools import permutations
from queue import PriorityQueue
import matplotlib.colors as mcolors
import networkx as nx
import networkx.algorithms.isomorphism as iso
import pickle
import torch.multiprocessing as mp
from sklearn.decomposition import PCA

import orca

import tempfile


def arg_parse():
    parser = argparse.ArgumentParser(description='count graphlets in a graph')
    parser.add_argument('--dataset', type=str)
    parser.add_argument('--queries_path', type=str)
    parser.add_argument('--out_path', type=str)
    parser.add_argument('--n_workers', type=int)
    parser.add_argument('--count_method', type=str)
    parser.add_argument('--baseline', type=str)
    parser.add_argument('--node_anchored', action="store_true")
    parser.set_defaults(dataset="enzymes",
                        queries_path="results/out-patterns.p",
                        out_path="results/counts.json",
                        n_workers=1,
                        count_method="bin",
                        baseline="none")
    # node_anchored=True)
    return parser.parse_args()


tgt_fn_cache = {}


def subgraph_isom(args):
    target, queries, is_anchored, target_anchor = args
    # print("making target")
    if target_anchor not in tgt_fn_cache:
        tgt_fn = os.path.join(tempfile.gettempdir(),
                              f'target{str(random.random())}')  # "/tmp/target" + str(random.random())
        with open(tgt_fn, "w") as f:
            # graph = nx.convert_node_labels_to_integers(target)
            graph = target
            f.write("t {} {}\n".format(1, len(graph)))
            for j in range(len(graph)):
                f.write("v {} {}\n".format(j, 1 if j == target_anchor and
                                                   is_anchored else 0))
            for u, v in sorted(graph.edges):
                f.write("e {} {} {}\n".format(u, v, 0))
        # tgt_fn_cache[target_anchor] = tgt_fn
    else:
        tgt_fn = tgt_fn_cache[target_anchor]

    # print("making query")
    qry_fn = os.path.join(tempfile.gettempdir(),
                          f'query{str(random.random())}')  # "/tmp/query" + str(random.random())
    # print(len(queries), "queries")
    # print(qry_fn)
    with open(qry_fn, "w") as f:
        for i, query in enumerate(queries):
            graph = nx.convert_node_labels_to_integers(query)
            f.write("t {} {} {}\n".format(i, len(query), len(query.edges) * 2))
            for j in range(len(graph)):
                f.write("{} {} {} {}\n".format(j, graph.nodes[j]["anchor"] if
                is_anchored else 0,
                                               graph.degree(j), " ".join([str(x) for x in
                                                                          sorted(graph.neighbors(j))])))

    # print("running")
    p = subprocess.Popen(["DAF/daf_twitter", "-d", tgt_fn, "-q", qry_fn, "-n",
                          str(len(queries)), "-m", "1"], stdout=subprocess.PIPE)
    counts = {}
    for i, line in enumerate(p.stdout.readlines()):
        toks = line.decode("utf-8").strip().split(" ")
        # print(line)
        if i >= 4:
            stat = toks[-1]
            if stat == "ms":
                break
            else:
                counts[i - 4] = 1 if int(toks[-2]) > 0 else 0

    # os.remove(tgt_fn)
    # os.remove(qry_fn)
    # print("done")
    return counts


def gen_baseline_queries(queries, targets, method="mfinder",
                         node_anchored=False):
    # use this to generate N size K queries
    # queries = [[0]*n for n in range(5, 21) for i in range(10)]
    if method == "mfinder":
        return utils.gen_baseline_queries_mfinder(queries, targets,
                                                  node_anchored=node_anchored)
    elif method == "rand-esu":
        return utils.gen_baseline_queries_rand_esu(queries, targets,
                                                   node_anchored=node_anchored)
    neighs = []
    for i, query in enumerate(queries):
        print(i)
        found = False
        if len(query) == 0:
            neighs.append(query)
            found = True
        while not found:
            if method == "radial":
                graph = random.choice(targets)
                node = random.choice(list(graph.nodes))
                neigh = list(nx.single_source_shortest_path_length(graph, node,
                                                                   cutoff=3).keys())
                # neigh = random.sample(neigh, min(len(neigh), 15))
                neigh = graph.subgraph(neigh)
                neigh = neigh.subgraph(list(sorted(nx.connected_components(
                    neigh), key=len))[-1])
                neigh = nx.convert_node_labels_to_integers(neigh)
                print(i, len(neigh), len(query))
                if len(neigh) == len(query):
                    neighs.append(neigh)
                    found = True
            elif method == "tree":
                # https://academic.oup.com/bioinformatics/article/20/11/1746/300212
                graph = random.choice(targets)
                start_node = random.choice(list(graph.nodes))
                neigh = [start_node]
                frontier = list(set(graph.neighbors(start_node)) - set(neigh))
                while len(neigh) < len(query) and frontier:
                    new_node = random.choice(list(frontier))
                    assert new_node not in neigh
                    neigh.append(new_node)
                    frontier += list(graph.neighbors(new_node))
                    frontier = [x for x in frontier if x not in neigh]
                if len(neigh) == len(query):
                    neigh = graph.subgraph(neigh)
                    neigh = nx.convert_node_labels_to_integers(neigh)
                    neighs.append(neigh)
                    found = True
    return neighs


cleaned_target_cache = None


def count_graphlets_helper(inp):
    i, queries, target, method, node_anchored, anchor_or_none = inp
    # NOTE: removing self loops!!
    queries = queries.copy()
    queries = [q.remove_edges_from(nx.selfloop_edges(q)) for q in queries]
    # if node_anchored and method == "bin":
    #    n_chances_left = sum([len(g) for g in targets])
    if method == "freq":
        ismags = nx.isomorphism.ISMAGS(query, query)
        n_symmetries = len(list(ismags.isomorphisms_iter(symmetry=False)))

    # print(n_symmetries, "symmetries")
    n, n_bin = 0, 0
    global cleaned_target_cache
    if cleaned_target_cache is None:
        cleaned_target_cache = target.copy()
        cleaned_target_cache.remove_edges_from(
            nx.selfloop_edges(cleaned_target_cache))
    else:
        target = cleaned_target_cache  # .copy()
    # print(i, j, len(target), n / n_symmetries)
    # matcher = nx.isomorphism.ISMAGS(target, query)
    if method == "bin":
        if node_anchored:
            for anchor in (target.nodes if anchor_or_none is None else
            [anchor_or_none]):
                # if random.random() > 0.1: continue
                # nx.set_node_attributes(target, 0, name="anchor")
                # target.nodes[anchor]["anchor"] = 1

                n += subgraph_isom(target, queries, is_anchored=True,
                                   target_anchor=anchor)
                # TODO: old
                # matcher = iso.GraphMatcher(target, query,
                #    node_match=iso.categorical_node_match(["anchor"], [0]))
                # if matcher.subgraph_is_isomorphic():
                #    n += 1
            # else:
            # n_chances_left -= 1
            # if n_chances_left < min_count:
            #    return i, -1
        else:
            matcher = iso.GraphMatcher(target, query)
            n += int(matcher.subgraph_is_isomorphic())
    elif method == "freq":
        matcher = iso.GraphMatcher(target, query)
        n += len(list(matcher.subgraph_isomorphisms_iter())) / n_symmetries
    else:
        print("counting method not understood")
    # n_matches.append(n / n_symmetries)
    # print(i, n / n_symmetries)
    count = n  # / n_symmetries
    # if include_bin:
    #    count = (count, n_bin)
    # print(i, count)
    return i, count


def count_graphlets_mp(queries, targets, n_workers=1, method="bin",
                       node_anchored=False, min_count=0):
    print(len(queries), len(targets))
    # idxs, counts = zip(*[count_graphlets_helper((i, q, targets, include_bin))
    #    for i, q in enumerate(queries)])
    # counts = list(counts)
    # return counts

    n_matches = defaultdict(float)
    # for i, query in enumerate(queries):
    pool = Pool(processes=n_workers)
    if node_anchored:
        # inp = [(i, query, target, method, node_anchored, anchor) for i, query
        #    in enumerate(queries) for target in targets for anchor in (target
        #        if len(targets) < 10 else [None])]
        # TODO: tmp for arxiv!!!!!!!!!!!!!!!!!!!!!
        anchors = random.sample(targets[0].nodes, 10000)
        # inp = [(i, query, target, method, node_anchored, anchor) for i, query
        #    in enumerate(queries) for target in targets for anchor in anchors]
        inp = [(i, queries, target, method, node_anchored, anchor) for i,
                                                                       target in enumerate(targets) for anchor in
               anchors]
    else:
        inp = [(i, query, target, method, node_anchored, None) for i, query
               in enumerate(queries) for target in targets]
    print(len(inp))
    n_done = 0
    for i, n in pool.imap_unordered(count_graphlets_helper, inp):
        print(n_done, len(n_matches), i, n, "                ", end="\r")
        n_matches[i] += n
        n_done += 1
    print()
    n_matches = [n_matches[i] for i in range(len(n_matches))]
    return n_matches


def count_graphlets(queries, targets, n_workers=1, method="bin",
                    node_anchored=False, min_count=0):
    isom = {k: 0 for k in range(len(queries))}
    pool = Pool(processes=n_workers)

    args = []
    x = range(len(targets[0]))
    for anchor in [random.choice(x) for _ in range(5000)]:  # random.sample(range(len(targets[0])), 5000):
        args.append((targets[0], queries, True, anchor))

    n_done = 0
    for d in pool.imap_unordered(subgraph_isom, args):
        for k, v in d.items():
            isom[k] += v
        print(n_done, [isom[x] for x in range(len(queries))])
        n_done += 1
    return isom


def count_exact(queries, targets, args):
    print("WARNING: orca only works for node anchored")
    # TODO: non node anchored
    n_matches_baseline = np.zeros(73)
    for target in targets:
        counts = np.array(orca.orbit_counts("node", 5, target))
        if args.count_method == "bin":
            counts = np.sign(counts)
        counts = np.sum(counts, axis=0)
        n_matches_baseline += counts
    # don't include size < 5
    n_matches_baseline = list(n_matches_baseline)[15:]
    counts5 = []
    num5 = 10  # len([q for q in queries if len(q) == 5])
    for x in list(sorted(n_matches_baseline, reverse=True))[:num5]:
        print(x)
        counts5.append(x)
    print("Average for size 5:", np.mean(np.log10(counts5)))

    atlas = [g for g in nx.graph_atlas_g()[1:] if nx.is_connected(g)
             and len(g) == 6]
    queries = []
    for g in atlas:
        for v in g.nodes:
            g = g.copy()
            nx.set_node_attributes(g, 0, name="anchor")
            g.nodes[v]["anchor"] = 1
            is_dup = False
            for g2 in queries:
                if nx.is_isomorphic(g, g2, node_match=(lambda a, b: a["anchor"]
                                                                    == b["anchor"]) if args.node_anchored else None):
                    is_dup = True
                    break
            if not is_dup:
                queries.append(g)
    print(len(queries))
    n_matches_baseline = count_graphlets(queries, targets,
                                         n_workers=args.n_workers, method=args.count_method,
                                         node_anchored=args.node_anchored,
                                         min_count=10000)
    counts6 = []
    num6 = 20  # len([q for q in queries if len(q) == 6])
    for x in list(sorted(n_matches_baseline, reverse=True))[:num6]:
        print(x)
        counts6.append(x)
    print("Average for size 6:", np.mean(np.log10(counts6)))
    return counts5 + counts6


if __name__ == "__main__":
    args = arg_parse()
    print("Using {} workers".format(args.n_workers))
    print("Baseline:", args.baseline)

    if args.dataset == 'enzymes':
        dataset = TUDataset(root='/tmp/ENZYMES', name='ENZYMES')
    elif args.dataset == 'cox2':
        dataset = TUDataset(root='/tmp/cox2', name='COX2')
    elif args.dataset == 'reddit-binary':
        dataset = TUDataset(root='/tmp/REDDIT-BINARY', name='REDDIT-BINARY')
    elif args.dataset == 'coil':
        dataset = TUDataset(root='/tmp/COIL-DEL', name='COIL-DEL')
    elif args.dataset == 'ppi-pathways':
        graph = nx.Graph()
        with open("data/ppi-pathways.csv", "r") as f:
            reader = csv.reader(f)
            for row in reader:
                graph.add_edge(int(row[0]), int(row[1]))
        dataset = [graph]
    elif args.dataset in ['diseasome', 'usroads', 'mn-roads', 'infect']:
        fn = {"diseasome": "bio-diseasome.mtx",
              "usroads": "road-usroads.mtx",
              "mn-roads": "mn-roads.mtx",
              "infect": "infect-dublin.edges"}
        graph = nx.Graph()
        with open("data/{}".format(fn[args.dataset]), "r") as f:
            for line in f:
                if not line.strip(): continue
                a, b = line.strip().split(" ")
                graph.add_edge(int(a), int(b))
        dataset = [graph]
    elif args.dataset.startswith('plant-'):
        size = int(args.dataset.split("-")[-1])
        dataset = decoder.make_plant_dataset(size)
    elif args.dataset == "analyze":
        with open("results/analyze.p", "rb") as f:
            cand_patterns, _ = pickle.load(f)
            queries = [q for score, q in cand_patterns[10]][:200]
        dataset = TUDataset(root='/tmp/ENZYMES', name='ENZYMES')
    elif args.dataset == "arxiv":
        dataset = PygNodePropPredDataset(name="ogbn-arxiv")
        task = "graph"
    elif args.dataset.startswith('data-'):
        task = "graph"
        dataset = []
        # for custom dataset
        # get custom dataset name
        dataset_name = args.dataset.replace('data-', '', 1)

        # path of dataset
        dataset_path = os.path.join('./data', dataset_name)

        # all name of files in custom dataset
        file_names = os.listdir(dataset_path)

        # read each file and covert it
        for file_name in file_names:
            if file_name.split('.')[-1] == 'gexf':
                # read gexf file
                file_path = os.path.join(dataset_path, file_name)
                g = nx.read_gexf(file_path, node_type=int)
                g_pyg = pyg_utils.from_networkx(g, group_node_attrs=['r'])
                g_covert = pyg_utils.to_networkx(g_pyg, node_attrs=['x'])
                relabel_mapping = {k: v['x'] for k, v in dict(g_covert.nodes.data()).items()}
                g_relabel = nx.relabel_nodes(g_covert, relabel_mapping)
                dataset.append(g_relabel)

    targets = []
    for i in range(len(dataset)):
        graph = dataset[i]
        if not type(graph) == nx.Graph and not type(graph) == nx.DiGraph:
            graph = pyg_utils.to_networkx(dataset[i]).to_undirected()
            graph = nx.convert_node_labels_to_integers(graph)
        targets.append(graph)

    if args.dataset != "analyze":
        with open(args.queries_path, "rb") as f:
            queries = pickle.load(f)

    # filter only top nonisomorphic size 6 motifs
    # filt_q = []
    # for q in queries:
    #    if len([qc for qc in filt_q if nx.is_isomorphic(q, qc)]) == 0:
    #        filt_q.append(q)
    # queries = filt_q[:]
    # print(len(queries))

    query_lens = [len(query) for query in queries]

    if args.baseline == "exact":
        n_matches_baseline = count_exact(queries, targets, args)
        n_matches = count_graphlets(queries[:len(n_matches_baseline)], targets,
                                    n_workers=args.n_workers, method=args.count_method,
                                    node_anchored=args.node_anchored)
    elif args.baseline == "none":
        n_matches = count_graphlets(queries, targets,
                                    n_workers=args.n_workers, method=args.count_method,
                                    node_anchored=args.node_anchored)
    else:
        baseline_queries = gen_baseline_queries(queries, targets,
                                                node_anchored=args.node_anchored, method=args.baseline)
        query_lens = [len(q) for q in baseline_queries]
        n_matches = count_graphlets(baseline_queries, targets,
                                    n_workers=args.n_workers, method=args.count_method,
                                    node_anchored=args.node_anchored)
    with open(args.out_path, "w") as f:
        json.dump((query_lens, n_matches, []), f)
