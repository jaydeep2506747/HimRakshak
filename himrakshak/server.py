"""Local-only HTTP API and dashboard server for HimRakshak."""

from __future__ import annotations

import argparse
import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from .risk import MODEL_ID
from .schema import SchemaValidationError
from .stations import (
    PendingQueueEmptyError,
    PendingQueueFullError,
    StationDisconnectedError,
    StationManager,
    STATION_ROSTER,
)


API_VERSION = 1
MAX_BODY_BYTES = 64 * 1024
PROJECT_ROOT = Path(__file__).resolve().parent.parent


class HimRakshakHandler(BaseHTTPRequestHandler):
    """Serve the dashboard and station-aware local JSON API."""

    server_version = "HimRakshak/1"

    @property
    def manager(self) -> StationManager:
        return self.server.station_manager  # type: ignore[attr-defined]

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status: int, message: str) -> None:
        self._send_json(status, {"error": {"status": status, "message": message}})

    def _read_json(self) -> dict[str, Any] | None:
        length_text = self.headers.get("Content-Length")
        try:
            length = int(length_text) if length_text is not None else -1
        except ValueError:
            self._send_error(HTTPStatus.BAD_REQUEST, "Content-Length must be an integer")
            return None
        if length < 0:
            self._send_error(HTTPStatus.BAD_REQUEST, "Request body length is required")
            return None
        if length > MAX_BODY_BYTES:
            self.rfile.read(min(length, MAX_BODY_BYTES + 1))
            self._send_error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Request body is too large")
            return None
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._send_error(HTTPStatus.BAD_REQUEST, "Request body must be valid JSON")
            return None
        if not isinstance(payload, dict):
            self._send_error(HTTPStatus.BAD_REQUEST, "Request body must be a JSON object")
            return None
        return payload

    def _read_optional_json(self) -> dict[str, Any] | None:
        length_text = self.headers.get("Content-Length")
        if length_text in (None, "", "0"):
            return {}
        return self._read_json()

    def _api_path(self) -> str:
        return urlsplit(self.path).path

    def _discard_request_body(self) -> None:
        length_text = self.headers.get("Content-Length")
        if not length_text:
            return
        try:
            length = int(length_text)
        except ValueError:
            return
        if 0 < length <= MAX_BODY_BYTES:
            self.rfile.read(length)

    @staticmethod
    def _station_id(path: str, suffix: str) -> str | None:
        prefix = "/api/stations/"
        if not path.startswith(prefix) or not path.endswith(suffix):
            return None
        value = path[len(prefix) : -len(suffix)] if suffix else path[len(prefix) :]
        return unquote(value).strip("/") or None

    @staticmethod
    def _fault_action(path: str) -> tuple[str, str] | None:
        prefix = "/api/stations/"
        marker = "/faults/"
        if not path.startswith(prefix) or marker not in path:
            return None
        station, action = path[len(prefix) :].split(marker, 1)
        station = unquote(station).strip("/")
        action = unquote(action).strip("/")
        if not station or not action or "/" in action:
            return None
        return station, action

    def do_GET(self) -> None:
        self._discard_request_body()
        path = self._api_path()
        if path == "/api/health":
            self._send_json(
                HTTPStatus.OK,
                {
                    "status": "ok",
                    "service": "HimRakshak",
                    "api_version": API_VERSION,
                    "model_id": MODEL_ID,
                    "illustrative": True,
                    "station_count": len(STATION_ROSTER),
                },
            )
            return
        if path == "/api/state":
            self._send_json(HTTPStatus.OK, self.manager.legacy_state())
            return
        if path == "/api/stations":
            self._send_json(HTTPStatus.OK, self.manager.overview())
            return
        if self._fault_action(path) is not None:
            self._send_error(HTTPStatus.METHOD_NOT_ALLOWED, "Method not allowed")
            return
        station_id = self._station_id(path, "")
        if station_id is not None:
            try:
                details = self.manager.station_details(station_id)
            except KeyError:
                self._send_error(HTTPStatus.NOT_FOUND, "Unknown station")
                return
            self._send_json(HTTPStatus.OK, {"station": details})
            return
        if path in {"/api/readings", "/api/simulate", "/api/stations"}:
            self._send_error(HTTPStatus.METHOD_NOT_ALLOWED, "Method not allowed")
            return
        if path.startswith("/api/"):
            self._send_error(HTTPStatus.NOT_FOUND, "Unknown API route")
            return
        if path in {"/", "/index.html"}:
            try:
                body = (PROJECT_ROOT / "index.html").read_bytes()
            except OSError:
                self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "Dashboard is unavailable")
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)
            return
        self._send_error(HTTPStatus.NOT_FOUND, "Resource not found")

    def do_POST(self) -> None:
        path = self._api_path()
        fault = self._fault_action(path)
        if fault is not None:
            self._handle_fault(*fault)
            return
        station_id = self._station_id(path, "/simulate")
        operation = "simulate"
        if station_id is None:
            station_id = self._station_id(path, "/readings")
            operation = "readings"
        if station_id is not None:
            self._handle_station_post(station_id, operation)
            return
        if path not in {"/api/readings", "/api/simulate"}:
            if path in {"/api/health", "/api/state", "/api/stations"}:
                self._send_error(HTTPStatus.METHOD_NOT_ALLOWED, "Method not allowed")
            elif path.startswith("/api/"):
                self._send_error(HTTPStatus.NOT_FOUND, "Unknown API route")
            else:
                self._send_error(HTTPStatus.NOT_FOUND, "Resource not found")
            return
        payload = self._read_json()
        if payload is None:
            return
        try:
            if path == "/api/simulate":
                station_id = payload.get("station", payload.get("node_id", "HR-01"))
                if not isinstance(station_id, str):
                    raise ValueError("station must be a string")
                result = self.manager.simulate(station_id, payload.get("scenario"))
            else:
                station_id = payload.get("node_id", "HR-01")
                if not isinstance(station_id, str):
                    raise ValueError("node_id must be a string")
                result = self.manager.ingest(station_id, payload)
            self._send_json(HTTPStatus.OK, result)
        except StationDisconnectedError as exc:
            self._send_error(HTTPStatus.CONFLICT, str(exc))
        except (KeyError, SchemaValidationError, ValueError) as exc:
            self._send_error(
                HTTPStatus.NOT_FOUND if isinstance(exc, KeyError) else HTTPStatus.BAD_REQUEST,
                str(exc),
            )
        except Exception:
            self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "Request could not be processed")

    def _handle_station_post(self, station_id: str, operation: str) -> None:
        try:
            self.manager.get(station_id)
        except KeyError:
            self._send_error(HTTPStatus.NOT_FOUND, "Unknown station")
            return
        payload = self._read_json()
        if payload is None:
            return
        try:
            if operation == "simulate":
                result = self.manager.simulate(station_id, payload.get("scenario"))
            else:
                result = self.manager.ingest(station_id, payload, "api reading")
            self._send_json(HTTPStatus.OK, result)
        except StationDisconnectedError as exc:
            self._send_error(HTTPStatus.CONFLICT, str(exc))
        except (SchemaValidationError, ValueError) as exc:
            self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
        except KeyError:
            self._send_error(HTTPStatus.NOT_FOUND, "Unknown station")
        except Exception:
            self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "Request could not be processed")

    def _handle_fault(self, station_id: str, action: str) -> None:
        try:
            self.manager.get(station_id)
        except KeyError:
            self._send_error(HTTPStatus.NOT_FOUND, "Unknown station")
            return
        payload = (
            self._read_optional_json()
            if action in {"release", "disconnect", "reconnect"}
            else self._read_json()
        )
        if payload is None:
            return
        try:
            if action in {"drop", "delay", "duplicate", "stale", "deliver"}:
                reading = payload.get("reading", payload)
                if not isinstance(reading, dict):
                    raise ValueError("reading must be a JSON object")
                if action == "drop":
                    result = self.manager.drop_packet(station_id, reading)
                elif action == "delay":
                    result = self.manager.delay_packet(station_id, reading)
                else:
                    result = self.manager.deliver_packet(
                        station_id, reading, f"reliability {action}"
                    )
            elif action == "release":
                result = self.manager.release_packet(station_id)
            elif action == "disconnect":
                result = self.manager.disconnect(station_id)
            elif action == "reconnect":
                result = self.manager.reconnect(station_id)
            else:
                self._send_error(HTTPStatus.NOT_FOUND, "Unknown reliability action")
                return
            self._send_json(HTTPStatus.OK, result)
        except PendingQueueFullError as exc:
            self._send_error(HTTPStatus.CONFLICT, str(exc))
        except (
            PendingQueueEmptyError,
            StationDisconnectedError,
            KeyError,
        ) as exc:
            self._send_error(HTTPStatus.CONFLICT, str(exc))
        except SchemaValidationError as exc:
            self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
        except ValueError as exc:
            self._send_error(HTTPStatus.BAD_REQUEST, str(exc))
        except Exception:
            self._send_error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "Reliability request could not be processed",
            )

    def _method_not_allowed(self) -> None:
        length_text = self.headers.get("Content-Length")
        if length_text:
            try:
                self.rfile.read(max(0, min(int(length_text), MAX_BODY_BYTES)))
            except ValueError:
                pass
        self._send_error(HTTPStatus.METHOD_NOT_ALLOWED, "Method not allowed")

    do_PUT = _method_not_allowed
    do_PATCH = _method_not_allowed
    do_DELETE = _method_not_allowed


class HimRakshakServer(ThreadingHTTPServer):
    """Threading HTTP server carrying station manager state."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], seed: int | None = None):
        self.station_manager = StationManager(seed=seed)
        super().__init__(server_address, HimRakshakHandler)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the local HimRakshak API and dashboard.")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765, help="HTTP port (default: 8765)")
    parser.add_argument("--seed", type=int, default=None, help="optional simulator seed")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.host != "127.0.0.1":
        _build_parser().error("--host must remain 127.0.0.1 for local-only service")
    try:
        server = HimRakshakServer(("127.0.0.1", args.port), seed=args.seed)
    except (OSError, ValueError) as exc:
        _build_parser().error(str(exc))
    print(f"HimRakshak local server: http://127.0.0.1:{server.server_address[1]}/")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
