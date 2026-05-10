"""
Fetch street network from OpenStreetMap via Overpass API for a given bounding box.
Output: GeoJSON FeatureCollection of LineStrings, one feature per OSM way.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import List

import requests

OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.openstreetmap.fr/api/interpreter",
]

# OSM highway types we care about for running/walking analysis.
RUNNABLE_HIGHWAYS = (
    "primary|secondary|tertiary|residential|unclassified|living_street|"
    "pedestrian|footway|path|track|cycleway|service|trunk|primary_link|"
    "secondary_link|tertiary_link|trunk_link"
)


def overpass_query(bbox: tuple, highway_filter: str) -> str:
    s, w, n, e = bbox[1], bbox[0], bbox[3], bbox[2]
    return f"""
[out:json][timeout:120];
(
  way["highway"~"^({highway_filter})$"]({s},{w},{n},{e});
);
out geom;
""".strip()


def run_overpass(query: str) -> dict:
    last_err = None
    for url in OVERPASS_ENDPOINTS:
        try:
            print(f"[info] querying {url} ...", file=sys.stderr)
            r = requests.post(url, data={"data": query}, timeout=180,
                              headers={"User-Agent": "strava-heat-mapper/0.1"})
            if r.status_code == 200:
                return r.json()
            print(f"  HTTP {r.status_code}: {r.text[:200]}", file=sys.stderr)
        except requests.RequestException as e:
            last_err = e
            print(f"  error: {e}", file=sys.stderr)
        time.sleep(2)
    raise RuntimeError(f"All Overpass endpoints failed: {last_err}")


def to_geojson(data: dict) -> dict:
    features = []
    for el in data.get("elements", []):
        if el.get("type") != "way" or "geometry" not in el:
            continue
        coords = [[pt["lon"], pt["lat"]] for pt in el["geometry"]]
        if len(coords) < 2:
            continue
        tags = el.get("tags", {}) or {}
        props = {
            "osm_id": el["id"],
            "highway": tags.get("highway"),
            "name": tags.get("name"),
            "surface": tags.get("surface"),
            "oneway": tags.get("oneway"),
        }
        features.append({
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": coords},
            "properties": props,
        })
    return {"type": "FeatureCollection", "features": features}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--bbox", default="16.1830,48.1182,16.5775,48.3231",
                   help="W,S,E,N (default: Vienna)")
    p.add_argument("--out", default="streets_vienna.geojson")
    p.add_argument("--filter", default=RUNNABLE_HIGHWAYS,
                   help="Pipe-separated OSM highway= values to include.")
    args = p.parse_args()

    bbox = tuple(float(v) for v in args.bbox.split(","))
    if len(bbox) != 4:
        p.error("--bbox needs W,S,E,N")

    q = overpass_query(bbox, args.filter)
    data = run_overpass(q)
    gj = to_geojson(data)
    Path(args.out).write_text(json.dumps(gj))
    print(f"[done] {len(gj['features'])} ways -> {args.out}")


if __name__ == "__main__":
    main()
