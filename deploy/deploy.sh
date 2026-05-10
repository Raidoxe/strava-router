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

echo "==> python deps"
python3 -m pip install --quiet --user -r requirements.txt

echo "==> sanity-check graph cache present"
test -f streets_graph.npz || {
  echo "MISSING streets_graph.npz - upload it once with scp." >&2
  exit 2
}
test -f signal_flag.npy || {
  echo "MISSING signal_flag.npy - upload it once with scp." >&2
  exit 2
}

echo "==> restart app service"
sudo /usr/bin/systemctl restart strava-router@$USER.service

echo "==> deploy ok"
