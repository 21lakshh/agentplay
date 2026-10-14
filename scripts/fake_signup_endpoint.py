#!/usr/bin/env python3
"""A stand-in signup endpoint for trying the welcome card locally.

    python3 scripts/fake_signup_endpoint.py [--port 8787] [--fail]
    AGENTPLAY_SIGNUP_URL=http://127.0.0.1:8787/exec agentplay view

Prints each event it receives. ``--fail`` answers 500, to see the card's error.
"""

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8787)
    ap.add_argument("--fail", action="store_true", help="answer 500 to every request")
    args = ap.parse_args()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a: object) -> None:
            return

        def do_POST(self) -> None:  # noqa: N802
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            try:
                print(json.dumps(json.loads(body)), flush=True)
            except ValueError:
                print(f"(not JSON) {body[:200]!r}", flush=True)
            self.send_response(500 if args.fail else 200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok": true}')

    print(f"Listening on http://127.0.0.1:{args.port}/exec", flush=True)
    ThreadingHTTPServer(("127.0.0.1", args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
