"""
Map a Strava heatmap image onto an OSM street network.

Inputs:
  - heatmap PNG produced by strava_heatmap.py
  - heatmap JSON sidecar (geo_bounds, zoom, image_size, tile_range)
  - streets GeoJSON produced by fetch_streets.py

Output:
  - streets_heat.geojson : same streets with `heat_mean`, `heat_max`, `heat_p95`,
    `samples`, `length_m` properties.
  - streets_heat.csv     : flat CSV for analysis
  - streets_heat_top.txt : human-readable preview of hottest streets

The "hot" Strava colormap is decoded by walking the channels:
  phase 1 (0 .. 1/3): R rises 0->255
  phase 2 (1/3 .. 2/3): G rises 0->255 (R=255)
  phase 3 (2/3 .. 1):   B rises 0->255 (R=255, G=255)
The alpha channel is used as a presence mask.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from pathlib import Path
from typing import Tuple

import numpy as np
from PIL import Image


def lonlat_to_world_pixel(lon: float, lat: float, zoom: int, tile_size: int = 256) -> Tuple[float, float]:
    """Web Mercator pixel coordinate at the given zoom."""
    n = 2 ** zoom
    x = (lon + 180.0) / 360.0 * tile_size * n
    lat_rad = math.radians(max(min(lat, 85.05112878), -85.05112878))
    y = (1.0 - math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi) / 2.0 * tile_size * n
    return x, y


def decode_hot(rgba: np.ndarray) -> np.ndarray:
    """
    Convert an (..., 4) uint8 array using Strava's 'hot' colormap into a [0,1] intensity.
    """
    rgba = rgba.astype(np.float32)
    r, g, b, a = rgba[..., 0], rgba[..., 1], rgba[..., 2], rgba[..., 3]

    intensity = np.zeros_like(r)
    # phase 1: black -> red
    p1 = r < 255
    intensity[p1] = r[p1] / 255.0 / 3.0
    # phase 2: red -> yellow (R=255, G ramps)
    p2 = (r >= 255) & (g < 255)
    intensity[p2] = (1.0 + g[p2] / 255.0) / 3.0
    # phase 3: yellow -> white (R=255, G=255, B ramps)
    p3 = (r >= 255) & (g >= 255)
    intensity[p3] = (2.0 + b[p3] / 255.0) / 3.0

    # mask by alpha — fully transparent = no data
    intensity *= (a / 255.0)
    return np.clip(intensity, 0.0, 1.0)


def sample_polyline(coords: list, heat: np.ndarray, zoom: int,
                    origin_px: Tuple[float, float], step_m: float = 8.0,
                    tile_size: int = 256) -> np.ndarray:
    H, W = heat.shape[:2]
    ox, oy = origin_px

    samples = []
    for i in range(len(coords) - 1):
        lon1, lat1 = coords[i]
        lon2, lat2 = coords[i + 1]
        mean_lat = math.radians((lat1 + lat2) / 2)
        dlat_m = (lat2 - lat1) * 111320.0
        dlon_m = (lon2 - lon1) * 111320.0 * math.cos(mean_lat)
        seg_m = math.hypot(dlat_m, dlon_m)

        n_samples = max(1, int(seg_m / step_m))
        for k in range(n_samples + 1):
            t = k / max(1, n_samples)
            lon = lon1 + t * (lon2 - lon1)
            lat = lat1 + t * (lat2 - lat1)
            wx, wy = lonlat_to_world_pixel(lon, lat, zoom, tile_size)
            px = int(round(wx - ox))
            py = int(round(wy - oy))
            if 0 <= px < W and 0 <= py < H:
                samples.append(heat[py, px])

    if not samples:
        return np.zeros(0, dtype=np.float32)
    return np.asarray(samples, dtype=np.float32)


def polyline_length_m(coords: list) -> float:
    total = 0.0
    for i in range(len(coords) - 1):
        lon1, lat1 = coords[i]
        lon2, lat2 = coords[i + 1]
        mean_lat = math.radians((lat1 + lat2) / 2)
        total += math.hypot((lat2 - lat1) * 111320.0,
                            (lon2 - lon1) * 111320.0 * math.cos(mean_lat))
    return total


def main():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--image", required=True, help="Heatmap PNG (e.g., heatmap_vienna_run_z14.png)")
    p.add_argument("--meta", required=True, help="Heatmap JSON sidecar")
    p.add_argument("--streets", default="streets_vienna.geojson")
    p.add_argument("--out-prefix", default="streets_heat")
    p.add_argument("--step-m", type=float, default=None,
                   help="Sampling step in meters along streets. Default: ~ pixel size at zoom.")
    p.add_argument("--min-samples", type=int, default=2,
                   help="Drop streets with fewer than this many in-bounds samples.")
    args = p.parse_args()

    meta = json.loads(Path(args.meta).read_text())
    zoom = meta["zoom"]
    tile_size = meta.get("tile_size", 256)
    tr = meta["tile_range"]
    origin_px = (tr["x_min"] * tile_size, tr["y_min"] * tile_size)

    img = Image.open(args.image).convert("RGBA")
    rgba = np.asarray(img)
    print(f"[info] heatmap shape={rgba.shape} zoom={zoom}")
    heat = decode_hot(rgba)
    print(f"[info] heat decoded; non-zero pixels: {(heat > 0).sum():,} "
          f"({(heat > 0).mean()*100:.1f}%)")

    if args.step_m is None:
        meters_per_pixel = (156543.03 * math.cos(math.radians(48.2)) / (2 ** zoom)) * (256 / tile_size)
        args.step_m = max(1.0, meters_per_pixel)
        print(f"[info] auto step_m={args.step_m:.2f} (tile_size={tile_size})")

    streets = json.loads(Path(args.streets).read_text())
    feats = streets["features"]
    print(f"[info] {len(feats):,} streets")

    out_features = []
    rows = []
    for idx, f in enumerate(feats):
        coords = f["geometry"]["coordinates"]
        if len(coords) < 2:
            continue
        s = sample_polyline(coords, heat, zoom, origin_px, step_m=args.step_m, tile_size=tile_size)
        if s.size < args.min_samples:
            continue
        length_m = polyline_length_m(coords)
        props = dict(f["properties"])
        props.update({
            "samples": int(s.size),
            "length_m": round(length_m, 1),
            "heat_mean": float(round(s.mean(), 4)),
            "heat_max": float(round(s.max(), 4)),
            "heat_p95": float(round(np.percentile(s, 95), 4)),
            "heat_nonzero_frac": float(round((s > 0).mean(), 4)),
        })
        out_features.append({
            "type": "Feature",
            "geometry": f["geometry"],
            "properties": props,
        })
        rows.append(props)

        if (idx + 1) % 20000 == 0:
            print(f"  ... processed {idx+1:,}/{len(feats):,}")

    print(f"[info] kept {len(out_features):,} streets with samples")

    out_geojson = Path(f"{args.out_prefix}.geojson")
    out_geojson.write_text(json.dumps({
        "type": "FeatureCollection",
        "features": out_features,
        "metadata": {
            "source_image": args.image,
            "zoom": zoom,
            "step_m": args.step_m,
            "vienna_run_heatmap": True,
        },
    }))

    out_csv = Path(f"{args.out_prefix}.csv")
    fieldnames = ["osm_id", "name", "highway", "length_m", "samples",
                  "heat_mean", "heat_max", "heat_p95", "heat_nonzero_frac",
                  "surface", "oneway"]
    with out_csv.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)

    # Ranking: heat_mean primary, length as tiebreaker; require >= 50m to filter stubs.
    ranked = [r for r in rows if r["length_m"] >= 50]
    ranked.sort(key=lambda r: (r["heat_mean"], r["length_m"]), reverse=True)

    out_top = Path(f"{args.out_prefix}_top.txt")
    with out_top.open("w") as fh:
        fh.write("== TOP 50 hottest run streets in Vienna (length >= 50m) ==\n")
        fh.write(f"{'rank':>4}  {'mean':>6}  {'p95':>6}  {'max':>6}  {'len(m)':>7}  "
                 f"{'highway':<14}  name\n")
        for i, r in enumerate(ranked[:50], 1):
            fh.write(f"{i:>4}  {r['heat_mean']:>6.3f}  {r['heat_p95']:>6.3f}  "
                     f"{r['heat_max']:>6.3f}  {r['length_m']:>7.0f}  "
                     f"{(r.get('highway') or '-'):<14}  "
                     f"{r.get('name') or '(unnamed)'}\n")
        # de-duplicate by name for a more readable list
        fh.write("\n== TOP 30 unique named streets (deduplicated) ==\n")
        seen = set()
        for r in ranked:
            n = r.get("name")
            if not n or n in seen:
                continue
            seen.add(n)
            fh.write(f"  {r['heat_mean']:>6.3f}  {r['length_m']:>6.0f}m  "
                     f"{(r.get('highway') or '-'):<14}  {n}\n")
            if len(seen) >= 30:
                break

    print(f"[done] -> {out_geojson}")
    print(f"[done] -> {out_csv}")
    print(f"[done] -> {out_top}")


if __name__ == "__main__":
    main()
