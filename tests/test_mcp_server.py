from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from gpucall.app import create_app
from gpucall.mcp_server import GPUCallMCPServer, TOOLS, serve_stdio


def copy_config(tmp_path: Path) -> Path:
    source = Path(__file__).resolve().parent / "fixtures" / "config"
    root = tmp_path / "config"
    root.mkdir(parents=True, exist_ok=True)
    for subdir in ["tuples", "surfaces", "workers", "recipes", "models", "engines", "tenants", "accounts"]:
        (root / subdir).mkdir(parents=True, exist_ok=True)
    for path in source.rglob("*.yml"):
        target = root / path.relative_to(source)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    return root


@pytest.fixture(autouse=True)
def isolate_gateway_env(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("GPUCALL_ALLOW_FAKE_AUTO_TUPLES", "1")
    monkeypatch.setenv("GPUCALL_ALLOW_UNAUTHENTICATED", "1")
    credentials = tmp_path / "credentials.yml"
    credentials.write_text("version: 1\nproviders: {}\n", encoding="utf-8")
    monkeypatch.setenv("GPUCALL_CREDENTIALS", str(credentials))
    monkeypatch.delenv("GPUCALL_API_KEY", raising=False)
    monkeypatch.delenv("GPUCALL_API_KEYS", raising=False)


@pytest.fixture()
def mcp_server(tmp_path):
    app = create_app(copy_config(tmp_path))
    with TestClient(app, base_url="http://gateway.test") as http_client:
        yield GPUCallMCPServer(gateway_url="http://gateway.test", http_client=http_client)


def _call(server: GPUCallMCPServer, method: str, params: dict | None = None, message_id: int = 1) -> dict:
    return server.handle_message({"jsonrpc": "2.0", "id": message_id, "method": method, "params": params or {}})


def _tool_body(response: dict) -> dict:
    assert "error" not in response, response
    content = response["result"]["content"]
    return json.loads(content[0]["text"])


def test_initialize_and_tools_list(mcp_server) -> None:
    init = _call(mcp_server, "initialize")
    assert init["result"]["serverInfo"]["name"] == "gpucall"
    assert init["result"]["protocolVersion"]

    assert mcp_server.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert mcp_server.initialized is True

    listing = _call(mcp_server, "tools/list")
    names = {tool["name"] for tool in listing["result"]["tools"]}
    assert names == {
        "gpucall_estimate",
        "gpucall_submit_task",
        "gpucall_job_status",
        "gpucall_cancel_job",
        "gpucall_readiness",
        "gpucall_failure_taxonomy",
    }
    for tool in TOOLS:
        assert tool["description"]
        assert tool["inputSchema"]["type"] == "object"


def test_estimate_tool_returns_plan(mcp_server) -> None:
    response = _call(
        mcp_server,
        "tools/call",
        {"name": "gpucall_estimate", "arguments": {"task": "infer", "mode": "sync"}},
    )
    body = _tool_body(response)

    assert response["result"]["isError"] is False
    assert body["status_code"] == 200
    assert body["body"]["phase"] == "estimate"
    assert body["body"]["billable"] is False


def test_submit_sync_task_tool(mcp_server) -> None:
    response = _call(
        mcp_server,
        "tools/call",
        {"name": "gpucall_submit_task", "arguments": {"task": "infer", "mode": "sync"}},
    )
    body = _tool_body(response)

    assert body["status_code"] == 200
    assert body["body"]["result"]["kind"] == "inline"


def test_async_submit_status_cancel_flow(mcp_server) -> None:
    submitted = _tool_body(
        _call(
            mcp_server,
            "tools/call",
            {"name": "gpucall_submit_task", "arguments": {"task": "infer", "mode": "async"}},
        )
    )
    assert submitted["status_code"] in (200, 202)
    job_id = submitted["body"]["job_id"]

    status = _tool_body(
        _call(mcp_server, "tools/call", {"name": "gpucall_job_status", "arguments": {"job_id": job_id}})
    )
    assert status["status_code"] == 200
    assert status["body"]["job_id"] == job_id

    cancel = _tool_body(
        _call(mcp_server, "tools/call", {"name": "gpucall_cancel_job", "arguments": {"job_id": job_id}})
    )
    assert cancel["status_code"] == 200
    assert cancel["body"]["job_id"] == job_id


def test_failure_taxonomy_tool(mcp_server) -> None:
    body = _tool_body(_call(mcp_server, "tools/call", {"name": "gpucall_failure_taxonomy", "arguments": {}}))

    assert body["status_code"] == 200
    assert "PROVIDER_CAPACITY_UNAVAILABLE" in body["body"]["provider_errors"]


def test_readiness_tool(mcp_server) -> None:
    body = _tool_body(_call(mcp_server, "tools/call", {"name": "gpucall_readiness", "arguments": {}}))

    assert body["status_code"] in (200, 503)
    assert isinstance(body["body"], dict)


def test_unknown_tool_and_method_errors(mcp_server) -> None:
    unknown_tool = _call(mcp_server, "tools/call", {"name": "not_a_tool", "arguments": {}})
    assert unknown_tool["error"]["code"] == -32602

    unknown_method = _call(mcp_server, "definitely/not-a-method")
    assert unknown_method["error"]["code"] == -32601


def test_stream_mode_is_rejected(mcp_server) -> None:
    response = _call(
        mcp_server,
        "tools/call",
        {"name": "gpucall_submit_task", "arguments": {"task": "infer", "mode": "stream"}},
    )
    assert response["result"]["isError"] is True


def test_gateway_unreachable_is_bounded_tool_error() -> None:
    server = GPUCallMCPServer(gateway_url="http://127.0.0.1:1", api_key=None)
    response = _call(
        server,
        "tools/call",
        {"name": "gpucall_failure_taxonomy", "arguments": {}},
    )
    assert response["result"]["isError"] is True
    body = json.loads(response["result"]["content"][0]["text"])
    assert body["error"] == "gateway unreachable"
    assert body["caller_action"]


def test_serve_stdio_round_trip(mcp_server) -> None:
    stdin = io.StringIO(
        "\n".join(
            [
                json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
                json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
                json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}),
                "not-json",
            ]
        )
        + "\n"
    )
    stdout = io.StringIO()

    assert serve_stdio(mcp_server, stdin=stdin, stdout=stdout) == 0
    lines = [json.loads(line) for line in stdout.getvalue().splitlines() if line.strip()]

    assert lines[0]["id"] == 1
    assert lines[1]["id"] == 2
    assert lines[2]["error"]["code"] == -32700


def test_api_key_never_appears_in_tool_output(tmp_path) -> None:
    app = create_app(copy_config(tmp_path))
    with TestClient(app, base_url="http://gateway.test", headers={"authorization": "Bearer sk-super-secret-key"}) as http_client:
        server = GPUCallMCPServer(gateway_url="http://gateway.test", http_client=http_client)
        listing = _call(server, "tools/list")
        taxonomy = _call(server, "tools/call", {"name": "gpucall_failure_taxonomy", "arguments": {}})

    for response in (listing, taxonomy):
        assert "sk-super-secret-key" not in json.dumps(response)
