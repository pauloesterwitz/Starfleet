#!/usr/bin/env python3
"""Stands in for the llama-swap gate (ctxproxy): just enough for Fleet's own polling and its key check.
  GET /starfleet/check  204 for the key fixture-key, 403 for any other key or none
  GET /v1/models, /running  an empty list: there is no model, so nothing can be loaded or unloaded
Everything else is refused with 404. Every request is appended to the log file as "METHOD path key-state", so the
test can prove that Fleet asked for nothing else.   Usage: gate.py PORT LOGFILE"""
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

KEY, PORT, LOG = "fixture-key", int(sys.argv[1]), sys.argv[2]


class Gate(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, code, body=None):
        data = json.dumps(body).encode() if body is not None else b""
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        sent = self.headers.get("X-Starfleet-Key")
        with open(LOG, "a") as f:
            f.write(f"GET {self.path} {'none' if sent is None else 'right' if sent == KEY else 'wrong'}\n")
        if self.path == "/starfleet/check":
            return self.reply(204) if sent == KEY else self.reply(403, {"error": {"message": "Only Starfleet Command may load models."}})
        if self.path == "/v1/models":
            return self.reply(200, {"data": []})
        if self.path == "/running":
            return self.reply(200, {"running": []})
        self.reply(404, {"error": "not here"})

    def do_POST(self):
        with open(LOG, "a") as f:
            f.write(f"POST {self.path} refused\n")
        self.reply(404, {"error": "not here"})


HTTPServer(("127.0.0.1", PORT), Gate).serve_forever()
