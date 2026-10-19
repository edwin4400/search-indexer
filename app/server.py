"""Local HTTP server for the app.

Three deliberate constraints:

* **Loopback only.** Binds 127.0.0.1; nothing on the network can reach it.
* **Token on every API call.** The token is passed in the launch URL, kept in
  `sessionStorage`, and sent as `X-Token`. A custom header is something a
  cross-site form post cannot set, so a malicious page cannot drive the API.
  An `Origin` header, when present, must also be loopback.
* **A fixed static whitelist.** Only four files are ever served, so there is no
  path-traversal surface to get wrong.
"""

from __future__ import annotations

import json
import mimetypes
import os
import secrets
import sys
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import msvcrt
except ImportError:                      # not Windows; locking is skipped
    msvcrt = None

from .service import AppService, BusyError

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "application/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/icons.svg": ("icons.svg", "image/svg+xml"),
    "/vendor/pdf.min.mjs": ("vendor/pdf.min.mjs", "application/javascript; charset=utf-8"),
    "/vendor/pdf.worker.min.mjs": (
        "vendor/pdf.worker.min.mjs", "application/javascript; charset=utf-8"),
    "/vendor/pdf.LICENSE": ("vendor/pdf.LICENSE", "text/plain; charset=utf-8"),
}
LOOPBACK_PREFIXES = ("http://127.0.0.1:", "http://localhost:", "http://[::1]:")


class Handler(BaseHTTPRequestHandler):
    server_version = "ContentSearch"
    protocol_version = "HTTP/1.1"

    @property
    def service(self) -> AppService:
        return self.server.service           # type: ignore[attr-defined]

    @property
    def token(self) -> str:
        return self.server.token             # type: ignore[attr-defined]

    def log_message(self, fmt, *args):       # noqa: A003 - base class signature
        if getattr(self.server, "verbose", False):
            print(f"  {fmt % args}", flush=True)

    def handle_one_request(self) -> None:
        try:
            super().handle_one_request()
        except ConnectionResetError:
            # A browser closing an idle keep-alive connection is normal. Without
            # this, every closed tab prints a full traceback to stderr.
            self.close_connection = True

    # ------------------------------------------------------------- plumbing

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload, status: int = 200) -> None:
        self._send(status, json.dumps(payload).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _fail(self, status: int, message: str) -> None:
        self._json({"error": message}, status)

    def _request_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            return {}

    def _authorised(self) -> bool:
        if self.headers.get("X-Token") != self.token:
            return False
        origin = self.headers.get("Origin")
        if origin and not origin.startswith(LOOPBACK_PREFIXES):
            return False
        return True

    def _static(self, path: str) -> None:
        name, content_type = STATIC[path]
        try:
            with open(os.path.join(STATIC_DIR, name), "rb") as handle:
                self._send(200, handle.read(), content_type)
        except OSError:
            self._fail(500, f"missing static asset: {name}")

    # ------------------------------------------------------------------ GET

    def do_GET(self) -> None:                # noqa: N802 - base class signature
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path in STATIC:
            return self._static(path)
        if not path.startswith("/api/"):
            return self._fail(404, "not found")
        if not self._authorised():
            return self._fail(401, "missing or invalid session token")

        params = urllib.parse.parse_qs(parsed.query)
        try:
            if path == "/api/status":
                return self._json(self.service.status())
            if path == "/api/search":
                query = (params.get("q") or [""])[0].strip()
                if not query:
                    return self._json(
                        {"query": "", "count": 0, "results": [], "note": None})
                limit = int((params.get("limit") or ["30"])[0])
                scope = (params.get("scope") or ["all"])[0]
                return self._json(self.service.search(query, limit=limit, scope=scope))
            if path == "/api/item":
                item = self.service.item(
                    int((params.get("id") or ["0"])[0]),
                    query=((params.get("m") or params.get("q") or [""])[0]).strip() or None,
                )
                return self._json(item) if item else self._fail(404, "no such item")
            if path == "/api/verify":
                return self._json(self.service.verify())
            if path == "/api/raw":
                # The original bytes, for viewers that need the real file rather
                # than extracted text (pdf.js, for one).
                data, name = self.service.raw_bytes(
                    int((params.get("id") or ["0"])[0]))
                kind = mimetypes.guess_type(name)[0] or "application/octet-stream"
                return self._send(200, data, kind)
        except (ValueError, TypeError) as exc:
            return self._fail(400, str(exc))
        return self._fail(404, "not found")

    # ------------------------------------------------------------------ POST

    def do_POST(self) -> None:               # noqa: N802 - base class signature
        path = urllib.parse.urlparse(self.path).path
        if not path.startswith("/api/"):
            return self._fail(404, "not found")
        if not self._authorised():
            return self._fail(401, "missing or invalid session token")

        body = self._request_body()
        try:
            if path == "/api/roots":
                return self._json(self.service.add_root(str(body.get("path", ""))))
            if path == "/api/roots/remove":
                return self._json(self.service.remove_root(str(body.get("path", ""))))
            if path == "/api/roots/toggle":
                roots = self.service.set_root_enabled(
                    str(body.get("path", "")), bool(body.get("enabled", True)))
                return self._json({"roots": roots})
            if path == "/api/index":
                return self._json(self.service.start_index())
            if path == "/api/watch":
                interval = body.get("interval")
                return self._json(self.service.start_watch(
                    float(interval) if interval else None))
            if path == "/api/stop":
                return self._json(self.service.stop())
            if path == "/api/index/clear":
                return self._json(self.service.clear_index())
            if path == "/api/mail/outlook":
                limit = body.get("limit")
                return self._json(self.service.start_mail(
                    limit=int(limit) if limit else None,
                    folder=str(body["folder"]) if body.get("folder") else None,
                    via=str(body.get("via", "file")),
                ))
            if path == "/api/recent":
                return self._json(
                    {"recent": self.service.record_search(str(body.get("query", "")))})
            if path == "/api/open":
                return self._json(self.service.open_item(
                    int(body.get("id", 0)), str(body.get("action", "open"))))
            if path == "/api/quit":
                # Answer first, then stop from another thread: shutdown() must
                # not be called on the thread running serve_forever.
                self._json({"stopping": True})
                threading.Timer(0.6, self.server.shutdown).start()
                return
        except BusyError as exc:
            return self._fail(409, str(exc))
        except (ValueError, TypeError) as exc:
            return self._fail(400, str(exc))
        return self._fail(404, "not found")


class LoopbackServer(ThreadingHTTPServer):
    """A loopback server that refuses to share its address.

    Python sets SO_REUSEADDR for HTTP servers. On Windows that lets a second
    process bind a port another process is already listening on, after which
    incoming connections are split between them - a page can load from one
    instance while its API calls reach another, which presents as a page that
    never finishes loading. Refusing the bind is the only predictable behaviour.
    """

    allow_reuse_address = False
    daemon_threads = True


def acquire_instance_lock(directory: str):
    """Stop two instances from sharing one index.

    Two writers on the same SQLite file is the more dangerous collision - worse
    than a busy port, because it looks like it is working. The lock is held by
    the OS and released when the process ends, so a crash cannot leave a stale
    lock behind.
    """
    if msvcrt is None:
        return None
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, "app.lock")
    handle = open(path, "a+b")
    if os.path.getsize(path) == 0:
        handle.write(b"0")
        handle.flush()
    handle.seek(0)
    try:
        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        handle.close()
        raise SystemExit(
            f"another instance is already using {directory}.\n"
            "  Close it first, or pass --data-dir to run a separate index."
        )
    return handle


