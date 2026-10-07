"""FastAPI application: REST API, agent tool endpoint, Prometheus metrics, dashboard."""

from __future__ import annotations

import base64
import logging
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import __version__, agent_tools, auth
from .auth import Principal
from .config import Settings, get_settings
from .db import Database
from .manager import SandboxError, SandboxManager
from .runtime import Runtime, build_runtime

STATIC = Path(__file__).parent / "static"


# --- request models ---------------------------------------------------------
class CreateSandbox(BaseModel):
    owner: str | None = Field(None, description="User the sandbox is allocated to (agents/admins only)")
    name: str | None = None
    image: str | None = None
    cpu: float | None = None
    memory_mb: int | None = None
    ttl_seconds: int | None = None
    env: dict[str, str] | None = None
    labels: dict[str, str] | None = None


class ExecRequest(BaseModel):
    command: str
    timeout: int | None = None
    workdir: str | None = None
    env: dict[str, str] | None = None


class WriteFile(BaseModel):
    path: str
    content: str | None = None
    content_base64: str | None = None


class Extend(BaseModel):
    seconds: int = Field(3600, gt=0)


class Reassign(BaseModel):
    owner: str


class CreateToken(BaseModel):
    principal: str
    role: str = "user"
    description: str = ""


class InvokeTool(BaseModel):
    tool: str
    arguments: dict[str, Any] = {}
    owner: str | None = Field(None, description="End user the agent is acting for")


