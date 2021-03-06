#
# Copyright (C) 2016 University of Oxford
#
# This file is part of msprime.
#
# msprime is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# msprime is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with msprime.  If not, see <http://www.gnu.org/licenses/>.
#
"""
Module responsible for converting tree sequence files from older
formats.
"""
from __future__ import division
from __future__ import print_function

import json
import logging

try:
    import h5py
    _h5py_imported = True
    # Numpy is required by h5py, so we can safely use it.
    import numpy as np
except ImportError:
    _h5py_imported = False


import msprime
import msprime.exceptions as exceptions


def _check_h5py():
    if not _h5py_imported:
        raise RuntimeError("h5py is required for converting HDF5 files.")


def _get_v2_provenance(command, attrs):
    """
    Returns the V2 tree provenance attributes reformatted as a V3
    provenance string.
    """
    environment = {}
    parameters = {}
    # Try to get the provenance strings. Malformed JSON should not prevent us
    # from finishing the conversion.
    try:
        environment = json.loads(str(attrs["environment"]))
    except ValueError:
        logging.warn("Failed to convert environment provenance")
    try:
        parameters = json.loads(str(attrs["parameters"]))
    except ValueError:
        logging.warn("Failed to convert parameters provenance")
    provenance = msprime.get_provenance_dict(command, parameters)
    provenance["version"] = environment.get("msprime_version", "Unknown_version")
    provenance["environment"] = environment
    return json.dumps(provenance).encode()


def _get_upgrade_provenance(root):
    """
    Returns the provenance string from upgrading the specified HDF5 file.
    """
    # TODO add more parameters here like filename, etc.
    parameters = {
        "source_version": list(map(int, root.attrs["format_version"]))
    }
    s = json.dumps(msprime.get_provenance_dict("upgrade", parameters))
    return s.encode()


def _convert_hdf5_mutations(
        mutations_group, sites, mutations, remove_duplicate_positions):
    """
    Loads the v2/v3 into the specified tables.
    """
    position = np.array(mutations_group["position"])
    node = np.array(mutations_group["node"], dtype=np.int32)
    unique_position, index = np.unique(position, return_index=True)
    if unique_position.shape != position.shape:
        if remove_duplicate_positions:
            position = position[index]
            node = node[index]
        else:
            # TODO add the number of duplicates so that we can improve the
            # error message.
            raise exceptions.DuplicatePositionsError()
    num_mutations = position.shape[0]
    sites.set_columns(
        position=position,
        ancestral_state=ord("0") * np.ones(num_mutations, dtype=np.int8),
        ancestral_state_length=np.ones(num_mutations, dtype=np.uint32))
    mutations.set_columns(
        node=node,
        site=np.arange(num_mutations, dtype=np.int32),
        derived_state=ord("1") * np.ones(num_mutations, dtype=np.int8),
        derived_state_length=np.ones(num_mutations, dtype=np.uint32))


def _load_legacy_hdf5_v2(root, remove_duplicate_positions):
    # Get the coalescence records
    trees_group = root["trees"]
    provenance = [
        _get_v2_provenance("generate_trees", trees_group.attrs),
    ]
    cr_node = np.array(trees_group["node"], dtype=np.int32)
    children = np.array(trees_group["children"], dtype=np.int32).flatten()
    edgesets = msprime.EdgesetTable()
    edgesets.set_columns(
        left=trees_group["left"],
        right=trees_group["right"],
        parent=cr_node,
        children=children,
        children_length=2 * np.ones(cr_node.shape, dtype=np.uint32))

    num_nodes = max(np.max(children), np.max(cr_node)) + 1
    sample_size = np.min(cr_node)
    flags = np.zeros(num_nodes, dtype=np.uint32)
    population = np.zeros(num_nodes, dtype=np.int32)
    time = np.zeros(num_nodes, dtype=np.float64)
    flags[:sample_size] = msprime.NODE_IS_SAMPLE
    cr_population = np.array(trees_group["population"], dtype=np.int32)
    cr_time = np.array(trees_group["time"])
    time[cr_node] = cr_time
    population[cr_node] = cr_population
    if "samples" in root:
        samples_group = root["samples"]
        population[:sample_size] = samples_group["population"]
        if "time" in samples_group:
            time[:sample_size] = samples_group["time"]
    nodes = msprime.NodeTable()
    nodes.set_columns(
        flags=flags, population=population, time=time)

    sites = msprime.SiteTable()
    mutations = msprime.MutationTable()
    if "mutations" in root:
        mutations_group = root["mutations"]
        _convert_hdf5_mutations(
            mutations_group, sites, mutations, remove_duplicate_positions)
        provenance.append(
            _get_v2_provenance("generate_mutations", mutations_group.attrs))
    provenance.append(_get_upgrade_provenance(root))
    return msprime.load_tables(
        nodes=nodes, edgesets=edgesets, sites=sites, mutations=mutations,
        provenance_strings=provenance)


