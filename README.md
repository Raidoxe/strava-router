# Strava Heat Router (Vienna)

Scrape Strava's global running heatmap for Vienna, project the heat onto OSM
streets, and plan closed-loop running routes that prefer popular streets while
penalising stops, sharp turns, and back-and-forth corridors. Plus a small web
app for picking a starting address and downloading a route.

## Layout

```
.
├── README.md
│
├── strava_heatmap.py          # tile scraper (Strava global heatmap)
├── fetch_streets.py           # OSM streets for a bbox via Overpass
├── heat_streets.py            # samples each street's heat from the heatmap PNG
├── plan_route.py              # CLI route planner (uses C engine + cached graph)
│
├── route_engine.py            # graph + ctypes wrapper around dijkstra_core
├── dijkstra_core.c            # edge-pair Dijkstra (turn/signal/corridor penalties)
├── dijkstra_core.dylib        # compiled C library
│
├── streets_vienna.geojson     # raw OSM streets (151,900 ways)
├── streets_heat_z14.geojson   # streets annotated with heat scores  ← input to planner
├── streets_heat_z14.csv       # same data, flat
├── streets_heat_z14_top.txt   # top-50 hottest streets (human-readable)
├── streets_graph.npz          # cached CSR adjacency (built once)
├── signal_flag.npy            # cached traffic-signal node flags
├── traffic_signals.json       # 11,800 traffic-signals/stops from OSM
│
├── heatmap_vienna_run_z14.png # stitched 9728×7680 heatmap (auth required to refresh)
├── heatmap_vienna_run_z14.json
│
├── tiles/                     # raw cached PNG tiles
└── web/                       # web app (Flask + Leaflet)
    ├── server.py
    └── static/{index.html, app.js, style.css}
```

## Build the C engine

```bash
clang -O3 -ffast-math -shared -fPIC -o dijkstra_core.dylib dijkstra_core.c -lm
```

## End-to-end refresh (only needed if you want fresh data)

```bash
# 1. Authenticated cookies for Strava (browser → DevTools → Network → tile request → Cookie header)
COOKIE='_strava4_session=...; CloudFront-Key-Pair-Id=...; CloudFront-Policy=...; CloudFront-Signature=...; _strava_idcf=...'

# 2. Scrape Vienna heatmap at z=14 (5 m/pixel)
python3 strava_heatmap.py --sport run --zoom 14 --name vienna --cookies "$COOKIE"

# 3. Fetch OSM streets for the Vienna bbox
python3 fetch_streets.py

# 4. Sample heat onto streets
python3 heat_streets.py \
  --image heatmap_vienna_run_z14.png \
  --meta heatmap_vienna_run_z14.json \
  --streets streets_vienna.geojson \
  --out-prefix streets_heat_z14
```

## Plan a route (CLI)

```bash
python3 plan_route.py                              # 4 km loop from default address
python3 plan_route.py --target-km 6 --seed 42      # 6 km, reproducible
python3 plan_route.py --home-lon 16.40 --home-lat 48.20
```

Outputs `route.geojson`, `route.gpx`, `route_summary.txt`.

## Web app

```bash
python3 web/server.py
# open http://localhost:8000
```

Type an address, pick a distance, hit **Generate**. The map shows the loop and
the GPX/GeoJSON download links appear underneath.

## Algorithm in two lines

Edge-pair Dijkstra over the OSM graph with cost
`length × (1 + α × (1 − heat)) + linear-turn-penalty + signal-stop-penalty`.
Outbound to a hot turnaround in a randomly-sampled compass octant; return
penalises both outbound edges and a 130 m corridor buffer so the two halves
diverge geographically.

The hot inner loop is in `dijkstra_core.c`. Total wall time per route is
~2 seconds for Vienna (1.27 M states, ~12 ms per return Dijkstra).
