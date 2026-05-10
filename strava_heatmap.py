"""
Scrape Strava's global heatmap for a given bounding box and sport.

Default target: Vienna, sport=Run.

Two endpoints:
  - Public  : https://heatmap-external-{a,b,c}.strava.com/tiles/{sport}/{color}/{z}/{x}/{y}.png
              (unauthenticated, z<=11)
  - Auth    : https://content-a.strava.com/identified/globalheat/{sport}/{color}/{z}/{x}/{y}.png
              (authenticated, z<=15) -- requires the FULL cookie header from a logged-in
              browser session (3 CloudFront cookies + Strava session cookies). A CloudFront
              Function on the edge validates the session.

Pass cookies via --cookies "<full Cookie header value>" or env STRAVA_CF_COOKIES.

Output:
  - heatmap_<name>_<sport>_z<zoom>.png   (stitched image)
  - heatmap_<name>_<sport>_z<zoom>.json  (tile range + geographic bounds)
  - tiles/<sport>/<zoom>/<x>/<y>.png     (raw cached tiles)
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from pathlib import Path
from typing import Optional, Tuple

import requests
from PIL import Image

DEFAULT_TILE_SIZE = 256  # public endpoint serves 256, auth endpoint serves 512 — detected at runtime
SUBDOMAINS = ["a", "b", "c"]

# Vienna bounding box (W, S, E, N)
VIENNA_BBOX = (16.1830, 48.1182, 16.5775, 48.3231)

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


def deg2tile(lat: float, lon: float, zoom: int) -> tuple[int, int]:
    lat_rad = math.radians(lat)
    n = 2 ** zoom
    x = int((lon + 180.0) / 360.0 * n)
    y = int((1.0 - math.log(math.tan(lat_rad) + 1 / math.cos(lat_rad)) / math.pi) / 2.0 * n)
    return x, y


def tile2deg(x: int, y: int, zoom: int) -> tuple[float, float]:
    """Return (lat, lon) of the NW corner of tile."""
    n = 2 ** zoom
    lon = x / n * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / n))))
    return lat, lon


def tile_range_for_bbox(bbox: tuple[float, float, float, float], zoom: int):
    w, s, e, n = bbox
    x_min, y_min = deg2tile(n, w, zoom)  # NW
    x_max, y_max = deg2tile(s, e, zoom)  # SE
    return x_min, y_min, x_max, y_max


def tile_url(sport: str, color: str, z: int, x: int, y: int,
             authenticated: bool, sub_idx: int = 0) -> str:
    if authenticated:
        # Strava authenticated heatmap is served only from content-a.strava.com.
        return f"https://content-a.strava.com/identified/globalheat/{sport}/{color}/{z}/{x}/{y}.png"
    sub = SUBDOMAINS[sub_idx % len(SUBDOMAINS)]
    return f"https://heatmap-external-{sub}.strava.com/tiles/{sport}/{color}/{z}/{x}/{y}.png"


def fetch_tile(session: requests.Session, sport: str, color: str, z: int, x: int, y: int,
               authenticated: bool, cache_dir: Path, retries: int = 3):
    cache_path = cache_dir / sport / str(z) / str(x) / f"{y}.png"
    if cache_path.exists() and cache_path.stat().st_size > 200:
        return cache_path.read_bytes()
    cache_path.parent.mkdir(parents=True, exist_ok=True)

    last_err = None
    for attempt in range(retries):
        url = tile_url(sport, color, z, x, y, authenticated, sub_idx=x + y + attempt)
        try:
            r = session.get(url, timeout=20)
            if r.status_code == 200 and r.headers.get("content-type", "").startswith("image/"):
                cache_path.write_bytes(r.content)
                return r.content
            if r.status_code == 404:
                return None
            last_err = f"HTTP {r.status_code}"
            if attempt == 0:
                print(f"  [warn] {url} -> {r.status_code} (body[:120]={r.text[:120]!r})",
                      file=sys.stderr)
        except requests.RequestException as e:
            last_err = str(e)
        time.sleep(0.4 * (attempt + 1))

    print(f"  [warn] tile {z}/{x}/{y} failed: {last_err}", file=sys.stderr)
    return None


def parse_cookie_string(s: str) -> dict:
    out = {}
    for part in s.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        k, v = part.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def has_cloudfront_cookies(cookies: dict) -> bool:
    needed = {"CloudFront-Key-Pair-Id", "CloudFront-Policy", "CloudFront-Signature"}
    return needed.issubset(cookies.keys())


def scrape(bbox, sport: str, color: str, zoom: int,
           output_prefix: str, cookies, cookie_header: str,
           cache_dir: Path) -> dict:
    authenticated = bool(cookie_header) and has_cloudfront_cookies(cookies or {})
    if not authenticated and zoom > 11:
        print(f"[info] zoom={zoom} requires authenticated session; falling back to z=11.",
              file=sys.stderr)
        zoom = 11

    x_min, y_min, x_max, y_max = tile_range_for_bbox(bbox, zoom)
    cols = x_max - x_min + 1
    rows = y_max - y_min + 1
    total = cols * rows
    print(f"[info] zoom={zoom} grid={cols}x{rows} ({total} tiles) authenticated={authenticated}")

    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Referer": "https://www.strava.com/maps/global-heatmap",
        "Origin": "https://www.strava.com",
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })
    # Use a raw Cookie header to avoid `~` URL-encoding by requests.
    if cookie_header:
        session.headers["Cookie"] = cookie_header

    import io
    tile_size = DEFAULT_TILE_SIZE
    canvas = None
    fetched = 0
    empty = 0
    for i, x in enumerate(range(x_min, x_max + 1)):
        for j, y in enumerate(range(y_min, y_max + 1)):
            data = fetch_tile(session, sport, color, zoom, x, y, authenticated, cache_dir)
            if data is None:
                empty += 1
                continue
            try:
                img = Image.open(io.BytesIO(data)).convert("RGBA")
                if canvas is None:
                    tile_size = img.size[0]  # detect 256 vs 512
                    canvas = Image.new("RGBA", (cols * tile_size, rows * tile_size), (0, 0, 0, 0))
                canvas.paste(img, (i * tile_size, j * tile_size))
                fetched += 1
            except Exception as e:
                print(f"  [warn] decode {zoom}/{x}/{y}: {e}", file=sys.stderr)
    if canvas is None:
        canvas = Image.new("RGBA", (cols * tile_size, rows * tile_size), (0, 0, 0, 0))

    out_png = Path(f"{output_prefix}.png")
    canvas.save(out_png, "PNG")

    nw_lat, nw_lon = tile2deg(x_min, y_min, zoom)
    se_lat, se_lon = tile2deg(x_max + 1, y_max + 1, zoom)
    meta = {
        "sport": sport,
        "color": color,
        "zoom": zoom,
        "authenticated": authenticated,
        "tile_size": tile_size,
        "tile_range": {"x_min": x_min, "y_min": y_min, "x_max": x_max, "y_max": y_max},
        "image_size": canvas.size,
        "geo_bounds": {
            "west": nw_lon, "north": nw_lat,
            "east": se_lon, "south": se_lat,
        },
        "tiles_fetched": fetched,
        "tiles_empty_or_failed": empty,
    }
    out_json = Path(f"{output_prefix}.json")
    out_json.write_text(json.dumps(meta, indent=2))

    print(f"[done] {fetched}/{total} tiles fetched ({empty} empty/failed)")
    print(f"[done] image  -> {out_png}")
    print(f"[done] meta   -> {out_json}")
    return meta


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--sport", default="run", choices=["all", "ride", "run", "water", "winter"])
    p.add_argument("--color", default="hot",
                   choices=["hot", "blue", "purple", "gray", "bluered", "mobileblue"])
    p.add_argument("--zoom", type=int, default=11,
                   help="Tile zoom (1-15). Without auth cookies, max is 11.")
    p.add_argument("--bbox", help="W,S,E,N bounding box. Defaults to Vienna.")
    p.add_argument("--name", default="vienna", help="Output filename slug.")
    p.add_argument("--cookies", default=os.environ.get("STRAVA_CF_COOKIES"),
                   help='CloudFront cookies for authenticated tiles, e.g. '
                        '"CloudFront-Key-Pair-Id=...; CloudFront-Policy=...; CloudFront-Signature=..."')
    p.add_argument("--cache-dir", default="tiles")
    p.add_argument("--output-dir", default=".")
    args = p.parse_args()

    if args.bbox:
        bbox = tuple(float(v) for v in args.bbox.split(","))
        if len(bbox) != 4:
            p.error("--bbox needs 4 comma-separated numbers: W,S,E,N")
    else:
        bbox = VIENNA_BBOX

    cookie_header = (args.cookies or "").strip()
    cookies = parse_cookie_string(cookie_header) if cookie_header else {}

    output_prefix = str(Path(args.output_dir) / f"heatmap_{args.name}_{args.sport}_z{args.zoom}")
    Path(args.output_dir).mkdir(parents=True, exist_ok=True)

    scrape(
        bbox=bbox,
        sport=args.sport,
        color=args.color,
        zoom=args.zoom,
        output_prefix=output_prefix,
        cookies=cookies,
        cookie_header=cookie_header,
        cache_dir=Path(args.cache_dir),
    )


if __name__ == "__main__":
    main()