def _load_legacy_hdf5_v3(root, remove_duplicate_positions):
    # get the trees group for the records and samples
    trees_group = root["trees"]
    nodes_group = trees_group["nodes"]
    time = np.array(nodes_group["time"])

    breakpoints = np.array(trees_group["breakpoints"])
    records_group = trees_group["records"]
    left_indexes = np.array(records_group["left"])
    right_indexes = np.array(records_group["right"])
    record_node = np.array(records_group["node"], dtype=np.int32)
    num_nodes = time.shape[0]
    sample_size = np.min(record_node)
    flags = np.zeros(num_nodes, dtype=np.uint32)
    flags[:sample_size] = msprime.NODE_IS_SAMPLE

    nodes = msprime.NodeTable()
    nodes.set_columns(
        flags=flags,
        time=nodes_group["time"],
        population=nodes_group["population"])
    edgesets = msprime.EdgesetTable()
    edgesets.set_columns(
        left=breakpoints[left_indexes],
        right=breakpoints[right_indexes],
        parent=record_node,
        children=records_group["children"],
        children_length=records_group["num_children"])
    sites = msprime.SiteTable()
    mutations = msprime.MutationTable()
    if "mutations" in root:
        _convert_hdf5_mutations(
            root["mutations"], sites, mutations, remove_duplicate_positions)
    provenance = []
    if "provenance" in root:
        provenance = list(root["provenance"])
    provenance.append(_get_upgrade_provenance(root))
    return msprime.load_tables(
        nodes=nodes, edgesets=edgesets, sites=sites, mutations=mutations,
        provenance_strings=provenance)


def load_legacy(filename, remove_duplicate_positions=False):
    """
    Reads the specified msprime HDF5 file and returns a tree sequence. This
    method is only intended to be used to read old format HDF5 files.

    If remove_duplicate_positions is True, remove all sites (except the
    first) that contain duplicate positions. If this is False, any input
    files that contain duplicate positions will raise an DuplicatePositionsError.
    """
    _check_h5py()
    loaders = {
        2: _load_legacy_hdf5_v2,
        3: _load_legacy_hdf5_v3,
    }
    root = h5py.File(filename, "r")
    if 'format_version' not in root.attrs:
        raise ValueError("HDF5 file not in msprime format")
    format_version = root.attrs['format_version']
    if format_version[0] not in loaders:
        raise ValueError("Version {} not supported for loading".format(format_version))
    try:
        ts = loaders[format_version[0]](root, remove_duplicate_positions)
    finally:
        root.close()
    return ts


def _dump_legacy_hdf5_v2(tree_sequence, root):
    root.attrs["format_version"] = (2, 999)
    root.attrs["sample_size"] = tree_sequence.get_sample_size()
    root.attrs["sequence_length"] = tree_sequence.get_sequence_length()
    left = []
    right = []
    node = []
    children = []
    time = []
    population = []
    for record in tree_sequence.records():
        left.append(record.left)
        right.append(record.right)
        node.append(record.node)
        if len(record.children) != 2:
            raise ValueError("V2 files only support binary records")
        children.append(record.children)
        time.append(record.time)
        population.append(record.population)
    l = len(time)
    trees = root.create_group("trees")
    trees.attrs["environment"] = json.dumps({"msprime_version": 0})
    trees.attrs["parameters"] = "{}"
    trees.create_dataset("left", (l, ), data=left, dtype=float)
    trees.create_dataset("right", (l, ), data=right, dtype=float)
    trees.create_dataset("time", (l, ), data=time, dtype=float)
    trees.create_dataset("node", (l, ), data=node, dtype="u4")
    trees.create_dataset("population", (l, ), data=population, dtype="u1")
    trees.create_dataset(
        "children", (l, 2), data=children, dtype="u4")
    samples = root.create_group("samples")
    population = []
    time = []
    l = tree_sequence.get_sample_size()
    for u in range(l):
        time.append(tree_sequence.get_time(u))
        population.append(tree_sequence.get_population(u))
    samples.create_dataset("time", (l, ), data=time, dtype=float)
    samples.create_dataset("population", (l, ), data=population, dtype="u1")
    if tree_sequence.get_num_mutations() > 0:
        node = []
        position = []
        for site in tree_sequence.sites():
            if len(site.mutations) != 1:
                raise ValueError("v2 does not support recurrent mutations")
            if site.ancestral_state != "0" or site.mutations[0].derived_state != "1":
                raise ValueError("v2 does not support non-binary mutations")
            position.append(site.position)
            node.append(site.mutations[0].node)
        l = len(node)
        mutations = root.create_group("mutations")
        mutations.attrs["environment"] = json.dumps({"msprime_version": 0})
        mutations.attrs["parameters"] = "{}"
        mutations.create_dataset("position", (l, ), data=position, dtype=float)
        mutations.create_dataset("node", (l, ), data=node, dtype="u4")


