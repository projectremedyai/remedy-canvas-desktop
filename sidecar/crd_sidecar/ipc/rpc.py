"""Minimal JSON-RPC 2.0 dispatcher that reads newline-delimited messages from stdin.

Phase 5 extends this with server-initiated progress notifications: a
handler can `get_current_emitter()` to stream `job.progress`-style events
back to the client without blocking the response. The emitter writes to
the same stdout the dispatcher reads responses from — OK because this
dispatcher is strictly sequential (one request at a time).
"""

from __future__ import annotations

import contextvars
import json
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import IO, Any

Handler = Callable[[dict[str, Any]], Any]


@dataclass
class RpcError(Exception):
    code: int
    message: str
    data: Any = None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            payload["data"] = self.data
        return payload


# JSON-RPC 2.0 error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


class Dispatcher:
    """Maps method names to handlers."""

    def __init__(self) -> None:
        self._handlers: dict[str, Handler] = {}

    def register(self, method: str, handler: Handler) -> None:
        if method in self._handlers:
            raise ValueError(f"Method already registered: {method}")
        self._handlers[method] = handler

    def method_names(self) -> Iterable[str]:
        return self._handlers.keys()

    def invoke(self, method: str, params: dict[str, Any]) -> Any:
        handler = self._handlers.get(method)
        if handler is None:
            raise RpcError(METHOD_NOT_FOUND, f"Method not found: {method}")
        return handler(params)


class ProgressEmitter:
    """Writes JSON-RPC 2.0 notifications to the dispatcher's output stream.

    Handlers that want to stream progress look it up via
    `get_current_emitter()` — no handler-signature changes needed.
    """

    def __init__(self, writer: Callable[[str], None]) -> None:
        self._writer = writer

    def emit(self, method: str, params: dict[str, Any]) -> None:
        message = json.dumps({"jsonrpc": "2.0", "method": method, "params": params})
        self._writer(message + "\n")


_current_emitter: contextvars.ContextVar[ProgressEmitter | None] = contextvars.ContextVar(
    "current_emitter", default=None
)


def get_current_emitter() -> ProgressEmitter | None:
    """Return the ProgressEmitter tied to the currently-executing request,
    or None if none has been set (unit tests, direct invokes, etc.).
    """
    return _current_emitter.get()


def _format_response(request_id: Any, result: Any) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": request_id, "result": result})


def _format_error(request_id: Any, error: RpcError) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": request_id, "error": error.to_dict()})


def handle_line(dispatcher: Dispatcher, line: str) -> str | None:
    """Process a single newline-delimited JSON-RPC request.

    Returns the response string, or None for notifications (requests without id).
    """
    try:
        msg = json.loads(line)
    except json.JSONDecodeError as exc:
        return _format_error(None, RpcError(PARSE_ERROR, f"Invalid JSON: {exc}"))

    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
        return _format_error(msg.get("id") if isinstance(msg, dict) else None,
                              RpcError(INVALID_REQUEST, "Not a JSON-RPC 2.0 request"))

    method = msg.get("method")
    if not isinstance(method, str):
        return _format_error(msg.get("id"), RpcError(INVALID_REQUEST, "Missing method"))

    params = msg.get("params") or {}
    if not isinstance(params, dict):
        return _format_error(msg.get("id"), RpcError(INVALID_PARAMS, "params must be an object"))

    request_id = msg.get("id")  # notifications have no id
    is_notification = "id" not in msg

    try:
        result = dispatcher.invoke(method, params)
    except RpcError as exc:
        return None if is_notification else _format_error(request_id, exc)
    except Exception as exc:  # noqa: BLE001 — last-resort boundary
        return None if is_notification else _format_error(
            request_id, RpcError(INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")
        )

    return None if is_notification else _format_response(request_id, result)


def serve_stdio(
    dispatcher: Dispatcher,
    *,
    stdin: IO[str] | None = None,
    stdout: IO[str] | None = None,
) -> None:
    """Blocking read loop over stdin. Exits when stdin closes (EOF).

    Wraps each request in a ProgressEmitter context so long-running
    handlers can push `job.progress`-style notifications without
    breaking the request/response flow.
    """
    stdin = stdin or sys.stdin
    stdout = stdout or sys.stdout

    def _write(line: str) -> None:
        stdout.write(line)
        stdout.flush()

    emitter = ProgressEmitter(_write)

    for raw in stdin:
        line = raw.strip()
        if not line:
            continue
        token = _current_emitter.set(emitter)
        try:
            response = handle_line(dispatcher, line)
        finally:
            _current_emitter.reset(token)
        if response is not None:
            _write(response + "\n")
