"""
route_engine.py — fast graph + edge-pair Dijkstra backed by a C extension.

Loads the streets GeoJSON once, builds a CSR-format adjacency, and caches it as
a single .npz so subsequent runs skip the multi-second parse.

The hot inner loop (edge-pair Dijkstra with all penalties) is implemented in
dijkstra_core.dylib and called via ctypes.
"""

from __future__ import annotations

import ctypes
import json
import math
import os
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np

# ----- C library loading -------------------------------------------------------

def _find_lib() -> Path:
    here = Path(__file__).parent
    candidates = [
        here / "dijkstra_core.so",      # Linux
        here / "dijkstra_core.dylib",   # macOS
        here / "dijkstra_core.dll",     # Windows (untested)
    ]
    for p in candidates:
        if p.exists():
            return p
    raise RuntimeError(
        "missing dijkstra_core shared library. Build it with `make` "
        "(uses gcc on Linux / clang on macOS)."
    )

_LIB_PATH = _find_lib()
_lib = ctypes.CDLL(str(_LIB_PATH))

_int_p = ctypes.POINTER(ctypes.c_int)
_dbl_p = ctypes.POINTER(ctypes.c_double)

_lib.corridor_mark.argtypes = [
    ctypes.c_int,                      # n_nodes
    _dbl_p, _dbl_p,                    # node_lon, node_lat
    _int_p, ctypes.c_int,              # out_indices, n_out
    _int_p, ctypes.c_int,              # exclude_indices, n_exclude
    ctypes.c_double, ctypes.c_double,  # radius_m, cos_lat0
    _int_p,                            # out_flag
]
_lib.corridor_mark.restype = None


def corridor_mark(g: "Graph", out_indices: np.ndarray,
                  exclude_indices: np.ndarray,
                  radius_m: float) -> np.ndarray:
    flag = np.zeros(g.n_nodes, dtype=np.int32)
    out_indices = np.asarray(out_indices, dtype=np.int32)
    exclude_indices = np.asarray(exclude_indices, dtype=np.int32)
    cos_lat0 = math.cos(math.radians(float(g.node_lat.mean())))
    _lib.corridor_mark(
        g.n_nodes,
        _dp(g.node_lon), _dp(g.node_lat),
        _ip(out_indices), len(out_indices),
        _ip(exclude_indices), len(exclude_indices),
        float(radius_m), float(cos_lat0),
        _ip(flag),
    )
    return flag


_lib.dijkstra_run.argtypes = [
    ctypes.c_int, ctypes.c_int,                       # n_nodes, n_edges
    _int_p, _int_p,                                    # edge_u, edge_v
    _dbl_p, _dbl_p,                                    # edge_len, edge_heat
    _dbl_p, _dbl_p,                                    # node_lon, node_lat
    _int_p, _int_p, _int_p,                            # adj_off, adj_edge, adj_to
    _int_p, _int_p,                                    # signal_flag, node_degree
    ctypes.c_int, ctypes.c_int,                       # start, target
    ctypes.c_double, ctypes.c_double,                 # alpha, turn_pen
    ctypes.c_double, ctypes.c_double,                 # signal_pen, inter_pen
    _int_p,                                            # penalised_edge_flag (or NULL)
    _int_p,                                            # corridor_flag (or NULL)
    ctypes.c_double, ctypes.c_double,                 # corridor_mult, max_cost
    _dbl_p, _int_p,                                    # out_dist, out_prev
    _dbl_p, _dbl_p, _dbl_p,                            # out_real, out_heat_w, out_heat_len
    _int_p,                                            # out_target_state
]
_lib.dijkstra_run.restype = ctypes.c_int


def _ip(arr):
    return arr.ctypes.data_as(_int_p) if arr is not None else None


def _dp(arr):
    return arr.ctypes.data_as(_dbl_p) if arr is not None else None


# ----- Graph data structure ---------------------------------------------------

