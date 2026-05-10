"""
plan_route.py — heat-aware closed-loop route planner backed by a C Dijkstra core.

Pipeline:
  1. Load (or build) the cached CSR graph from streets_heat_z14.geojson.
  2. Snap home to nearest graph node.
  3. Run a full edge-pair Dijkstra outbound (in C).
  4. Pick a turnaround in a chosen compass octant.
  5. Build a corridor buffer around the outbound path.
  6. Run a return Dijkstra (in C) penalising outbound edges and the corridor.
  7. Concatenate, smooth, simplify, write GPX/GeoJSON/summary.

Penalties supported (all configurable from CLI):
  - alpha       : heat preference (cost = length × (1 + α (1−heat)))
  - turn-pen-m  : linear in absolute turn angle, calibrated to a 90° turn
  - signal-pen-m: per traffic-signal/stop node passed
  - inter-pen-m : per extra branch at a non-signal junction
  - corridor    : multiplier on edges sitting inside the outbound corridor buffer

Run-to-run variation: each invocation samples a compass octant weighted by best
heat in that octant, jitters alpha and the distance band, and softmax-shuffles
candidates within the chosen octant.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import random
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Tuple

import numpy as np

import route_engine as re_engine
from route_engine import (
    Graph, dijkstra, reconstruct_state_path, state_node, state_edge,
    best_state_per_node, haversine_m,
)


# --------------------------------------------------------------------- helpers


def load_signal_flag(g: Graph, signals_path: Path,
                     max_snap_m: float = 12.0,
                     cache_path: Path = None) -> np.ndarray:
    """Build a 0/1 flag per graph node marking traffic-signal/stop nodes.
    Cached to disk because graph + signals don't change between runs."""
    if cache_path is not None and cache_path.exists() and \
       signals_path.exists() and \
       cache_path.stat().st_mtime > signals_path.stat().st_mtime:
        flag = np.load(cache_path)
        if len(flag) == g.n_nodes:
            print(f"[info] {int(flag.sum()):,} signal nodes (cached)")
            return flag

    flag = np.zeros(g.n_nodes, dtype=np.int32)
    if not signals_path.exists():
        return flag

    sigs = json.loads(signals_path.read_text())
    slons = np.array([s["lon"] for s in sigs], dtype=np.float64)
    slats = np.array([s["lat"] for s in sigs], dtype=np.float64)

    # Bucket graph nodes once into a grid for O(1) lookup
    cell_deg = 0.0005
    cx = np.floor(g.node_lon / cell_deg).astype(np.int64)
    cy = np.floor(g.node_lat / cell_deg).astype(np.int64)
    # combine into a single key
    keys = cx * 100000 + cy
    sort_order = np.argsort(keys, kind="stable")
    sorted_keys = keys[sort_order]
    # boundaries of each bucket
    bucket_start = {}
    prev_k = None; prev_i = 0
    for i, k in enumerate(sorted_keys):
        if k != prev_k:
            if prev_k is not None:
                bucket_start[prev_k] = (prev_i, i)
            prev_k = int(k); prev_i = i
    bucket_start[prev_k] = (prev_i, len(sorted_keys))

    cos_lat = math.cos(math.radians(48.2))
    R2 = max_snap_m ** 2
    snapped = 0
    for i in range(len(sigs)):
        slon = slons[i]; slat = slats[i]
        gx = int(math.floor(slon / cell_deg))
        gy = int(math.floor(slat / cell_deg))
        best_idx = -1; best_d2 = R2
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                k = (gx + dx) * 100000 + (gy + dy)
                rng = bucket_start.get(k)
                if rng is None: continue
                lo, hi = rng
                idxs = sort_order[lo:hi]
                # vectorised distance for this bucket (small)
                ddx = (g.node_lon[idxs] - slon) * cos_lat * 111320.0
                ddy = (g.node_lat[idxs] - slat) * 111320.0
                d2 = ddx * ddx + ddy * ddy
                j = int(np.argmin(d2))
                if d2[j] < best_d2:
                    best_d2 = float(d2[j])
                    best_idx = int(idxs[j])
        if best_idx >= 0:
            flag[best_idx] = 1
            snapped += 1
    print(f"[info] {snapped:,} signal/stop nodes snapped")

    if cache_path is not None:
        np.save(cache_path, flag)
    return flag


