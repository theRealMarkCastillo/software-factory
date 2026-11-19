"""Tiny dependency-free HTTP app: double an integer."""

from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlsplit


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 - stdlib HTTP handler API
        number = int(parse_qs(urlsplit(self.path).query).get("n", ["1"])[0])
        body = str(number * 2).encode()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", 8000), Handler).serve_forever()
