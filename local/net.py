"""Minimal stdlib JSON-over-HTTP plumbing shared by the site and coordinator deployments."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

MAX_BODY_BYTES = 5_000_000


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _call(req: urllib.request.Request, timeout: float) -> dict:
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:300]
        raise RuntimeError(f"{req.full_url} -> HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"{req.full_url} unreachable: {e.reason}") from e


def post_json(url: str, payload: dict, timeout: float = 30) -> dict:
    body = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    return _call(req, timeout)


def get_json(url: str, timeout: float = 5) -> dict:
    return _call(urllib.request.Request(url), timeout)


class JsonHandler(BaseHTTPRequestHandler):
    """Subclasses define GET/POST route tables: path -> fn(body) -> JSON-able object."""

    get_routes: dict = {}
    post_routes: dict = {}
    static_files: dict = {}  # path -> (file Path, content type)

    def log_message(self, fmt, *args):
        if "/fl/" not in self.path:  # training rounds are chatty
            super().log_message(fmt, *args)

    def send_json(self, status: int, obj) -> None:
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_file(self, path: Path, ctype: str) -> None:
        data = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _dispatch(self, routes: dict, body: dict | None) -> None:
        path = urlparse(self.path).path
        if body is None and path in self.static_files:
            self.send_file(*self.static_files[path])
            return
        fn = routes.get(path)
        if fn is None:
            self.send_json(404, {"error": f"no route {path}"})
            return
        try:
            self.send_json(200, fn(body))
        except ApiError as e:
            self.send_json(e.status, {"error": str(e)})
        except (ValueError, KeyError, TypeError) as e:
            self.send_json(400, {"error": f"{type(e).__name__}: {e}"})
        except Exception as e:  # fail loudly, but keep the server up
            self.send_json(500, {"error": f"{type(e).__name__}: {e}"})

    def do_GET(self):
        self._dispatch(self.get_routes, None)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY_BYTES:
            self.send_json(413, {"error": "body too large"})
            return
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self.send_json(400, {"error": "body must be JSON"})
            return
        if not isinstance(body, dict):
            self.send_json(400, {"error": "body must be a JSON object"})
            return
        self._dispatch(self.post_routes, body)