class Graph:
    """CSR adjacency graph. Nodes are integer indices; original (lon, lat) tuple
    keys are kept for round-tripping back to lat/lon when needed."""

    __slots__ = (
        "n_nodes", "n_edges",
        "edge_u", "edge_v", "edge_len", "edge_heat",
        "edge_name_idx", "edge_highway_idx", "name_table", "highway_table",
        "node_lon", "node_lat", "node_key",
        "adj_off", "adj_edge", "adj_to",
        "node_degree",
        "key_to_idx",
    )

    def __init__(self):
        self.n_nodes = 0
        self.n_edges = 0
        self.edge_u = None; self.edge_v = None
        self.edge_len = None; self.edge_heat = None
        self.edge_name_idx = None; self.edge_highway_idx = None
        self.name_table = []; self.highway_table = []
        self.node_lon = None; self.node_lat = None; self.node_key = None
        self.adj_off = None; self.adj_edge = None; self.adj_to = None
        self.node_degree = None
        self.key_to_idx = {}

    # -------- build / cache --------

    @classmethod
    def from_geojson(cls, geojson_path: Path, cache_path: Path = None,
                     verbose: bool = True) -> "Graph":
        # Cache-first: if the cache exists and the source GeoJSON is missing
        # or older, load straight from cache.
        if cache_path and cache_path.exists():
            source_present = geojson_path.exists()
            if not source_present or cache_path.stat().st_mtime >= geojson_path.stat().st_mtime:
                if verbose:
                    print(f"[graph] loading cache: {cache_path}")
                return cls._load_cache(cache_path)

        if not geojson_path.exists():
            raise FileNotFoundError(
                f"Neither cache ({cache_path}) nor source GeoJSON "
                f"({geojson_path}) found. Run heat_streets.py to build one."
            )

        if verbose:
            print(f"[graph] parsing {geojson_path} ...")
        g = cls._build_from_geojson(geojson_path, verbose=verbose)

        if cache_path:
            if verbose:
                print(f"[graph] saving cache: {cache_path}")
            g._save_cache(cache_path)
        return g

    @classmethod
    def _build_from_geojson(cls, geojson_path: Path, verbose: bool = True) -> "Graph":
        data = json.loads(geojson_path.read_text())
        feats = data["features"]
        if verbose:
            print(f"[graph] {len(feats):,} features; building edges ...")

        # First pass: collect edges
        edges_u: List[int] = []
        edges_v: List[int] = []
        edges_len: List[float] = []
        edges_heat: List[float] = []
        edges_name_idx: List[int] = []
        edges_highway_idx: List[int] = []
        name_to_idx: Dict[str, int] = {"": 0}
        highway_to_idx: Dict[str, int] = {"": 0}
        name_table = [""]
        highway_table = [""]

        node_lon: List[float] = []
        node_lat: List[float] = []
        node_keys: List[Tuple[int, int]] = []
        key_to_idx: Dict[Tuple[int, int], int] = {}

        def _node_key(lon, lat):
            return (round(lon * 1e6), round(lat * 1e6))

        def _intern_node(lon, lat):
            k = _node_key(lon, lat)
            idx = key_to_idx.get(k)
            if idx is None:
                idx = len(node_keys)
                key_to_idx[k] = idx
                node_keys.append(k)
                node_lon.append(lon)
                node_lat.append(lat)
            return idx

        for f in feats:
            coords = f["geometry"]["coordinates"]
            if len(coords) < 2:
                continue
            props = f["properties"]
            heat = float(props.get("heat_mean", 0.0) or 0.0)
            name = (props.get("name") or "")
            highway = (props.get("highway") or "")
            n_idx = name_to_idx.get(name)
            if n_idx is None:
                n_idx = len(name_table)
                name_to_idx[name] = n_idx
                name_table.append(name)
            h_idx = highway_to_idx.get(highway)
            if h_idx is None:
                h_idx = len(highway_table)
                highway_to_idx[highway] = h_idx
                highway_table.append(highway)

            for i in range(len(coords) - 1):
                a = coords[i]; b = coords[i + 1]
                if a == b:
                    continue
                u_idx = _intern_node(a[0], a[1])
                v_idx = _intern_node(b[0], b[1])
                if u_idx == v_idx:
                    continue
                # haversine length
                R = 6371008.8
                lat1 = math.radians(a[1]); lat2 = math.radians(b[1])
                dlat = lat2 - lat1
                dlon = math.radians(b[0] - a[0])
                hh = (math.sin(dlat / 2) ** 2
                      + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2)
                length = 2 * R * math.asin(math.sqrt(hh))
                if length < 0.5:
                    continue
                edges_u.append(u_idx)
                edges_v.append(v_idx)
                edges_len.append(length)
                edges_heat.append(heat)
                edges_name_idx.append(n_idx)
                edges_highway_idx.append(h_idx)

        n_nodes = len(node_keys)
        n_edges = len(edges_u)
        if verbose:
            print(f"[graph] {n_nodes:,} nodes, {n_edges:,} edges; building CSR ...")

        # Build CSR adjacency
        adj_count = np.zeros(n_nodes + 1, dtype=np.int32)
        for u, v in zip(edges_u, edges_v):
            adj_count[u + 1] += 1
            adj_count[v + 1] += 1
        adj_off = np.cumsum(adj_count).astype(np.int32)
        total_adj = int(adj_off[-1])
        adj_edge = np.empty(total_adj, dtype=np.int32)
        adj_to = np.empty(total_adj, dtype=np.int32)
        cursor = adj_off[:-1].copy()
        for eid in range(n_edges):
            u = edges_u[eid]; v = edges_v[eid]
            adj_edge[cursor[u]] = eid; adj_to[cursor[u]] = v; cursor[u] += 1
            adj_edge[cursor[v]] = eid; adj_to[cursor[v]] = u; cursor[v] += 1
        node_degree = np.diff(adj_off).astype(np.int32)

        g = cls()
        g.n_nodes = n_nodes
        g.n_edges = n_edges
        g.edge_u = np.asarray(edges_u, dtype=np.int32)
        g.edge_v = np.asarray(edges_v, dtype=np.int32)
        g.edge_len = np.asarray(edges_len, dtype=np.float64)
        g.edge_heat = np.asarray(edges_heat, dtype=np.float64)
        g.edge_name_idx = np.asarray(edges_name_idx, dtype=np.int32)
        g.edge_highway_idx = np.asarray(edges_highway_idx, dtype=np.int32)
        g.name_table = name_table
        g.highway_table = highway_table
        g.node_lon = np.asarray(node_lon, dtype=np.float64)
        g.node_lat = np.asarray(node_lat, dtype=np.float64)
        # node_key is a list of (lon_micro, lat_micro) tuples, only kept for compat.
        g.node_key = node_keys
        g.adj_off = adj_off
        g.adj_edge = adj_edge
        g.adj_to = adj_to
        g.node_degree = node_degree
        g.key_to_idx = key_to_idx
        return g

    def _save_cache(self, path: Path):
        np.savez_compressed(
            path,
            edge_u=self.edge_u, edge_v=self.edge_v,
            edge_len=self.edge_len, edge_heat=self.edge_heat,
            edge_name_idx=self.edge_name_idx,
            edge_highway_idx=self.edge_highway_idx,
            name_table=np.array(self.name_table, dtype=object),
            highway_table=np.array(self.highway_table, dtype=object),
            node_lon=self.node_lon, node_lat=self.node_lat,
            adj_off=self.adj_off, adj_edge=self.adj_edge, adj_to=self.adj_to,
            node_degree=self.node_degree,
        )

    @classmethod
    def _load_cache(cls, path: Path) -> "Graph":
        z = np.load(path, allow_pickle=True)
        g = cls()
        g.edge_u = z["edge_u"].astype(np.int32, copy=False)
        g.edge_v = z["edge_v"].astype(np.int32, copy=False)
        g.edge_len = z["edge_len"].astype(np.float64, copy=False)
        g.edge_heat = z["edge_heat"].astype(np.float64, copy=False)
        g.edge_name_idx = z["edge_name_idx"].astype(np.int32, copy=False)
        g.edge_highway_idx = z["edge_highway_idx"].astype(np.int32, copy=False)
        g.name_table = list(z["name_table"])
        g.highway_table = list(z["highway_table"])
        g.node_lon = z["node_lon"].astype(np.float64, copy=False)
        g.node_lat = z["node_lat"].astype(np.float64, copy=False)
        g.adj_off = z["adj_off"].astype(np.int32, copy=False)
        g.adj_edge = z["adj_edge"].astype(np.int32, copy=False)
        g.adj_to = z["adj_to"].astype(np.int32, copy=False)
        g.node_degree = z["node_degree"].astype(np.int32, copy=False)
        g.n_nodes = len(g.node_lon)
        g.n_edges = len(g.edge_u)
        # rebuild key_to_idx
        keys = list(zip((g.node_lon * 1e6).round().astype(np.int64),
                        (g.node_lat * 1e6).round().astype(np.int64)))
        g.node_key = [(int(a), int(b)) for a, b in keys]
        g.key_to_idx = {k: i for i, k in enumerate(g.node_key)}
        return g

    # -------- helpers --------

    def snap_lonlat(self, lon: float, lat: float) -> int:
        """Find the index of the closest graph node to (lon, lat)."""
        dlon = (self.node_lon - lon)
        dlat = (self.node_lat - lat)
        sd = dlon * dlon + dlat * dlat
        return int(np.argmin(sd))

    def haversine_m(self, i: int, j: int) -> float:
        return haversine_m(
            (self.node_lon[i], self.node_lat[i]),
            (self.node_lon[j], self.node_lat[j]),
        )


