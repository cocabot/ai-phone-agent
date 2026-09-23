#!/usr/bin/env python3
"""Serve static/plivo_answer.xml for Plivo answer_url (needs a public tunnel)."""

from __future__ import annotations

from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

XML_PATH = Path(__file__).with_name("static") / "plivo_answer.xml"


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        body = XML_PATH.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "application/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        self.do_GET()

    def log_message(self, fmt: str, *args) -> None:
        print(f"plivo-answer {self.address_string()} {fmt % args}")


def main() -> None:
    port = int(__import__("os").environ.get("PLIVO_ANSWER_PORT", "8787"))
    server = HTTPServer(("0.0.0.0", port), Handler)
    print(f"serving {XML_PATH} on http://0.0.0.0:{port}/")
    print("Point a public HTTPS URL at this server, set PLIVO_ANSWER_URL to it.")
    server.serve_forever()


if __name__ == "__main__":
    main()