def create_app(settings: Settings | None = None, runtime: Runtime | None = None) -> FastAPI:
    settings = settings or get_settings()
    db = Database(settings.db_path)
    manager = SandboxManager(settings, db, runtime or build_runtime(settings))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.background:
            manager.start()
        yield
        manager.shutdown()

    app = FastAPI(title="IntraLLM Sandbox", version=__version__, lifespan=lifespan)
    app.state.manager = manager
    app.state.db = db

    @app.exception_handler(SandboxError)
    async def _sandbox_error(_: Request, exc: SandboxError):
        return JSONResponse({"detail": exc.message}, status_code=exc.status_code)

    def principal(authorization: str = Header("")) -> Principal:
        token = authorization.removeprefix("Bearer ").strip()
        p = auth.resolve(token, settings.admin_token, db)
        if p is None:
            raise HTTPException(401, "invalid or missing bearer token")
        return p

    def admin(p: Principal = Depends(principal)) -> Principal:
        if not p.is_admin:
            raise HTTPException(403, "admin role required")
        return p

    # --- meta ---------------------------------------------------------------
    @app.get("/healthz")
    def healthz():
        return {"ok": True, "runtime": manager.runtime.name, "version": __version__}

    @app.get("/api/v1/whoami")
    def whoami(p: Principal = Depends(principal)):
        return {"name": p.name, "role": p.role}

    # --- sandboxes ----------------------------------------------------------
    @app.post("/api/v1/sandboxes", status_code=201)
    def create_sandbox(body: CreateSandbox, p: Principal = Depends(principal)):
        return manager.view(manager.create(p, **body.model_dump()))

    @app.get("/api/v1/sandboxes")
    def list_sandboxes(
        active: bool = Query(False, description="Only creating/running sandboxes"),
        owner: str | None = None,
        p: Principal = Depends(principal),
    ):
        return [manager.view(sb) for sb in manager.list(p, active_only=active, owner=owner)]

    @app.get("/api/v1/sandboxes/{sandbox_id}")
    def get_sandbox(sandbox_id: str, p: Principal = Depends(principal)):
        return manager.view(manager.get(p, sandbox_id))

    @app.delete("/api/v1/sandboxes/{sandbox_id}")
    def delete_sandbox(sandbox_id: str, p: Principal = Depends(principal)):
        return manager.view(manager.destroy(p, sandbox_id, reason=f"stopped by {p.name}"))

    @app.post("/api/v1/sandboxes/{sandbox_id}/exec")
    def exec_sandbox(sandbox_id: str, body: ExecRequest, p: Principal = Depends(principal)):
        return asdict(manager.exec(p, sandbox_id, body.command, body.timeout, body.workdir, body.env))

    @app.put("/api/v1/sandboxes/{sandbox_id}/files")
    def write_file(sandbox_id: str, body: WriteFile, p: Principal = Depends(principal)):
        if body.content_base64 is not None:
            data = base64.b64decode(body.content_base64)
        else:
            data = (body.content or "").encode()
        manager.write_file(p, sandbox_id, body.path, data)
        return {"path": body.path, "bytes": len(data)}

    @app.get("/api/v1/sandboxes/{sandbox_id}/files")
    def read_file(sandbox_id: str, path: str, p: Principal = Depends(principal)):
        data = manager.read_file(p, sandbox_id, path)
        try:
            return {"path": path, "content": data.decode("utf-8")}
        except UnicodeDecodeError:
            return {"path": path, "content_base64": base64.b64encode(data).decode()}

    @app.get("/api/v1/sandboxes/{sandbox_id}/ls")
    def list_files(sandbox_id: str, path: str = "/workspace", p: Principal = Depends(principal)):
        return [asdict(e) for e in manager.list_files(p, sandbox_id, path)]

    @app.post("/api/v1/sandboxes/{sandbox_id}/extend")
    def extend(sandbox_id: str, body: Extend, p: Principal = Depends(principal)):
        return manager.view(manager.extend(p, sandbox_id, body.seconds))

    @app.post("/api/v1/sandboxes/{sandbox_id}/reassign")
    def reassign(sandbox_id: str, body: Reassign, p: Principal = Depends(principal)):
        return manager.view(manager.reassign(p, sandbox_id, body.owner))

    @app.get("/api/v1/sandboxes/{sandbox_id}/metrics")
    def sandbox_metrics(sandbox_id: str, p: Principal = Depends(principal)):
        manager.get(p, sandbox_id)
        return manager.metrics.sandbox_history(sandbox_id)

    @app.get("/api/v1/sandboxes/{sandbox_id}/audit")
    def sandbox_audit(sandbox_id: str, limit: int = 100, p: Principal = Depends(principal)):
        manager.get(p, sandbox_id)
        return db.list_audit(sandbox_id, limit)

    # --- dashboard ----------------------------------------------------------
    @app.get("/api/v1/dashboard/summary")
    def dashboard_summary(p: Principal = Depends(principal)):
        return manager.summary(p)

    @app.get("/api/v1/audit")
    def audit(limit: int = 200, _: Principal = Depends(admin)):
        return db.list_audit(None, limit)

    # --- agent integration --------------------------------------------------
    @app.get("/api/v1/agent/tools")
    def agent_tool_schema():
        return agent_tools.TOOLS

    @app.post("/api/v1/agent/invoke")
    def agent_invoke(body: InvokeTool, p: Principal = Depends(principal)):
        owner = body.owner if p.role != "user" else p.name
        return agent_tools.invoke(manager, p, owner, body.tool, body.arguments)

    # --- tokens (admin) -----------------------------------------------------
    @app.post("/api/v1/tokens", status_code=201)
    def create_token(body: CreateToken, _: Principal = Depends(admin)):
        if body.role not in auth.ROLES:
            raise HTTPException(400, f"role must be one of {auth.ROLES}")
        token = auth.new_token()
        db.insert_token(token, body.principal, body.role, body.description)
        return {"token": token, "principal": body.principal, "role": body.role}

    @app.get("/api/v1/tokens")
    def list_tokens(_: Principal = Depends(admin)):
        return db.list_tokens()

    @app.delete("/api/v1/tokens/{token_id}")
    def revoke_token(token_id: str, _: Principal = Depends(admin)):
        if not db.revoke_token(token_id):
            raise HTTPException(404, "token not found")
        return {"revoked": token_id}

    # --- Prometheus ---------------------------------------------------------
    @app.get("/metrics", response_class=PlainTextResponse)
    def prometheus():
        lines = [
            "# HELP intrallm_sandbox_active Active sandboxes",
            "# TYPE intrallm_sandbox_active gauge",
            f"intrallm_sandbox_active {db.count_active()}",
            "# HELP intrallm_sandbox_cpu_percent Sandbox CPU usage (100 = one core)",
            "# TYPE intrallm_sandbox_cpu_percent gauge",
            "# HELP intrallm_sandbox_memory_bytes Sandbox memory usage",
            "# TYPE intrallm_sandbox_memory_bytes gauge",
        ]
        for sb in db.list_sandboxes(status="running", limit=100_000):
            s = manager.metrics.get_latest(sb["id"])
            if not s:
                continue
            lbl = f'sandbox="{sb["id"]}",owner="{_esc(sb["owner"])}"'
            lines.append(f"intrallm_sandbox_cpu_percent{{{lbl}}} {s['cpu_percent']}")
            lines.append(f"intrallm_sandbox_memory_bytes{{{lbl}}} {s['memory_bytes']}")
        return "\n".join(lines) + "\n"

    # --- UI -----------------------------------------------------------------
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html")

    logging.basicConfig(level=logging.INFO)
    return app


def _esc(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