def build_corridor_flag(g: Graph, out_node_indices: List[int],
                        corridor_m: float, exclude: set) -> np.ndarray:
    """Mark every graph node within corridor_m of any node in out_node_indices.
    Vectorised: for each outbound node we do a single numpy-wide distance test
    against all graph nodes and OR the result into the flag mask."""
    flag = np.zeros(g.n_nodes, dtype=np.int32)
    if corridor_m <= 0 or not out_node_indices:
        return flag

    out_idx = np.asarray(out_node_indices, dtype=np.int64)
    out_lons = g.node_lon[out_idx]
    out_lats = g.node_lat[out_idx]
    # Approximate distance: equirectangular projection scaled by cos(lat).
    cos_lat = math.cos(math.radians(48.2))
    radius2 = corridor_m * corridor_m

    in_buf = np.zeros(g.n_nodes, dtype=bool)
    # process in small batches to keep memory bounded but still vectorised.
    batch = 32
    for s in range(0, len(out_idx), batch):
        chunk_lon = out_lons[s:s + batch]   # (B,)
        chunk_lat = out_lats[s:s + batch]
        # broadcast: (B, 1) - (N,) -> (B, N) -- but that's huge for big N.
        # Instead just iterate this batch in a tight numpy loop.
        for i in range(len(chunk_lon)):
            dx = (g.node_lon - chunk_lon[i]) * cos_lat * 111320.0
            dy = (g.node_lat - chunk_lat[i]) * 111320.0
            in_buf |= (dx * dx + dy * dy) < radius2

    flag[in_buf] = 1
    if exclude:
        for k in exclude:
            flag[k] = 0
    return flag


def turn_cos_idx(g: Graph, prev_from: int, u: int, v: int) -> float:
    pf_lon, pf_lat = g.node_lon[prev_from], g.node_lat[prev_from]
    u_lon, u_lat = g.node_lon[u], g.node_lat[u]
    v_lon, v_lat = g.node_lon[v], g.node_lat[v]
    mid_lat = math.radians((pf_lat + v_lat) * 0.5)
    cl = math.cos(mid_lat)
    ix = (u_lon - pf_lon) * cl; iy = u_lat - pf_lat
    ox = (v_lon - u_lon) * cl; oy = v_lat - u_lat
    n1 = math.hypot(ix, iy) or 1e-12
    n2 = math.hypot(ox, oy) or 1e-12
    return (ix * ox + iy * oy) / (n1 * n2)


def path_total_turn_deg(g: Graph, node_seq: List[int]) -> float:
    total = 0.0
    for i in range(1, len(node_seq) - 1):
        ct = turn_cos_idx(g, node_seq[i - 1], node_seq[i], node_seq[i + 1])
        ct = max(-1.0, min(1.0, ct))
        total += math.degrees(math.acos(ct))
    return total


def loop_metrics(g: Graph, edge_ids: List[int], node_seq: List[int],
                 signal_flag: np.ndarray) -> dict:
    total_len = float(g.edge_len[edge_ids].sum()) if edge_ids else 0.0
    if total_len == 0:
        return {"length_m": 0, "heat_mean_weighted": 0,
                "named_streets": [], "signals_passed": 0,
                "turns_count": 0, "intersections_passed": 0,
                "sharp_turns": 0, "total_turn_deg": 0}
    weighted_heat = float((g.edge_heat[edge_ids] * g.edge_len[edge_ids]).sum() / total_len)

    streets = []; seen = set()
    for e in edge_ids:
        n_idx = int(g.edge_name_idx[e])
        n = g.name_table[n_idx]
        if n and n not in seen:
            seen.add(n); streets.append(n)

    signals = 0; intersections = 0; turns = 0; sharp = 0; total_angle = 0.0
    for i in range(1, len(node_seq) - 1):
        n = node_seq[i]
        if signal_flag[n]:
            signals += 1
        elif g.node_degree[n] > 2:
            intersections += 1
        ct = turn_cos_idx(g, node_seq[i - 1], n, node_seq[i + 1])
        ct = max(-1.0, min(1.0, ct))
        ang = math.degrees(math.acos(ct))
        total_angle += ang
        if ang >= 25:
            turns += 1
        if ang >= 45:
            sharp += 1

    return {
        "length_m": total_len,
        "heat_mean_weighted": weighted_heat,
        "named_streets": streets,
        "signals_passed": signals,
        "intersections_passed": intersections,
        "turns_count": turns,
        "sharp_turns": sharp,
        "total_turn_deg": round(total_angle, 1),
    }