def serve(
    db_path: str,
    config_path: str,
    port: int = 8765,
    open_browser: bool = True,
    verbose: bool = False,
) -> None:
    # One instance per index, taken before anything touches the database. Two
    # writers on one SQLite file is a worse collision than a busy port, because
    # it looks like it is working.
    lock = acquire_instance_lock(os.path.dirname(os.path.abspath(db_path)) or ".")
    service = AppService(db_path, config_path, verbose=verbose)
    token = secrets.token_urlsafe(24)

    server = None
    note = None
    for candidate in (port, 0):
        try:
            server = LoopbackServer(("127.0.0.1", candidate), Handler)
            if candidate != port:
                note = (f"port {port} was already in use, so this run uses "
                        f"{server.server_address[1]} - open the URL printed above")
            break
        except OSError:
            continue
    if server is None:
        service.close()
        raise SystemExit("could not bind a loopback port")

    server.service = service                 # type: ignore[attr-defined]
    server.token = token                     # type: ignore[attr-defined]
    server.verbose = verbose                 # type: ignore[attr-defined]

    url = f"http://127.0.0.1:{server.server_address[1]}/?t={token}"
    # flush: stdout is block-buffered when redirected, and the URL is the one
    # line a user or a supervisor needs to see immediately.
    print("content search is running", flush=True)
    print(f"  url    {url}", flush=True)
    if note:
        print(f"  note   {note}", flush=True)
    print(f"  db     {db_path}", flush=True)
    print(f"  roots  {len(service.active_roots())} enabled", flush=True)
    if getattr(sys.stdout, "isatty", lambda: False)():
        print("  loopback only - Ctrl+C to stop", flush=True)
    else:
        print("  loopback only - stop it with Quit app in the Folders panel", flush=True)

    if service.config["watch"].get("enabled") and service.active_roots():
        try:
            service.start_watch()
            print("  watch  resumed from saved configuration", flush=True)
        except (BusyError, ValueError) as exc:
            print(f"  watch  not resumed: {exc}", flush=True)

    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping...", flush=True)
    finally:
        service.close()
        server.server_close()
        if lock is not None:
            lock.close()
