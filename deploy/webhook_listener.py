"""
Tiny webhook listener that GitHub calls on every push.

Verifies the HMAC-SHA256 signature, runs deploy.sh in a subprocess. Designed
to run as the strava-deploy.service systemd unit.

Required env (loaded from /etc/strava-router/deploy.env):
  WEBHOOK_SECRET    same value pasted into GitHub repo → Settings → Webhooks
  DEPLOY_PORT       (optional) port to listen on, default 9001
  DEPLOY_BRANCH     (optional) branch name to react to, default main
  REPO_DIR          (optional) where to run deploy.sh, default /srv/strava-router
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "").encode()
DEPLOY_PORT = int(os.environ.get("DEPLOY_PORT", "9001"))
DEPLOY_BRANCH = os.environ.get("DEPLOY_BRANCH", "main")
REPO_DIR = os.environ.get("REPO_DIR", "/srv/strava-router")

if not WEBHOOK_SECRET:
    sys.stderr.write(
        "FATAL: WEBHOOK_SECRET not set. Put it in /etc/strava-router/deploy.env\n"
    )
    sys.exit(1)


def verify_signature(payload: bytes, header: str) -> bool:
    if not header or not header.startswith("sha256="):
        return False
    expected = hmac.new(WEBHOOK_SECRET, payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, header.split("=", 1)[1])


def run_deploy():
    """Run deploy.sh; logs go to journal via stdout/stderr."""
    print(f"[deploy] starting deploy.sh in {REPO_DIR}", flush=True)
    p = subprocess.run(
        ["bash", "deploy/deploy.sh"],
        cwd=REPO_DIR,
        capture_output=True,
        text=True,
        timeout=300,
    )
    print(f"[deploy] deploy.sh stdout:\n{p.stdout}", flush=True)
    if p.stderr:
        print(f"[deploy] deploy.sh stderr:\n{p.stderr}", flush=True)
    print(f"[deploy] exit code {p.returncode}", flush=True)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # use journal-friendly logging
        sys.stderr.write("[%s] %s\n" % (self.address_string(), fmt % args))

    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"ok\n")
            return
        self.send_response(404)
        self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        sig = self.headers.get("X-Hub-Signature-256", "")
        if not verify_signature(body, sig):
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b"bad signature\n")
            return

        event = self.headers.get("X-GitHub-Event", "")
        if event == "ping":
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"pong\n")
            return
        if event != "push":
            self.send_response(204)
            self.end_headers()
            return

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            self.send_response(400)
            self.end_headers()
            return

        ref = payload.get("ref", "")
        expected_ref = f"refs/heads/{DEPLOY_BRANCH}"
        if ref != expected_ref:
            print(f"[deploy] ignoring push to {ref} (want {expected_ref})", flush=True)
            self.send_response(204)
            self.end_headers()
            return

        # Reply quickly, do the deploy in a background thread.
        self.send_response(202)
        self.end_headers()
        self.wfile.write(b"accepted\n")
        threading.Thread(target=run_deploy, daemon=True).start()


def main():
    server = HTTPServer(("0.0.0.0", DEPLOY_PORT), Handler)
    print(f"[deploy] webhook listener on :{DEPLOY_PORT}, "
          f"branch={DEPLOY_BRANCH}, repo={REPO_DIR}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