def edges_to_geometry(g: Graph, edge_ids: List[int],
                      node_seq: List[int]) -> List[List[float]]:
    out = []
    for i, eid in enumerate(edge_ids):
        u = int(g.edge_u[eid]); v = int(g.edge_v[eid])
        # determine direction relative to node_seq[i]
        if node_seq[i] == u:
            seg_lonlat = [(g.node_lon[u], g.node_lat[u]),
                          (g.node_lon[v], g.node_lat[v])]
        else:
            seg_lonlat = [(g.node_lon[v], g.node_lat[v]),
                          (g.node_lon[u], g.node_lat[u])]
        if not out:
            out.extend([list(p) for p in seg_lonlat])
        else:
            out.append(list(seg_lonlat[1]))
    return out


def write_gpx(path: Path, coords, name: str):
    body = '<?xml version="1.0" encoding="UTF-8"?>\n'
    body += '<gpx version="1.1" creator="strava-heat-router" xmlns="http://www.topografix.com/GPX/1/1">\n'
    body += f'  <trk><name>{name}</name><trkseg>\n'
    for lon, lat in coords:
        body += f'    <trkpt lat="{lat:.7f}" lon="{lon:.7f}"></trkpt>\n'
    body += '  </trkseg></trk>\n</gpx>\n'
    path.write_text(body)


def _perp_dist_m(p, a, b):
    lat0 = math.radians((a[1] + b[1]) / 2)
    cl = math.cos(lat0)
    px = (p[0] - a[0]) * cl * 111320.0
    py = (p[1] - a[1]) * 111320.0
    bx = (b[0] - a[0]) * cl * 111320.0
    by = (b[1] - a[1]) * 111320.0
    L2 = bx * bx + by * by
    if L2 < 1e-9:
        return math.hypot(px, py)
    t = max(0.0, min(1.0, (px * bx + py * by) / L2))
    return math.hypot(px - t * bx, py - t * by)


def douglas_peucker(coords, epsilon_m: float):
    if len(coords) < 3:
        return list(coords)
    keep = [False] * len(coords)
    keep[0] = keep[-1] = True
    stack = [(0, len(coords) - 1)]
    while stack:
        i, j = stack.pop()
        if j - i < 2:
            continue
        a, b = coords[i], coords[j]
        max_d = 0.0
        max_k = -1
        for k in range(i + 1, j):
            d = _perp_dist_m(coords[k], a, b)
            if d > max_d:
                max_d = d; max_k = k
        if max_d > epsilon_m and max_k > 0:
            keep[max_k] = True
            stack.append((i, max_k))
            stack.append((max_k, j))
    return [c for c, k in zip(coords, keep) if k]


# --------------------------------------------------------------------- planner


class PlanOptions:
    """Plain options bag — works for both CLI args and HTTP requests."""
    __slots__ = ("alpha", "turn_pen_m", "signal_pen_m", "inter_pen_m",
                 "corridor_m", "corridor_mult", "simplify_m", "verbose",
                 "home_label")

    def __init__(self, *, alpha=3.0, turn_pen_m=30.0, signal_pen_m=80.0,
                 inter_pen_m=10.0, corridor_m=130.0, corridor_mult=3.5,
                 simplify_m=4.0, verbose=True, home_label=""):
        self.alpha = alpha
        self.turn_pen_m = turn_pen_m
        self.signal_pen_m = signal_pen_m
        self.inter_pen_m = inter_pen_m
        self.corridor_m = corridor_m
        self.corridor_mult = corridor_mult
        self.simplify_m = simplify_m
        self.verbose = verbose
        self.home_label = home_label

    @classmethod
    def from_argparse(cls, args):
        return cls(
            alpha=args.alpha, turn_pen_m=args.turn_pen_m,
            signal_pen_m=args.signal_pen_m, inter_pen_m=args.inter_pen_m,
            corridor_m=args.corridor_m, corridor_mult=args.corridor_mult,
            simplify_m=args.simplify_m, verbose=True,
            home_label=args.home_label,
        )


