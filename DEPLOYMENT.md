# Deployment

This doc describes the cheapest path to host the app on a home server with
auto-deploy on `git push`. The chosen stack is:

- **Code**: GitHub repo (private or public).
- **Public access**: Cloudflare Tunnel (free, no port-forwarding, free TLS,
  works from any home network).
- **Process supervisor**: systemd (one unit for the app, one for the deploy
  webhook listener).
- **Auto-deploy**: GitHub webhook → tiny HMAC-verified Python listener → runs
  `deploy/deploy.sh` which pulls + rebuilds + restarts.

If you'd rather pay €4.59/mo for a Hetzner CX22 VPS and skip the tunnel, all
the same steps apply minus the Cloudflare bits — instead run Caddy directly
(see `deploy/Caddyfile.example`) and point your domain's A record at the VPS.

---

## 0. One-time on your local machine

```bash
cd /path/to/StravaScrape
git status                     # confirm we're tracking the right things
gh repo create strava-router --public --source . --remote origin --push
# (or: github.com -> New repo -> follow the "push existing" instructions)
```

---

## 1. Server prerequisites

Tested on Ubuntu/Debian. Adjust apt → dnf/pacman as needed.

```bash
sudo apt update
sudo apt install -y python3-pip python3-venv build-essential git curl

# Install Cloudflare Tunnel
curl -L https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb \
  -o /tmp/cloudflared.deb
sudo dpkg -i /tmp/cloudflared.deb
```

---

## 2. Clone & first-run

Pick a directory that matches the systemd unit (`/srv/strava-router`):

```bash
sudo mkdir -p /srv/strava-router
sudo chown $USER:$USER /srv/strava-router
git clone git@github.com:YOUR_USER/strava-router.git /srv/strava-router
cd /srv/strava-router
make                       # builds dijkstra_core.so
pip3 install --user -r requirements.txt
```

### Transfer the data files (one-time)

The two big input files are gitignored. Copy from your laptop:

```bash
# from your laptop, not the server
scp streets_graph.npz traffic_signals.json YOUR_USER@server:/srv/strava-router/
```

`signal_flag.npy` auto-builds from `traffic_signals.json` on first request.
You only need `streets_heat_z14.geojson` if you want to *regenerate* the
graph cache; otherwise the npz is enough.

### Quick sanity check

```bash
python3 plan_route.py --target-km 4 --out-prefix /tmp/test
```

Should finish in ~2 s and write `/tmp/test.geojson`.

---

## 3. App as a systemd service

```bash
sudo cp deploy/strava-router.service /etc/systemd/system/strava-router@.service
sudo systemctl daemon-reload
sudo systemctl enable --now strava-router@$USER.service
sudo systemctl status strava-router@$USER.service
# Should show "active (running)" with gunicorn listening on 127.0.0.1:8000
curl http://127.0.0.1:8000/         # should return the index HTML
```

The `@$USER` syntax means the service runs as your user, with permissions to
write to the working directory.

---

## 4. Public access via Cloudflare Tunnel

Make sure your domain's nameservers are already pointed at Cloudflare
(it's free; sign up at cloudflare.com and follow their "Add a Site" flow).

```bash
cloudflared login                                          # browser flow
cloudflared tunnel create strava-router                    # creates a UUID
mkdir -p ~/.cloudflared
cp deploy/cloudflared-config.example.yml ~/.cloudflared/config.yml
$EDITOR ~/.cloudflared/config.yml                          # paste UUID, set hostname
cloudflared tunnel route dns strava-router heatroute.YOURDOMAIN.com
sudo cloudflared service install                           # systemd unit auto-installed
sudo systemctl start cloudflared
```

Hit `https://heatroute.YOURDOMAIN.com` from anywhere — TLS is handled by
Cloudflare, traffic flows through the outbound-only tunnel.

---

## 5. Auto-deploy webhook

```bash
# 1. Generate a webhook secret and store it on the server:
sudo mkdir -p /etc/strava-router
sudo cp deploy/deploy.env.example /etc/strava-router/deploy.env
SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
sudo sed -i "s|REPLACE_ME_WITH_64_HEX_CHARS|$SECRET|" /etc/strava-router/deploy.env
sudo chown $USER:$USER /etc/strava-router/deploy.env
sudo chmod 600 /etc/strava-router/deploy.env

# 2. Allow the deploy listener to restart the app without a password:
echo "$USER ALL=(root) NOPASSWD: /usr/bin/systemctl restart strava-router@$USER.service" \
  | sudo tee /etc/sudoers.d/strava-deploy
sudo chmod 440 /etc/sudoers.d/strava-deploy

# 3. Install + start the webhook listener service:
sudo cp deploy/strava-deploy.service /etc/systemd/system/strava-deploy@.service
sudo systemctl daemon-reload
sudo systemctl enable --now strava-deploy@$USER.service
sudo systemctl status strava-deploy@$USER.service

# 4. Print the secret you need:
sudo grep WEBHOOK_SECRET /etc/strava-router/deploy.env
```

### Wire up GitHub

Repo → **Settings → Webhooks → Add webhook**:

| Field | Value |
|---|---|
| Payload URL | `https://heatroute.YOURDOMAIN.com/__deploy` |
| Content type | `application/json` |
| Secret | (paste the value from step 4) |
| SSL verification | Enable |
| Events | "Just the push event" |

Click **Add webhook**, then click into it → **Recent Deliveries** → "Redeliver"
to test. You should see a 202 Accepted, and `journalctl -u strava-deploy@$USER`
will show `[deploy] starting deploy.sh`.

---

## 6. From now on

Every `git push origin main` triggers:

1. GitHub POSTs to `/__deploy`
2. Cloudflare Tunnel forwards to `127.0.0.1:9001`
3. `webhook_listener.py` verifies the HMAC and calls `deploy/deploy.sh`
4. `deploy.sh` does `git pull` + `make` + `pip install -r requirements.txt` +
   `systemctl restart strava-router`

Total time per deploy: ~5–10 seconds.

---

## Troubleshooting

```bash
# App logs
sudo journalctl -u strava-router@$USER -f

# Deploy listener logs
sudo journalctl -u strava-deploy@$USER -f

# Cloudflare tunnel logs
sudo journalctl -u cloudflared -f

# Manual deploy (if webhook isn't firing):
cd /srv/strava-router && bash deploy/deploy.sh
```

Common issues:
- **403 from `/__deploy`**: WEBHOOK_SECRET on server doesn't match the one in
  GitHub. Check `sudo grep WEBHOOK_SECRET /etc/strava-router/deploy.env`.
- **App fails to start with "missing dijkstra_core"**: run `make` in
  `/srv/strava-router`.
- **App fails with "Neither cache nor source GeoJSON found"**: scp
  `streets_graph.npz` to the server.
- **Deploy says "Permission denied" restarting service**: the sudoers file
  in step 5.2 wasn't created correctly. Run `sudo visudo -f /etc/sudoers.d/strava-deploy`.

---

## Alternative: free PaaS instead of home server

If you don't want to deal with a server at all, the same code deploys to
[Render.com](https://render.com) for free:

1. New → Web Service → connect the GitHub repo.
2. Build command: `make && pip install -r requirements.txt`
3. Start command: `gunicorn -w 1 -b 0.0.0.0:$PORT web.server:app`
4. Disk: add a 1 GB persistent disk mounted at `/data` and scp the data files
   there once (`render-cli ssh ...`), or place them in the repo via Git LFS.
5. Add your custom domain in Render → Settings → Custom Domain.

Free tier sleeps after 15 min idle (~30 s cold start). Otherwise identical
to the home-server setup.
