"""gpucall MCP server: a stdio Model Context Protocol tool surface for AI agents.

This is part of the v2.5 Agent-Native Execution Layer. It exposes the governed
gateway API as MCP tools so agent runtimes (Claude Code, Codex, Gemini CLI,
custom agents) can request computation without knowing provider mechanics.

Design constraints:

- Deterministic thin adapter: every tool maps 1:1 to a gateway HTTP endpoint.
  No routing, retry policy, provider choice, or budget decision happens here.
- No new dependency: newline-delimited JSON-RPC 2.0 over stdio using the
  standard library, with httpx (already a core dependency) for gateway calls.
- Secrets never cross the tool boundary: the API key is read from
  GPUCALL_API_KEY and is not echoed in tool output, errors, or logs.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Callable

import httpx

MCP_PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "gpucall"

_TASK_REQUEST_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "task": {"type": "string", "description": "Workload task, e.g. infer or vision."},
        "mode": {"type": "string", "enum": ["sync", "async", "stream"], "description": "Execution mode."},
        "inline_inputs": {"type": "object", "description": "Inline inputs such as {\"prompt\": ...}."},
        "input_refs": {"type": "array", "description": "DataRef inputs for large or binary payloads."},
        "messages": {"type": "array", "description": "Chat-style messages when the recipe accepts them."},
        "metadata": {"type": "object", "description": "Request metadata such as {\"intent\": ...}."},
        "response_format": {"type": "object", "description": "Structured output contract (json_schema preferred)."},
        "idempotency_key": {"type": "string", "description": "Reuse on retries to avoid duplicate billable execution."},
        "max_tokens": {"type": "integer"},
        "timeout_seconds": {"type": "integer"},
    },
    "required": ["task", "mode"],
}

TOOLS: list[dict[str, Any]] = [
    {
        "name": "gpucall_estimate",
        "description": (
            "Non-billable pre-execution estimate. Compiles the governed route for a workload "
            "declaration and returns the selected recipe, tuple chain, and estimated cost without "
            "reserving budget or executing anything. Call this before submitting billable work."
        ),
        "inputSchema": _TASK_REQUEST_SCHEMA,
    },
    {
        "name": "gpucall_submit_task",
        "description": (
            "Submit a governed GPU workload. mode=sync waits for the result; mode=async returns a "
            "job id to poll with gpucall_job_status. gpucall chooses provider, GPU, model, and "
            "endpoint deterministically; callers only declare workload intent."
        ),
        "inputSchema": _TASK_REQUEST_SCHEMA,
    },
    {
        "name": "gpucall_job_status",
        "description": "Poll an async job by job_id. Returns the job record including state and result when terminal.",
        "inputSchema": {
            "type": "object",
            "properties": {"job_id": {"type": "string"}},
            "required": ["job_id"],
        },
    },
    {
        "name": "gpucall_cancel_job",
        "description": "Cancel a non-terminal async job by job_id.",
        "inputSchema": {
            "type": "object",
            "properties": {"job_id": {"type": "string"}},
            "required": ["job_id"],
        },
    },
    {
        "name": "gpucall_readiness",
        "description": (
            "Machine-readable gateway readiness: route-scoped production readiness, provider "
            "evidence freshness, and blockers with owner and next action."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "gpucall_failure_taxonomy",
        "description": (
            "The deterministic failure/retry taxonomy: provider temporary-unavailability codes, "
            "governance failure kinds, retry semantics, and caller actions. Use it to decide the "
            "next step after any failure without asking a human."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
]

_TOOL_ROUTES: dict[str, tuple[str, str]] = {
    "gpucall_job_status": ("GET", "/v2/jobs/{job_id}"),
    "gpucall_cancel_job": ("POST", "/v2/jobs/{job_id}/cancel"),
    "gpucall_readiness": ("GET", "/readyz/details"),
    "gpucall_failure_taxonomy": ("GET", "/v2/failure-taxonomy"),
}


class GPUCallMCPServer:
    """Newline-delimited JSON-RPC 2.0 handler for the MCP stdio transport."""

    def __init__(
        self,
        gateway_url: str | None = None,
        api_key: str | None = None,
        http_client: httpx.Client | None = None,
    ) -> None:
        self.gateway_url = (gateway_url or os.getenv("GPUCALL_GATEWAY_URL") or "").rstrip("/")
        key = api_key if api_key is not None else os.getenv("GPUCALL_API_KEY")
        headers = {"authorization": f"Bearer {key}"} if key else {}
        self._client = http_client or httpx.Client(
            base_url=self.gateway_url or "http://127.0.0.1:18088",
            timeout=float(os.getenv("GPUCALL_MCP_TIMEOUT_SECONDS", "630")),
            headers=headers,
        )
        self.initialized = False

    def handle_message(self, message: dict[str, Any]) -> dict[str, Any] | None:
        method = message.get("method")
        message_id = message.get("id")
        if method == "initialize":
            return self._result(
                message_id,
                {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": SERVER_NAME, "version": _server_version()},
                },
            )
        if method == "notifications/initialized":
            self.initialized = True
            return None
        if method == "ping":
            return self._result(message_id, {})
        if method == "tools/list":
            return self._result(message_id, {"tools": TOOLS})
        if method == "tools/call":
            params = message.get("params") or {}
            return self._tools_call(message_id, params)
        if message_id is None:
            return None
        return self._error(message_id, -32601, f"method not found: {method}")

    def _tools_call(self, message_id: Any, params: dict[str, Any]) -> dict[str, Any]:
        name = str(params.get("name") or "")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return self._error(message_id, -32602, "tool arguments must be an object")
        if not self.gateway_url and self._client.base_url is None:
            return self._error(message_id, -32603, "GPUCALL_GATEWAY_URL is not configured")
        try:
            if name == "gpucall_estimate":
                response = self._client.post("/v2/estimate", json=arguments)
            elif name == "gpucall_submit_task":
                mode = str(arguments.get("mode") or "sync")
                if mode == "stream":
                    return self._tool_error(message_id, "mode=stream is not supported over MCP; use sync or async")
                response = self._client.post(f"/v2/tasks/{mode}", json=arguments)
            elif name in _TOOL_ROUTES:
                http_method, path_template = _TOOL_ROUTES[name]
                if "{job_id}" in path_template:
                    job_id = str(arguments.get("job_id") or "")
                    if not job_id:
                        return self._error(message_id, -32602, "job_id is required")
                    path = path_template.format(job_id=job_id)
                else:
                    path = path_template
                response = self._client.request(http_method, path)
            else:
                return self._error(message_id, -32602, f"unknown tool: {name}")
        except httpx.HTTPError as exc:
            return self._tool_error(
                message_id,
                json.dumps(
                    {
                        "error": "gateway unreachable",
                        "kind": type(exc).__name__,
                        "gateway_url": self.gateway_url or str(self._client.base_url),
                        "caller_action": "check gateway URL, network reachability, and that the gateway is running",
                    },
                    ensure_ascii=False,
                ),
            )
        return self._tool_response(message_id, response)

    def _tool_response(self, message_id: Any, response: httpx.Response) -> dict[str, Any]:
        try:
            payload: Any = response.json()
        except ValueError:
            payload = {"status_code": response.status_code, "body": response.text[:2000]}
        body = json.dumps({"status_code": response.status_code, "body": payload}, ensure_ascii=False, default=str)
        return self._result(
            message_id,
            {
                "content": [{"type": "text", "text": body}],
                "isError": response.status_code >= 400,
            },
        )

    def _tool_error(self, message_id: Any, text: str) -> dict[str, Any]:
        return self._result(message_id, {"content": [{"type": "text", "text": text}], "isError": True})

    @staticmethod
    def _result(message_id: Any, result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": message_id, "result": result}

    @staticmethod
    def _error(message_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}}


def _server_version() -> str:
    from gpucall import __version__

    return __version__


def serve_stdio(
    server: GPUCallMCPServer | None = None,
    *,
    stdin: Any = None,
    stdout: Any = None,
) -> int:
    active = server or GPUCallMCPServer()
    reader = stdin or sys.stdin
    writer = stdout or sys.stdout
    for line in reader:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            writer.write(
                json.dumps({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
                + "\n"
            )
            writer.flush()
            continue
        response = active.handle_message(message)
        if response is not None:
            writer.write(json.dumps(response, ensure_ascii=False) + "\n")
            writer.flush()
    return 0


def main(argv: list[str] | None = None) -> int:
    return serve_stdio()


if __name__ == "__main__":
    raise SystemExit(main())