def haversine_m(a, b) -> float:
    R = 6371008.8
    lat1 = math.radians(a[1]); lat2 = math.radians(b[1])
    dlat = lat2 - lat1
    dlon = math.radians(b[0] - a[0])
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * R * math.asin(math.sqrt(h))


# ----- C Dijkstra wrapper -----------------------------------------------------

class DijkstraResult:
    __slots__ = ("dist", "prev", "real", "heat_w", "heat_len",
                 "target_state", "relaxed")
    def __init__(self, dist, prev, real, heat_w, heat_len, target_state, relaxed):
        self.dist = dist
        self.prev = prev
        self.real = real
        self.heat_w = heat_w
        self.heat_len = heat_len
        self.target_state = target_state
        self.relaxed = relaxed


def dijkstra(g: Graph, start_node: int, *,
             target_node: int = -1,
             alpha: float = 3.0,
             turn_pen_m: float = 30.0,
             signal_pen_m: float = 80.0,
             inter_pen_m: float = 10.0,
             signal_flag: np.ndarray = None,
             penalised_edge_flag: np.ndarray = None,
             corridor_flag: np.ndarray = None,
             corridor_mult: float = 1.0,
             max_cost: float = math.inf) -> DijkstraResult:
    n_states = 2 * g.n_edges + 1
    out_dist = np.empty(n_states, dtype=np.float64)
    out_prev = np.empty(n_states, dtype=np.int32)
    out_real = np.empty(n_states, dtype=np.float64)
    out_heat_w = np.empty(n_states, dtype=np.float64)
    out_heat_len = np.empty(n_states, dtype=np.float64)
    out_target = np.array([-1], dtype=np.int32)

    if signal_flag is None:
        signal_flag = np.zeros(g.n_nodes, dtype=np.int32)

    relaxed = _lib.dijkstra_run(
        g.n_nodes, g.n_edges,
        _ip(g.edge_u), _ip(g.edge_v),
        _dp(g.edge_len), _dp(g.edge_heat),
        _dp(g.node_lon), _dp(g.node_lat),
        _ip(g.adj_off), _ip(g.adj_edge), _ip(g.adj_to),
        _ip(signal_flag.astype(np.int32, copy=False)),
        _ip(g.node_degree),
        int(start_node), int(target_node),
        float(alpha), float(turn_pen_m), float(signal_pen_m), float(inter_pen_m),
        _ip(penalised_edge_flag) if penalised_edge_flag is not None else None,
        _ip(corridor_flag) if corridor_flag is not None else None,
        float(corridor_mult),
        float(max_cost) if math.isfinite(max_cost) else 1e308,
        _dp(out_dist), _ip(out_prev),
        _dp(out_real), _dp(out_heat_w), _dp(out_heat_len),
        _ip(out_target),
    )
    return DijkstraResult(
        dist=out_dist, prev=out_prev,
        real=out_real, heat_w=out_heat_w, heat_len=out_heat_len,
        target_state=int(out_target[0]),
        relaxed=relaxed,
    )


