#!/usr/bin/env python3
"""Tiny localhost receiver: the browser capture POSTs its JSON here."""
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

OUT_PATH = sys.argv[1]
PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8765


class H(BaseHTTPRequestHandler):
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "content-type")

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_POST(self):
        n = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(n)
        try:
            json.loads(body)  # validate
            with open(OUT_PATH, "wb") as f:
                f.write(body)
            print(f"saved {n} bytes -> {OUT_PATH}", flush=True)
            code, msg = 200, b"ok"
        except Exception as e:  # noqa: BLE001
            print(f"bad payload: {e}", flush=True)
            code, msg = 400, str(e).encode()
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(msg)

    def log_message(self, *a):  # quiet
        pass


HTTPServer(("127.0.0.1", PORT), H).serve_forever()
