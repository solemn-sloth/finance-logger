#!/usr/bin/env python3
"""
One-off Monzo OAuth bootstrap.

Run this whenever the refresh token has died (revoked, or unused >90 days)
and daily_snapshot.py starts failing with "bad_refresh_token".

Flow:
  1. Prints an authorize URL — open it in a browser logged into the target
     Monzo account.
  2. Starts a local server on MONZO_REDIRECT_URI's port to catch the
     callback and exchange the code for tokens.
  3. Saves the refresh token to tokens.json (used by monzo.py from then on).

Monzo detail: the resulting access token is initially "unapproved" — it can
only hit /ping/whoami until you tap "Yes, allow access" on the push
notification Monzo sends to the account's app. That can take up to a
minute; do it before running daily_snapshot.py.

Run: python3 src/bootstrap_monzo.py
"""

import os
import secrets
import sys
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlencode, urlparse, parse_qs

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / "config" / ".env")

sys.path.insert(0, os.path.dirname(__file__))
import monzo  # noqa: E402

AUTH_URL = "https://auth.monzo.com/"
CLIENT_ID = os.environ["MONZO_CLIENT_ID"]
CLIENT_SECRET = os.environ["MONZO_CLIENT_SECRET"]
REDIRECT_URI = os.environ["MONZO_REDIRECT_URI"]

_result = {}


class _CallbackHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        params = parse_qs(urlparse(self.path).query)
        _result["code"] = params.get("code", [None])[0]
        _result["state"] = params.get("state", [None])[0]
        _result["error"] = params.get("error_description", [None])[0]

        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        if _result["code"]:
            self.wfile.write(b"Monzo authorized. You can close this tab.")
        else:
            self.wfile.write(f"Monzo auth failed: {_result['error']}".encode())


def main():
    state = secrets.token_urlsafe(16)
    authorize_url = f"{AUTH_URL}?" + urlencode({
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "state": state,
    })

    port = urlparse(REDIRECT_URI).port or 8080
    print(
        f"This server has no browser — from your laptop, first tunnel the callback port:\n\n"
        f"  ssh -L {port}:localhost:{port} <user>@<this-server>\n\n"
        f"Then, in a browser on your laptop, open:\n\n  {authorize_url}\n"
    )
    try:
        webbrowser.open(authorize_url)
    except Exception:
        pass
    print(f"Waiting for callback on port {port}...")

    server = HTTPServer(("localhost", port), _CallbackHandler)
    server.handle_request()  # blocks for exactly one request

    if not _result.get("code"):
        print(f"ERROR: Monzo did not return a code: {_result.get('error')}", file=sys.stderr)
        sys.exit(1)
    if _result.get("state") != state:
        print("ERROR: state mismatch — possible CSRF, aborting.", file=sys.stderr)
        sys.exit(1)

    resp = requests.post(
        f"{monzo.MONZO_API}/oauth2/token",
        data={
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "redirect_uri": REDIRECT_URI,
            "code": _result["code"],
        },
        timeout=15,
    )
    if not resp.ok:
        print(f"ERROR: token exchange failed: {resp.status_code} {resp.text}", file=sys.stderr)
        sys.exit(1)

    data = resp.json()
    monzo.save_token(data["refresh_token"])
    print("\nSaved new refresh token to tokens.json.")
    print(
        "IMPORTANT: Monzo just sent a push notification to your phone — "
        "tap 'Yes, allow access' in the Monzo app before running daily_snapshot.py, "
        "or balance/transaction calls will fail with 403 until approved."
    )


if __name__ == "__main__":
    main()