def plan_loop(g: Graph, signal_flag: np.ndarray, home_lonlat,
              target_km: float, opts: PlanOptions, rng) -> dict:
    """The real planning function. Pre-loaded graph + signal_flag.
    Returns a dict with the loop, geometry, metrics. Does no I/O.
    """
    log = print if opts.verbose else (lambda *a, **k: None)

    home_node = g.snap_lonlat(*home_lonlat)
    snap_d = haversine_m(home_lonlat, (g.node_lon[home_node], g.node_lat[home_node]))
    log(f"[info] home snap offset: {snap_d:.1f} m")

    alpha = opts.alpha * rng.uniform(0.8, 1.3)
    target_m = target_km * 1000.0
    half = target_m / 2.0
    band_lo = half - rng.uniform(150.0, 350.0)
    band_hi = half + rng.uniform(250.0, 500.0)
    log(f"[info] target={target_km} km  alpha={alpha:.2f}  "
        f"band={band_lo:.0f}-{band_hi:.0f} m")

    # Outbound full Dijkstra
    t0 = time.time()
    out_res = dijkstra(g, home_node, alpha=alpha,
                       turn_pen_m=opts.turn_pen_m,
                       signal_pen_m=opts.signal_pen_m,
                       inter_pen_m=opts.inter_pen_m,
                       signal_flag=signal_flag)
    log(f"[info] outbound C-Dijkstra: {time.time()-t0:.2f}s, "
        f"relaxed {out_res.relaxed:,}")

    best_state_arr, _ = best_state_per_node(g, out_res.dist, home_node)

    # Candidate turnarounds in distance band
    real = out_res.real
    valid = best_state_arr >= 0
    states = best_state_arr.copy()
    states[~valid] = 0
    rds = real[states]
    in_band = valid & (rds >= band_lo) & (rds <= band_hi)
    cand_nodes = np.nonzero(in_band)[0]
    if cand_nodes.size == 0:
        raise RuntimeError("No candidate turnaround points in the distance band; "
                           "try a different target distance or relax penalties.")

    cand_states = best_state_arr[cand_nodes]
    cand_real = out_res.real[cand_states]
    heat_w = out_res.heat_w[cand_states]
    heat_l = out_res.heat_len[cand_states]
    cand_heat = np.where(heat_l > 0, heat_w / np.maximum(heat_l, 1e-9), 0.0)

    # Octant binning
    home_lon = g.node_lon[home_node]; home_lat = g.node_lat[home_node]
    cos_lat = math.cos(math.radians(home_lat))
    nlon = g.node_lon[cand_nodes]; nlat = g.node_lat[cand_nodes]
    bearings = np.arctan2((nlon - home_lon) * cos_lat, nlat - home_lat)
    oct_idx = (((bearings + math.pi) / (2 * math.pi)) * 8).astype(np.int32) % 8

    candidates_by_oct = defaultdict(list)
    for i in range(cand_nodes.size):
        candidates_by_oct[int(oct_idx[i])].append(
            (float(cand_heat[i]), float(cand_real[i]), int(cand_nodes[i]),
             int(cand_states[i]))
        )
    for oc in candidates_by_oct:
        candidates_by_oct[oc].sort(
            key=lambda t: -(t[0] - 0.4 * abs(t[1] - half) / max(half, 1)))

    OCT_NAMES = {0: "S", 1: "SW", 2: "W", 3: "NW", 4: "N", 5: "NE", 6: "E", 7: "SE"}
    octants = sorted(candidates_by_oct.keys())
    oct_weights = [max(0.05, max(t[0] for t in candidates_by_oct[oc])) ** 2
                   for oc in octants]
    chosen_oct = rng.choices(octants, weights=oct_weights, k=1)[0]
    direction_name = OCT_NAMES.get(chosen_oct, str(chosen_oct))
    log(f"[info] direction: {direction_name} "
        f"(best heat: {candidates_by_oct[chosen_oct][0][0]:.3f})")

    pool = candidates_by_oct[chosen_oct][:30]
    if len(pool) < 8:
        for oc in sorted(octants,
                         key=lambda x: -max(t[0] for t in candidates_by_oct[x])):
            if oc != chosen_oct:
                pool.extend(candidates_by_oct[oc][:10])
                if len(pool) >= 12:
                    break

    weights = [math.exp((t[0] - 0.5 * abs(t[1] - half) / max(half, 1)) * 3)
               for t in pool]
    iter_order = []
    pc = list(pool); wc = list(weights)
    while pc:
        idx = rng.choices(range(len(pc)), weights=wc, k=1)[0]
        iter_order.append(pc.pop(idx)); wc.pop(idx)

    best_loop = None
    return_iters = 0
    return_t = 0.0
    for cand in iter_order[:20]:
        mh, rd, mid_node, mid_state = cand
        rec_o = reconstruct_state_path(out_res.prev, mid_state, g, home_node)
        if rec_o is None:
            continue
        out_nodes, out_edges = rec_o

        exclude_list = [home_node]
        s = g.adj_off[home_node]; e = g.adj_off[home_node + 1]
        for k in range(s, e):
            exclude_list.append(int(g.adj_to[k]))
        corridor = re_engine.corridor_mark(
            g,
            np.asarray(out_nodes, dtype=np.int32),
            np.asarray(exclude_list, dtype=np.int32),
            opts.corridor_m,
        )

        penalised = np.zeros(g.n_edges, dtype=np.int32)
        for e_id in out_edges:
            penalised[e_id] = 1

        t0 = time.time()
        ret_res = dijkstra(g, mid_node,
                           target_node=home_node,
                           alpha=alpha,
                           turn_pen_m=opts.turn_pen_m,
                           signal_pen_m=opts.signal_pen_m,
                           inter_pen_m=opts.inter_pen_m,
                           signal_flag=signal_flag,
                           penalised_edge_flag=penalised,
                           corridor_flag=corridor,
                           corridor_mult=opts.corridor_mult)
        return_iters += 1
        return_t += time.time() - t0

        ret_state = ret_res.target_state
        if ret_state < 0:
            continue
        rec_b = reconstruct_state_path(ret_res.prev, ret_state, g, mid_node)
        if rec_b is None:
            continue
        back_nodes, back_edges = rec_b

        loop_edges = list(out_edges) + list(back_edges)
        loop_nodes = list(out_nodes) + list(back_nodes[1:])
        m = loop_metrics(g, loop_edges, loop_nodes, signal_flag)

        dist_pen = abs(m["length_m"] - target_m) / target_m
        composite = (
            m["heat_mean_weighted"]
            - 0.6 * dist_pen
            + rng.uniform(-0.08, 0.08)
        )
        score = (composite, m["heat_mean_weighted"], -abs(m["length_m"] - target_m))
        if best_loop is None or score > best_loop["score"]:
            best_loop = {
                "score": score, "edges": loop_edges, "nodes": loop_nodes,
                "out_edges": out_edges, "out_nodes": out_nodes,
                "back_edges": back_edges, "back_nodes": back_nodes,
                "metrics": m,
                "out_len": float(g.edge_len[out_edges].sum()),
                "back_len": float(g.edge_len[back_edges].sum()),
            }

    log(f"[info] {return_iters} return Dijkstras in {return_t:.2f}s "
        f"({return_t/max(1,return_iters)*1000:.0f} ms each)")

    if best_loop is None:
        raise RuntimeError("Could not assemble a closed loop "
                           "(try a different distance or seed).")

    # Build geometry + simplified polyline.
    coords_full = edges_to_geometry(g, best_loop["edges"], best_loop["nodes"])
    if opts.simplify_m > 0:
        coords = douglas_peucker(coords_full, opts.simplify_m)
    else:
        coords = coords_full

    return {
        "home_node": home_node,
        "home_lonlat": home_lonlat,
        "alpha": alpha,
        "direction": direction_name,
        "coords": coords,
        "coords_full": coords_full,
        "best_loop": best_loop,
    }


