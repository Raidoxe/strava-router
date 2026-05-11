#!/usr/bin/env bash
# Pull latest code, rebuild C lib, install/refresh deps, restart the app.
# Run from the repo root (e.g. /srv/strava-router).

set -euo pipefail

cd "$(dirname "$0")/.."

echo "==> git pull"
git fetch --quiet
git reset --hard "origin/$(git rev-parse --abbrev-ref HEAD)"

echo "==> rebuild dijkstra_core"
make --silent clean
make --silent

echo "==> python venv + deps"
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -r requirements.txt

echo "==> sanity-check graph cache present"
test -f streets_graph.npz || {
  echo "MISSING streets_graph.npz - upload it once with scp." >&2
  exit 2
}

# Heatmap tile cache is optional - the app still runs without it, but the
# in-browser heatmap overlay will be empty. Warn so the user notices.
if [ ! -d tiles ] || [ -z "$(ls -A tiles/run/14 2>/dev/null)" ]; then
  echo "WARN: tiles/ cache missing or empty - heatmap overlay will 404." >&2
  echo "      Upload with: scp -r tiles/ <user>@<host>:$(pwd)/" >&2
fi

echo "==> restart app service"
sudo /usr/bin/systemctl restart "strava-router@$USER.service"

echo "==> deploy ok"
