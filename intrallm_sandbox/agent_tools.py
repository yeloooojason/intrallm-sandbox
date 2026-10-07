"""Tool definitions that let the IntraLLM agent drive sandboxes.

The schemas use the OpenAI-compatible function-calling format, which most
self-hosted LLM servers (vLLM, Ollama, TGI, ...) accept. The agent can either:

* fetch them from ``GET /api/v1/agent/tools`` and invoke through
  ``POST /api/v1/agent/invoke`` (language-agnostic), or
* use :class:`intrallm_sandbox.client.AgentToolkit` in Python.
"""

from __future__ import annotations

import base64
from dataclasses import asdict
from typing import Any

from .auth import Principal
from .manager import SandboxError, SandboxManager


def _fn(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


SANDBOX_ID = {"type": "string", "description": "Sandbox id returned by sandbox_create, e.g. sbx-1a2b3c4d5e6f"}

TOOLS: list[dict] = [
    _fn(
        "sandbox_create",
        "Create an isolated Linux sandbox (container) to run code or shell commands for the current user. "
        "Reuse an existing sandbox for the same task instead of creating a new one.",
        {
            "name": {"type": "string", "description": "Short human readable name for the task"},
            "image": {"type": "string", "description": "Container image; omit for the default"},
            "cpu": {"type": "number", "description": "CPU cores, e.g. 1"},
            "memory_mb": {"type": "integer", "description": "Memory limit in MB, e.g. 1024"},
            "ttl_seconds": {"type": "integer", "description": "Lifetime in seconds before auto-destroy"},
        },
        [],
    ),
    _fn(
        "sandbox_exec",
        "Run a shell command inside a sandbox (working dir /workspace). Returns exit_code, stdout and stderr.",
        {
            "sandbox_id": SANDBOX_ID,
            "command": {"type": "string", "description": "Shell command, run with sh -c"},
            "timeout": {"type": "integer", "description": "Seconds before the command is killed"},
            "workdir": {"type": "string", "description": "Working directory, default /workspace"},
        },
        ["sandbox_id", "command"],
    ),
    _fn(
        "sandbox_write_file",
        "Create or overwrite a text file inside a sandbox.",
        {
            "sandbox_id": SANDBOX_ID,
            "path": {"type": "string", "description": "File path, relative paths are under /workspace"},
            "content": {"type": "string", "description": "Full file content"},
        },
        ["sandbox_id", "path", "content"],
    ),
    _fn(
        "sandbox_read_file",
        "Read a text file from a sandbox.",
        {"sandbox_id": SANDBOX_ID, "path": {"type": "string", "description": "File path"}},
        ["sandbox_id", "path"],
    ),
    _fn(
        "sandbox_list_files",
        "List the entries of a directory inside a sandbox.",
        {"sandbox_id": SANDBOX_ID, "path": {"type": "string", "description": "Directory, default /workspace"}},
        ["sandbox_id"],
    ),
    _fn(
        "sandbox_list",
        "List the current user's active sandboxes.",
        {},
        [],
    ),
    _fn(
        "sandbox_destroy",
        "Destroy a sandbox when the task is finished, releasing its resources.",
        {"sandbox_id": SANDBOX_ID},
        ["sandbox_id"],
    ),
]

TOOL_NAMES = {t["function"]["name"] for t in TOOLS}


def invoke(manager: SandboxManager, principal: Principal, owner: str | None, tool: str, args: dict) -> dict[str, Any]:
    """Execute one tool call. Errors are returned (not raised) so the LLM can react to them."""
    if tool not in TOOL_NAMES:
        return {"ok": False, "error": f"unknown tool {tool!r}"}
    try:
        return {"ok": True, "result": _dispatch(manager, principal, owner, tool, args or {})}
    except SandboxError as e:
        return {"ok": False, "error": e.message}
    except KeyError as e:
        return {"ok": False, "error": f"missing argument: {e.args[0]}"}
    except (TypeError, AttributeError) as e:
        return {"ok": False, "error": f"bad arguments: {e}"}


def _brief(sb: dict) -> dict:
    keys = ("id", "name", "owner", "status", "image", "cpu", "memory_mb", "expires_at")
    return {k: sb[k] for k in keys}


def _dispatch(m: SandboxManager, p: Principal, owner: str | None, tool: str, a: dict) -> Any:
    if tool == "sandbox_create":
        allowed = {"name", "image", "cpu", "memory_mb", "ttl_seconds"}
        return _brief(m.create(p, owner=owner, **{k: v for k, v in a.items() if k in allowed}))
    if tool == "sandbox_list":
        return [_brief(sb) for sb in m.list(p, active_only=True, owner=owner)]
    sid = a.get("sandbox_id")
    if not sid:
        raise SandboxError("sandbox_id is required")
    # An agent acting for user X must not touch user Y's sandbox even if it created it.
    if owner and m.get(p, sid)["owner"] != owner:
        raise SandboxError(f"sandbox {sid} not found")
    if tool == "sandbox_exec":
        return asdict(m.exec(p, sid, a["command"], a.get("timeout"), a.get("workdir")))
    if tool == "sandbox_write_file":
        data = a["content"].encode()
        m.write_file(p, sid, a["path"], data)
        return {"path": a["path"], "bytes": len(data)}
    if tool == "sandbox_read_file":
        raw = m.read_file(p, sid, a["path"])
        try:
            return {"path": a["path"], "content": raw.decode("utf-8")}
        except UnicodeDecodeError:
            return {"path": a["path"], "content_base64": base64.b64encode(raw).decode(), "binary": True}
    if tool == "sandbox_list_files":
        return [asdict(e) for e in m.list_files(p, sid, a.get("path") or "/workspace")]
    if tool == "sandbox_destroy":
        return _brief(m.destroy(p, sid, reason="destroyed by agent"))
    raise SandboxError(f"unhandled tool {tool}")
