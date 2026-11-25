"""The resident capture service: a named pipe for the command line and an optional local HTTP endpoint."""

from __future__ import annotations

import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from . import __version__, pipe_name
from .capture import CaptureError, capture_png, take_screenshot
from .config import Area, Settings

MAX_REQUEST = 64 * 1024
UI_COMMANDS = ("show", "select", "quit")


class BusyError(CaptureError):
    pass


class CaptureService(QObject):
    captured = Signal(str, str, str)  # saved file path, source (shortcut, cli, http, ...), area
    failed = Signal(str, str)  # error message, source
    command = Signal(str)  # one of UI_COMMANDS, for the window

    def __init__(self, settings: Settings, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        # When set, each saved capture starts a transaction and further captures are refused until release().
        self.hold_after_capture = False
        self._busy = False
        self._busy_lock = threading.Lock()
        self._server = QLocalServer(self)
        self._server.newConnection.connect(self._on_new_connection)
        self._buffers: dict[QLocalSocket, bytes] = {}
        self._http: ThreadingHTTPServer | None = None
        self.http_error = ""

    # -- lifecycle ------------------------------------------------------------

    def start(self) -> bool:
        """Listen on the pipe; False if another instance already owns it."""
        if not self._server.listen(pipe_name()):
            logging.error("Cannot listen on pipe %s: %s", pipe_name(), self._server.errorString())
            return False
        logging.info("Listening on pipe \\\\.\\pipe\\%s", pipe_name())
        self.restart_http()
        return True

    def stop(self) -> None:
        self._stop_http()
        self._server.close()

    def restart_http(self) -> None:
        self._stop_http()
        self.http_error = ""
        if not self.settings.http_enabled:
            return
        port = self.settings.http_port
        try:
            server = ThreadingHTTPServer(("127.0.0.1", port), _HttpHandler)
        except OSError as exc:
            self.http_error = f"Port {port} is not available ({exc.strerror or exc})"
            logging.error("HTTP endpoint: %s", self.http_error)
            return
        server.daemon_threads = True
        server.service = self  # type: ignore[attr-defined]
        self._http = server
        threading.Thread(target=server.serve_forever, name="http", daemon=True).start()
        logging.info("HTTP endpoint on http://127.0.0.1:%d/", port)

    def _stop_http(self) -> None:
        if self._http:
            self._http.shutdown()
            self._http.server_close()
            self._http = None

    @property
    def http_running(self) -> bool:
        return self._http is not None

    # -- commands -------------------------------------------------------------

    @property
    def busy(self) -> bool:
        return self._busy

    def release(self) -> None:
        """Allow captures again after a transaction has finished."""
        self._busy = False

    def capture(self, area: Area | None = None, output: str | None = None, source: str = "app") -> str:
        """Save a screenshot; raises BusyError while a transaction is in progress (avoids double charges)."""
        with self._busy_lock:
            if self._busy:
                logging.warning("Capture from %s refused: a transaction is in progress", source)
                raise BusyError("A transaction is in progress. Try again when it has finished.")
            area = area or self.settings.area
            try:
                path = str(take_screenshot(self.settings, area, output))
            except (CaptureError, OSError) as exc:
                self.failed.emit(str(exc), source)
                raise
            if self.hold_after_capture:
                self._busy = True
        self.captured.emit(path, source, str(area))
        return path

    def handle(self, message: dict) -> dict:
        cmd = message.get("cmd", "capture")
        if cmd == "ping":
            return {"ok": True, "version": __version__, "pid": os.getpid(), "busy": self._busy}
        if cmd in UI_COMMANDS:
            self.command.emit(cmd)
            return {"ok": True}
        if cmd != "capture":
            return {"ok": False, "error": f"Unknown command {cmd!r}"}
        try:
            area = Area.parse(message["area"]) if message.get("area") else None
            return {"ok": True, "path": self.capture(area, message.get("output") or None, "cli")}
        except (ValueError, CaptureError, OSError) as exc:
            return {"ok": False, "error": str(exc)}

    # -- named pipe -----------------------------------------------------------

    def _on_new_connection(self) -> None:
        while self._server.hasPendingConnections():
            socket = self._server.nextPendingConnection()
            self._buffers[socket] = b""
            socket.readyRead.connect(lambda s=socket: self._on_ready_read(s))
            socket.disconnected.connect(lambda s=socket: self._drop(s))

    def _drop(self, socket: QLocalSocket) -> None:
        self._buffers.pop(socket, None)
        socket.deleteLater()

    def _on_ready_read(self, socket: QLocalSocket) -> None:
        if socket not in self._buffers:
            return
        data = self._buffers[socket] + bytes(socket.readAll().data())
        if b"\n" not in data and len(data) < MAX_REQUEST:
            self._buffers[socket] = data
            return
        del self._buffers[socket]
        try:
            message = json.loads(data.split(b"\n", 1)[0])
            reply = self.handle(message) if isinstance(message, dict) else {"ok": False, "error": "Bad request"}
        except ValueError:
            reply = {"ok": False, "error": "Bad request"}
        socket.write(json.dumps(reply).encode("utf-8") + b"\n")
        socket.flush()
        socket.disconnectFromServer()


class _HttpHandler(BaseHTTPRequestHandler):
    server_version = f"SwiftScreenReader/{__version__}"

    def do_GET(self) -> None:  # noqa: N802
        # Refuse browser requests (a web page could otherwise trigger captures) and DNS rebinding.
        host = (self.headers.get("Host") or "").split(":")[0].lower()
        if self.headers.get("Origin") or self.headers.get("Sec-Fetch-Site") or host not in ("127.0.0.1", "localhost"):
            self._send_json(403, {"ok": False, "error": "Forbidden"})
            return

        service: CaptureService = self.server.service  # type: ignore[attr-defined]
        url = urlparse(self.path)
        query = parse_qs(url.query)
        try:
            area = Area.parse(query["area"][0]) if query.get("area") else None
        except ValueError as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})
            return

        if url.path in ("/", "/status"):
            settings = service.settings
            self._send_json(200, {
                "ok": True,
                "version": __version__,
                "area": str(settings.area) if settings.area else None,
                "folder": str(settings.screenshots_dir()),
                "busy": service.busy,
            })
        elif url.path == "/capture":
            try:
                self._send_json(200, {"ok": True, "path": service.capture(area, source="http")})
            except BusyError as exc:
                self._send_json(409, {"ok": False, "error": str(exc)})
            except (CaptureError, OSError) as exc:
                self._send_json(500, {"ok": False, "error": str(exc)})
        elif url.path == "/capture.png":
            area = area or service.settings.area
            if area is None:
                self._send_json(409, {"ok": False, "error": "No capture area selected yet."})
                return
            try:
                data = capture_png(area)
            except CaptureError as exc:
                self._send_json(500, {"ok": False, "error": str(exc)})
                return
            self._send(200, "image/png", data)
        else:
            self._send_json(404, {"ok": False, "error": "Not found"})

    def _send_json(self, status: int, data: dict) -> None:
        self._send(status, "application/json", json.dumps(data).encode("utf-8"))

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:  # noqa: A002
        logging.debug("HTTP %s - %s", self.address_string(), format % args)
