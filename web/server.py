"""
Flask backend for the heat-route planner.

Loads the graph + traffic-signal flags ONCE at startup so each request just
runs the C Dijkstra (~2 s).

Endpoints:
  GET  /                    -> static index.html
  GET  /static/<path>       -> static files
  POST /api/plan            -> body: {lon, lat, target_km, seed?, address?}
                               returns: {geojson, gpx, stats, snap_offset_m}
"""

from __future__ import annotations

import math
import random
import sys
import time
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_from_directory

# Make the project root importable when running `python3 web/server.py`.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import plan_route as pr
from route_engine import Graph, haversine_m


app = Flask(__name__, static_folder=str(Path(__file__).parent / "static"),
            static_url_path="/static")

# ---- one-time graph load -----------------------------------------------------
print("[server] loading graph ...", flush=True)
_t0 = time.time()
GRAPH: Graph = Graph.from_geojson(
    ROOT / "streets_heat_z14.geojson",
    ROOT / "streets_graph.npz",
)
print(f"[server] graph: {GRAPH.n_nodes:,} nodes, {GRAPH.n_edges:,} edges "
      f"({time.time()-_t0:.2f}s)")
SIGNAL_FLAG = pr.load_signal_flag(GRAPH, ROOT / "traffic_signals.json",
                                  cache_path=ROOT / "signal_flag.npy")
print(f"[server] {int(SIGNAL_FLAG.sum()):,} signal nodes loaded.")

# Clamp the planner's home coordinate to within Vienna's heatmap bounds.
LAT_MIN, LAT_MAX = 48.107, 48.327
LON_MIN, LON_MAX = 16.171, 16.589

# Strava heatmap tile cache (populated by strava_heatmap.py). Served read-only
# to the FE so we can overlay it on the Leaflet map.
TILES_DIR = ROOT / "tiles"


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/tiles/<sport>/<int:z>/<int:x>/<int:y>.png")
def heatmap_tile(sport: str, z: int, x: int, y: int):
    # Allow only the known sport slugs Strava exposes; rejects path traversal.
    if sport not in {"all", "ride", "run", "water", "winter"}:
        abort(404)
    tile_dir = TILES_DIR / sport / str(z) / str(x)
    if not tile_dir.exists():
        abort(404)
    fname = f"{y}.png"
    if not (tile_dir / fname).exists():
        abort(404)
    resp = send_from_directory(tile_dir, fname, mimetype="image/png")
    resp.headers["Cache-Control"] = "public, max-age=86400"
    return resp


@app.route("/api/plan", methods=["POST"])
def api_plan():
    data = request.get_json(force=True, silent=True) or {}
    try:
        lon = float(data["lon"])
        lat = float(data["lat"])
        target_km = float(data.get("target_km", 4.0))
    except (KeyError, ValueError, TypeError):
        return jsonify({"error": "lon, lat, target_km required"}), 400

    if not (LAT_MIN <= lat <= LAT_MAX and LON_MIN <= lon <= LON_MAX):
        return jsonify({
            "error": (f"address is outside the Vienna coverage area "
                      f"(lat {LAT_MIN}-{LAT_MAX}, lon {LON_MIN}-{LON_MAX}). "
                      f"This demo only has data for Vienna right now."),
        }), 422

    if not (1.0 <= target_km <= 25.0):
        return jsonify({"error": "target_km must be between 1 and 25"}), 400

    seed = data.get("seed")
    if seed is None:
        seed = int(time.time() * 1000) & 0xFFFFFFFF
    rng = random.Random(int(seed))

    opts = pr.PlanOptions(
        alpha=float(data.get("alpha", 3.0)),
        turn_pen_m=float(data.get("turn_pen_m", 30.0)),
        signal_pen_m=float(data.get("signal_pen_m", 80.0)),
        inter_pen_m=float(data.get("inter_pen_m", 10.0)),
        corridor_m=float(data.get("corridor_m", 130.0)),
        corridor_mult=float(data.get("corridor_mult", 3.5)),
        simplify_m=float(data.get("simplify_m", 4.0)),
        verbose=False,
        home_label=str(data.get("address", "")),
    )

    t0 = time.time()
    try:
        result = pr.plan_loop(GRAPH, SIGNAL_FLAG, (lon, lat), target_km, opts, rng)
    except RuntimeError as e:
        return jsonify({"error": str(e)}), 500
    elapsed_ms = int((time.time() - t0) * 1000)

    home_node = result["home_node"]
    snap_off = haversine_m(
        (lon, lat),
        (float(GRAPH.node_lon[home_node]), float(GRAPH.node_lat[home_node])),
    )

    geojson = pr.make_geojson(result, opts)
    name = f"{target_km:.1f}km hot loop"
    if opts.home_label:
        name += f" from {opts.home_label[:60]}"
    gpx = pr.make_gpx(result["coords"], name)

    m = result["best_loop"]["metrics"]
    return jsonify({
        "geojson": geojson,
        "gpx": gpx,
        "snap_offset_m": round(snap_off, 1),
        "snap_lonlat": [float(GRAPH.node_lon[home_node]),
                        float(GRAPH.node_lat[home_node])],
        "stats": {
            "length_km": round(m["length_m"] / 1000, 2),
            "heat_mean_weighted": round(m["heat_mean_weighted"], 3),
            "named_streets": m["named_streets"],
            "signals_passed": m["signals_passed"],
            "intersections_passed": m["intersections_passed"],
            "turns_count": m["turns_count"],
            "sharp_turns": m["sharp_turns"],
            "total_turn_deg": m["total_turn_deg"],
            "direction": result["direction"],
        },
        "seed": int(seed),
        "elapsed_ms": elapsed_ms,
    })


if __name__ == "__main__":
    port = int(__import__("os").environ.get("PORT", 8000))
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
