from __future__ import annotations

import json

from crd_sidecar.ipc.handlers import register_builtin_handlers
from crd_sidecar.ipc.rpc import Dispatcher, handle_line


def _response(dispatcher: Dispatcher, payload: dict) -> dict:
    raw = handle_line(dispatcher, json.dumps(payload))
    assert raw is not None
    return json.loads(raw)


def test_builtin_ping_handler():
    dispatcher = Dispatcher()
    register_builtin_handlers(dispatcher)

    response = _response(
        dispatcher,
        {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {}},
    )

    assert response["result"] == {
        "ok": True,
        "service": "remedy-canvas-desktop-sidecar",
    }


def test_analyze_html_runs_without_external_services():
    dispatcher = Dispatcher()
    register_builtin_handlers(dispatcher)

    response = _response(
        dispatcher,
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "analyze_html",
            "params": {
                "html": "<h2>Overview</h2><p><img src='hero.png'></p>",
                "identifier": "smoke-page",
            },
        },
    )

    result = response["result"]
    assert result["rule_count"] > 0
    assert result["issue_count"] > 0
    assert any(issue["rule_id"] == "IMG001" for issue in result["issues"])


def test_rpc_reports_invalid_params():
    dispatcher = Dispatcher()
    register_builtin_handlers(dispatcher)

    response = _response(
        dispatcher,
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "analyze_html",
            "params": {"html": None},
        },
    )

    assert response["error"]["code"] == -32602
