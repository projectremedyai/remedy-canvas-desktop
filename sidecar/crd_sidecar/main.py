"""Sidecar entrypoint — speaks JSON-RPC 2.0 over stdio with the Tauri shell."""

from __future__ import annotations

import sys

from crd_sidecar.ipc.rpc import Dispatcher, serve_stdio
from crd_sidecar.ipc.handlers import register_builtin_handlers
from crd_sidecar.telemetry import capture_exception, init_crash_reporting


def build_dispatcher() -> Dispatcher:
    dispatcher = Dispatcher()
    register_builtin_handlers(dispatcher)
    return dispatcher


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    init_crash_reporting()
    dispatcher = build_dispatcher()

    if argv and argv[0] == "--list-methods":
        for name in sorted(dispatcher.method_names()):
            print(name)
        return 0

    try:
        serve_stdio(dispatcher)
    except BaseException as exc:  # noqa: BLE001
        capture_exception(exc, component="sidecar", phase="serve_stdio")
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