def make_geojson(result: dict, opts: PlanOptions) -> dict:
    bl = result["best_loop"]
    m = bl["metrics"]
    return {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": result["coords"]},
            "properties": {
                "length_m": m["length_m"],
                "heat_mean_weighted": m["heat_mean_weighted"],
                "named_streets": m["named_streets"],
                "signals_passed": m["signals_passed"],
                "intersections_passed": m["intersections_passed"],
                "turns_count": m["turns_count"],
                "sharp_turns": m["sharp_turns"],
                "total_turn_deg": m["total_turn_deg"],
                "direction": result.get("direction"),
                "home": opts.home_label,
            }
        }]
    }


def make_gpx(coords, name: str) -> str:
    body = '<?xml version="1.0" encoding="UTF-8"?>\n'
    body += '<gpx version="1.1" creator="strava-heat-router" xmlns="http://www.topografix.com/GPX/1/1">\n'
    body += f'  <trk><name>{name}</name><trkseg>\n'
    for lon, lat in coords:
        body += f'    <trkpt lat="{lat:.7f}" lon="{lon:.7f}"></trkpt>\n'
    body += '  </trkseg></trk>\n</gpx>\n'
    return body


def write_outputs(out_prefix: str, result: dict, target_km: float,
                  seed: int, opts: PlanOptions):
    bl = result["best_loop"]
    m = bl["metrics"]
    coords = result["coords"]

    print(f"[ok] {m['length_m']/1000:.2f} km   "
          f"heat={m['heat_mean_weighted']:.3f}   "
          f"signals={m['signals_passed']}   "
          f"junctions={m['intersections_passed']}   "
          f"turns≥25°={m['turns_count']}   sharp≥45°={m['sharp_turns']}   "
          f"total_turn={m['total_turn_deg']:.0f}°")
    print("[ok] streets:")
    for n in m["named_streets"][:25]:
        print(f"      - {n}")

    out_geo = Path(f"{out_prefix}.geojson")
    out_geo.write_text(json.dumps(make_geojson(result, opts)))
    Path(f"{out_prefix}.gpx").write_text(make_gpx(coords, f"{target_km}km hot loop"))
    Path(f"{out_prefix}_summary.txt").write_text(
        f"Home: {opts.home_label}\n"
        f"Seed: {seed}  (pass --seed {seed} to reproduce)\n"
        f"Target: {target_km:.1f} km    Alpha: {result['alpha']:.2f}\n"
        f"Turn-pen: {opts.turn_pen_m}m   Signal-pen: {opts.signal_pen_m}m   "
        f"Inter-pen: {opts.inter_pen_m}m   Corridor: {opts.corridor_m}m × {opts.corridor_mult}\n"
        f"Actual: {m['length_m']/1000:.2f} km "
        f"(out {bl['out_len']/1000:.2f}, back {bl['back_len']/1000:.2f})\n"
        f"Heat (length-weighted): {m['heat_mean_weighted']:.3f}\n"
        f"Stops: {m['signals_passed']} signals, {m['intersections_passed']} junctions\n"
        f"Turns: {m['turns_count']} ≥25°, {m['sharp_turns']} ≥45°, "
        f"total angular sweep {m['total_turn_deg']:.0f}°\n"
        f"Streets in order:\n"
        + "\n".join(f"  - {n}" for n in m["named_streets"])
    )
    print(f"[done] -> {out_geo}, {out_prefix}.gpx, {out_prefix}_summary.txt")


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--streets", default="streets_heat_z14.geojson")
    p.add_argument("--graph-cache", default="streets_graph.npz")
    p.add_argument("--signals", default="traffic_signals.json")
    p.add_argument("--home-lon", type=float, default=16.3708773)
    p.add_argument("--home-lat", type=float, default=48.1854269)
    p.add_argument("--home-label",
                   default="Wiedner Gürtel 62, 1040 Wien (Microservice GmbH block)")
    p.add_argument("--target-km", type=float, default=4.0)
    p.add_argument("--alpha", type=float, default=3.0)
    p.add_argument("--turn-pen-m", type=float, default=30.0)
    p.add_argument("--signal-pen-m", type=float, default=80.0)
    p.add_argument("--inter-pen-m", type=float, default=10.0)
    p.add_argument("--corridor-m", type=float, default=130.0)
    p.add_argument("--corridor-mult", type=float, default=3.5)
    p.add_argument("--simplify-m", type=float, default=4.0)
    p.add_argument("--seed", type=int, default=None)
    p.add_argument("--out-prefix", default="route")
    args = p.parse_args()

    seed = args.seed if args.seed is not None else int(time.time() * 1000) & 0xFFFFFFFF
    print(f"[info] seed={seed}")
    rng = random.Random(seed)

    t_total = time.time()
    g = Graph.from_geojson(Path(args.streets), Path(args.graph_cache))
    signal_flag = load_signal_flag(g, Path(args.signals),
                                   cache_path=Path("signal_flag.npy"))

    opts = PlanOptions.from_argparse(args)
    result = plan_loop(g, signal_flag,
                       (args.home_lon, args.home_lat),
                       args.target_km, opts, rng)
    write_outputs(args.out_prefix, result, args.target_km, seed, opts)
    print(f"[time] total: {time.time()-t_total:.2f}s")


if __name__ == "__main__":
    main()