def state_node(state: int, g: Graph, start_node: int) -> int:
    if state == 0:
        return start_node
    eid = (state - 1) >> 1
    direction = (state - 1) & 1
    return int(g.edge_v[eid] if direction == 0 else g.edge_u[eid])


def state_edge(state: int) -> int:
    return -1 if state == 0 else ((state - 1) >> 1)


def reconstruct_state_path(prev: np.ndarray, end_state: int, g: Graph,
                           start_node: int) -> Tuple[List[int], List[int]]:
    """Return (node_seq, edge_seq) from start_node to end_state."""
    if end_state == 0:
        return [start_node], []
    nodes = []
    edges_used = []
    s = end_state
    while s != 0 and s != -1:
        nodes.append(state_node(s, g, start_node))
        edges_used.append(state_edge(s))
        s = int(prev[s])
        if s == -1:
            return None
    nodes.append(start_node)
    nodes.reverse()
    edges_used.reverse()
    return nodes, edges_used


def best_state_per_node(g: Graph, dist: np.ndarray, start_node: int):
    """Return (best_state, best_cost) arrays indexed by node.
    Fully vectorised — no Python loop over states."""
    finite = np.isfinite(dist)
    state_ids = np.nonzero(finite)[0]
    if state_ids.size == 0:
        return (np.full(g.n_nodes, -1, dtype=np.int64),
                np.full(g.n_nodes, math.inf, dtype=np.float64))

    # node_of_state, vectorised
    e = (state_ids - 1) >> 1
    e_safe = np.maximum(e, 0)               # state 0 will be overwritten below
    d = (state_ids - 1) & 1
    node_of_state = np.where(d == 0, g.edge_v[e_safe], g.edge_u[e_safe]).astype(np.int64)
    node_of_state[state_ids == 0] = start_node
    cost_vals = dist[state_ids]

    # lexsort: primary key node, secondary key cost ascending. The first row in
    # each node-group is therefore the best state for that node.
    order = np.lexsort((cost_vals, node_of_state))
    sorted_nodes = node_of_state[order]
    sorted_states = state_ids[order]
    sorted_costs = cost_vals[order]

    is_first = np.empty(sorted_nodes.shape, dtype=bool)
    is_first[0] = True
    is_first[1:] = sorted_nodes[1:] != sorted_nodes[:-1]

    nodes_first = sorted_nodes[is_first]
    states_first = sorted_states[is_first]
    costs_first = sorted_costs[is_first]

    best_state = np.full(g.n_nodes, -1, dtype=np.int64)
    best_cost = np.full(g.n_nodes, math.inf, dtype=np.float64)
    best_state[nodes_first] = states_first
    best_cost[nodes_first] = costs_first
    return best_state, best_cost
