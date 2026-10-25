"""Local dev helper: serve learning-service/static and forward /api/* to the API, so the old test UI
(which calls relative URLs) works against the unified backend without changing either.
Local use only: it binds to 127.0.0.1 and is not part of the API.

Run the API first, then, from the repository root:
  uv run python scripts/dev_ui_proxy.py [ui_port=8080] [api_port=8000]
and open http://127.0.0.1:8080
"""

import http.client
import mimetypes
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

STATIC = (Path(__file__).resolve().parent.parent / "learning-service" / "static").resolve()
UI_PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8080
API_PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 8000
HOP = {
    "connection",
    "keep-alive",
    "transfer-encoding",
    "te",
    "trailer",
    "upgrade",
    "proxy-authorization",
    "host",
}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def _send(self, status, headers, body):
        self.send_response(status)
        for k, v in headers:
            if k.lower() not in HOP | {"content-length"}:
                self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _static(self):
        name = self.path.split("?", 1)[0].lstrip("/") or "index.html"
        path = (STATIC / name).resolve()
        if STATIC not in path.parents or not path.is_file():
            return self._send(404, [("Content-Type", "text/plain")], b"not found")
        ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self._send(
            200,
            [
                (
                    "Content-Type",
                    ctype + "; charset=utf-8"
                    if ctype.startswith("text/") or ctype.endswith("javascript")
                    else ctype,
                )
            ],
            path.read_bytes(),
        )

    def _proxy(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        headers = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
        conn = http.client.HTTPConnection("127.0.0.1", API_PORT, timeout=300)
        try:
            conn.request(self.command, self.path, body=body, headers=headers)
            resp = conn.getresponse()
            self._send(resp.status, resp.getheaders(), resp.read())
        except OSError as exc:
            self._send(
                502,
                [("Content-Type", "application/json")],
                f'{{"detail":"API not reachable on port {API_PORT}: {exc}"}}'.encode(),
            )
        finally:
            conn.close()

    def _route(self):
        if self.path.startswith(("/api/", "/openapi.json", "/docs")):
            self._proxy()
        else:
            self._static()

    do_GET = do_POST = do_PUT = do_DELETE = do_PATCH = _route


print(
    f"UI on http://127.0.0.1:{UI_PORT}  ->  API on 127.0.0.1:{API_PORT}  (static: {STATIC})",
    flush=True,
)
ThreadingHTTPServer(("127.0.0.1", UI_PORT), Handler).serve_forever()
