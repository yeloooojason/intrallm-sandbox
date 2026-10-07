"""Python client for the sandbox API, plus an LLM tool adapter for the IntraLLM agent.

Example::

    from intrallm_sandbox.client import SandboxClient, AgentToolkit

    client = SandboxClient("http://sandbox.intra:8080", token="isb_...")
    kit = AgentToolkit(client, owner="alice")

    # 1. pass kit.tools to the LLM (OpenAI-compatible `tools=` parameter)
    # 2. for each tool call the model emits:
    result_json = kit.call(tool_call.function.name, tool_call.function.arguments)
"""

from __future__ import annotations

import json
from typing import Any

import httpx


class SandboxAPIError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"{status}: {detail}")
        self.status = status
        self.detail = detail


class SandboxClient:
    def __init__(self, base_url: str, token: str, timeout: float = 660.0) -> None:
        self._http = httpx.Client(
            base_url=base_url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _req(self, method: str, path: str, **kw) -> Any:
        r = self._http.request(method, path, **kw)
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except ValueError:
                detail = r.text
            raise SandboxAPIError(r.status_code, str(detail))
        return r.json()

    def create(self, owner: str | None = None, **kw) -> dict:
        return self._req("POST", "/api/v1/sandboxes", json={"owner": owner, **kw})

    def list(self, active: bool = True, owner: str | None = None) -> list[dict]:
        params = {"active": active}
        if owner:
            params["owner"] = owner
        return self._req("GET", "/api/v1/sandboxes", params=params)

    def get(self, sandbox_id: str) -> dict:
        return self._req("GET", f"/api/v1/sandboxes/{sandbox_id}")

    def exec(self, sandbox_id: str, command: str, timeout: int | None = None, workdir: str | None = None) -> dict:
        body = {"command": command, "timeout": timeout, "workdir": workdir}
        return self._req("POST", f"/api/v1/sandboxes/{sandbox_id}/exec", json=body)

    def write_file(self, sandbox_id: str, path: str, content: str) -> dict:
        return self._req("PUT", f"/api/v1/sandboxes/{sandbox_id}/files", json={"path": path, "content": content})

    def read_file(self, sandbox_id: str, path: str) -> dict:
        return self._req("GET", f"/api/v1/sandboxes/{sandbox_id}/files", params={"path": path})

    def ls(self, sandbox_id: str, path: str = "/workspace") -> list[dict]:
        return self._req("GET", f"/api/v1/sandboxes/{sandbox_id}/ls", params={"path": path})

    def extend(self, sandbox_id: str, seconds: int = 3600) -> dict:
        return self._req("POST", f"/api/v1/sandboxes/{sandbox_id}/extend", json={"seconds": seconds})

    def destroy(self, sandbox_id: str) -> dict:
        return self._req("DELETE", f"/api/v1/sandboxes/{sandbox_id}")

    def tools(self) -> list[dict]:
        return self._req("GET", "/api/v1/agent/tools")

    def invoke(self, tool: str, arguments: dict, owner: str | None = None) -> dict:
        return self._req("POST", "/api/v1/agent/invoke", json={"tool": tool, "arguments": arguments, "owner": owner})


class AgentToolkit:
    """Adapts the sandbox API to an LLM tool-calling loop, scoped to one end user."""

    def __init__(self, client: SandboxClient, owner: str) -> None:
        self.client = client
        self.owner = owner
        self._tools: list[dict] | None = None

    @property
    def tools(self) -> list[dict]:
        if self._tools is None:
            self._tools = self.client.tools()
        return self._tools

    def call(self, name: str, arguments: str | dict) -> str:
        """Run one tool call and return a JSON string to feed back as the tool message."""
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments or "{}")
            except json.JSONDecodeError as e:
                return json.dumps({"ok": False, "error": f"arguments are not valid JSON: {e}"})
        result = self.client.invoke(name, arguments, owner=self.owner)
        return json.dumps(result, ensure_ascii=False)
