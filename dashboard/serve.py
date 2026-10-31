#!/usr/bin/env python3
"""Local read-only dashboard for the Day Trader skill pack.

    python3 dashboard/serve.py [--port 8765] [--dir trader]

Reads the JSON files the skills write into trader/ (watchlist, entry, plan,
timemachine, orders, risk, alerts, prices) and shows them in a browser.
It never writes to trader/ and never talks to a broker. Where a file is
missing it falls back to dashboard/demo/ and labels the panel as demo data.
"""
import argparse
import json
import os
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
NAMES = ["watchlist", "hl_watchlist", "hl_signals", "entry", "plan", "timemachine", "orders", "risk", "alerts", "prices"]


def load(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def clock():
    try:
        out = subprocess.run([sys.executable, os.path.join(ROOT, "trader-alerts", "alerts.py"), "clock"],
                             capture_output=True, text=True, timeout=10, cwd=ROOT)
        return out.stdout.strip()
    except Exception as e:  # noqa: BLE001
        return "clock unavailable: %s" % e


class Handler(BaseHTTPRequestHandler):
    data_dir = "trader"

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as fh:
                return self._send(200, fh.read(), "text/html; charset=utf-8")
        if path == "/btc":
            with open(os.path.join(HERE, "btc.html"), "rb") as fh:
                return self._send(200, fh.read(), "text/html; charset=utf-8")
        if path == "/api/btc":
            import btc
            try:
                return self._send(200, json.dumps(btc.analyse()).encode(), "application/json")
            except Exception as e:  # noqa: BLE001
                return self._send(502, json.dumps({"error": str(e)}).encode(), "application/json")
        if path == "/api/state":
            files, demo = {}, []
            for n in NAMES:
                d = load(os.path.join(ROOT, self.data_dir, n + ".json"))
                if d is None:
                    d = load(os.path.join(HERE, "demo", n + ".json"))
                    if d is not None:
                        demo.append(n)
                if d is not None:
                    files[n] = d
            body = json.dumps({"files": files, "demo": demo, "clock": clock()}).encode()
            return self._send(200, body, "application/json")
        self._send(404, b"not found", "text/plain")

    def log_message(self, *a):
        pass


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--dir", default="trader", help="folder the skills write to, relative to the repo root")
    a = ap.parse_args()
    Handler.data_dir = a.dir
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), Handler)
    print("Dashboard on http://127.0.0.1:%d  (reading %s/, Ctrl+C to stop)" % (a.port, a.dir))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