def _dump_legacy_hdf5_v3(tree_sequence, root):
    root.attrs["format_version"] = (3, 999)
    root.attrs["sample_size"] = 0
    root.attrs["sequence_length"] = 0
    trees = root.create_group("trees")
    # Get the breakpoints from the records.
    left = [cr.left for cr in tree_sequence.records()]
    breakpoints = np.unique(left + [tree_sequence.sequence_length])
    trees.create_dataset(
        "breakpoints", (len(breakpoints), ), data=breakpoints, dtype=float)

    left = []
    right = []
    node = []
    children = []
    num_children = []
    time = []
    for cr in tree_sequence.records():
        node.append(cr.node)
        left.append(np.searchsorted(breakpoints, cr.left))
        right.append(np.searchsorted(breakpoints, cr.right))
        children.extend(cr.children)
        num_children.append(len(cr.children))
        time.append(cr.time)
    records_group = trees.create_group("records")
    l = len(num_children)
    records_group.create_dataset("left", (l, ), data=left, dtype="u4")
    records_group.create_dataset("right", (l, ), data=right, dtype="u4")
    records_group.create_dataset("node", (l, ), data=node, dtype="u4")
    records_group.create_dataset("num_children", (l, ), data=num_children, dtype="u4")
    records_group.create_dataset(
        "children", (len(children), ), data=children, dtype="u4")

    indexes_group = trees.create_group("indexes")
    I = sorted(range(l), key=lambda j: (left[j], time[j]))
    O = sorted(range(l), key=lambda j: (right[j], -time[j]))
    indexes_group.create_dataset("insertion_order", (l, ), data=I, dtype="u4")
    indexes_group.create_dataset("removal_order", (l, ), data=O, dtype="u4")

    nodes_group = trees.create_group("nodes")
    population = np.zeros(tree_sequence.num_nodes, dtype="u4")
    time = np.zeros(tree_sequence.num_nodes, dtype=float)
    tree = next(tree_sequence.trees())
    for u in range(tree_sequence.sample_size):
        population[u] = tree.population(u)
        time[u] = tree.time(u)
    for cr in tree_sequence.records():
        population[cr.node] = cr.population
        time[cr.node] = cr.time
    l = tree_sequence.num_nodes
    nodes_group.create_dataset("time", (l, ), data=time, dtype=float)
    nodes_group.create_dataset("population", (l, ), data=population, dtype="u4")

    node = []
    position = []
    for site in tree_sequence.sites():
        if len(site.mutations) != 1:
            raise ValueError("v3 does not support recurrent mutations")
        if site.ancestral_state != "0" or site.mutations[0].derived_state != "1":
            raise ValueError("v3 does not support non-binary mutations")
        position.append(site.position)
        node.append(site.mutations[0].node)
    l = len(position)
    if l > 0:
        mutations = root.create_group("mutations")
        mutations.create_dataset("position", (l, ), data=position, dtype=float)
        mutations.create_dataset("node", (l, ), data=node, dtype="u4")

    provenance = tree_sequence.get_provenance()
    if len(provenance) > 0:
        dt = h5py.special_dtype(vlen=bytes)
        root.create_dataset("provenance", (len(provenance), ), data=provenance, dtype=dt)


def dump_legacy(tree_sequence, filename, version=3):
    """
    Writes the specified tree sequence to a HDF5 file in the specified
    legacy file format version.
    """
    _check_h5py()
    dumpers = {
        2: _dump_legacy_hdf5_v2,
        3: _dump_legacy_hdf5_v3
    }
    if version not in dumpers:
        raise ValueError("Version {} file format is supported".format(version))
    root = h5py.File(filename, "w")
    try:
        dumpers[version](tree_sequence, root)
    finally:
        root.close()
