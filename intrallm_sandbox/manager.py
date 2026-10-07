"""Sandbox lifecycle, quotas, reaping and metrics sampling."""

from __future__ import annotations

import logging
import secrets
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from typing import Any

import psutil

from .auth import Principal
from .config import Settings
from .db import Database
from .runtime import ExecResult, ResourceStats, Runtime, RuntimeError_, SandboxSpec

log = logging.getLogger("intrallm_sandbox")


class SandboxError(Exception):
    status_code = 400

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class NotFound(SandboxError):
    status_code = 404


class Forbidden(SandboxError):
    status_code = 403


class QuotaExceeded(SandboxError):
    status_code = 429


class Conflict(SandboxError):
    status_code = 409


class MetricsStore:
    """In-memory ring buffers of host and per-sandbox resource samples."""

    def __init__(self, history: int) -> None:
        self.history = history
        self._lock = threading.Lock()
        self.latest: dict[str, dict[str, Any]] = {}
        self.per_sandbox: dict[str, deque] = {}
        self.host: deque = deque(maxlen=history)

    def record_sandbox(self, sandbox_id: str, ts: float, stats: ResourceStats) -> None:
        sample = {"ts": ts, **asdict(stats)}
        with self._lock:
            self.latest[sandbox_id] = sample
            self.per_sandbox.setdefault(sandbox_id, deque(maxlen=self.history)).append(sample)

    def record_host(self, sample: dict[str, Any]) -> None:
        with self._lock:
            self.host.append(sample)

    def forget(self, sandbox_id: str) -> None:
        with self._lock:
            self.latest.pop(sandbox_id, None)
            self.per_sandbox.pop(sandbox_id, None)

    def sandbox_history(self, sandbox_id: str) -> list[dict[str, Any]]:
        with self._lock:
            return list(self.per_sandbox.get(sandbox_id, ()))

    def host_history(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(self.host)

    def get_latest(self, sandbox_id: str) -> dict[str, Any] | None:
        with self._lock:
            return self.latest.get(sandbox_id)


def host_snapshot() -> dict[str, Any]:
    vm = psutil.virtual_memory()
    du = psutil.disk_usage("/")
    try:
        load = psutil.getloadavg()
    except (AttributeError, OSError):
        load = (0.0, 0.0, 0.0)
    return {
        "ts": time.time(),
        "cpu_percent": psutil.cpu_percent(None),
        "cpu_count": psutil.cpu_count() or 1,
        "memory_used": vm.total - vm.available,
        "memory_total": vm.total,
        "memory_percent": vm.percent,
        "disk_used": du.used,
        "disk_total": du.total,
        "load1": load[0],
    }


class SandboxManager:
    def __init__(self, settings: Settings, db: Database, runtime: Runtime) -> None:
        self.settings = settings
        self.db = db
        self.runtime = runtime
        self.metrics = MetricsStore(settings.metrics_history)
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._create_lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=16, thread_name_prefix="sbx-stats")
        psutil.cpu_percent(None)  # prime the host CPU counter

    # --- background loops ------------------------------------------------
    def start(self) -> None:
        self.reconcile()
        for target, interval in (
            (self.sample_metrics, self.settings.metrics_interval),
            (self.reap, self.settings.reaper_interval),
        ):
            t = threading.Thread(target=self._loop, args=(target, interval), daemon=True, name=target.__name__)
            t.start()
            self._threads.append(t)

    def shutdown(self) -> None:
        self._stop.set()
        for t in self._threads:
            t.join(timeout=5)
        self._pool.shutdown(wait=False, cancel_futures=True)

    def _loop(self, fn, interval: float) -> None:
        while not self._stop.is_set():
            try:
                fn()
            except Exception:
                log.exception("background task %s failed", fn.__name__)
            self._stop.wait(interval)

    def reconcile(self) -> None:
        """Align DB state with what the runtime really has (after restarts/crashes)."""
        known = set()
        for sb in self.db.list_sandboxes(active_only=True, limit=100_000):
            known.add(sb["handle"])
            if not sb["handle"] or not self.runtime.is_alive(sb["handle"]):
                self.db.update_sandbox(
                    sb["id"], status="error", terminated_at=time.time(), terminate_reason="lost after restart"
                )
        for handle in self.runtime.list_handles():
            if handle not in known:
                log.warning("removing orphan sandbox %s", handle)
                self.runtime.destroy(handle)

    def sample_metrics(self) -> None:
        self.metrics.record_host(host_snapshot())
        running = self.db.list_sandboxes(status="running", limit=100_000)

        def one(sb):
            return sb, self.runtime.stats(sb["handle"])

        now = time.time()
        for sb, stats in self._pool.map(one, running):
            if stats is not None:
                self.metrics.record_sandbox(sb["id"], now, stats)

    def reap(self) -> None:
        now = time.time()
        for sb in self.db.list_sandboxes(status="running", limit=100_000):
            reason = None
            if sb["expires_at"] <= now:
                reason = "ttl expired"
            elif self.settings.idle_timeout and now - sb["last_active_at"] > self.settings.idle_timeout:
                reason = "idle timeout"
            elif not self.runtime.is_alive(sb["handle"]):
                reason = "runtime exited"
            if reason:
                log.info("reaping sandbox %s: %s", sb["id"], reason)
                self._terminate(sb, actor="system", reason=reason)

    # --- lifecycle -------------------------------------------------------
    def create(
        self,
        principal: Principal,
        owner: str | None = None,
        name: str | None = None,
        image: str | None = None,
        cpu: float | None = None,
        memory_mb: int | None = None,
        ttl_seconds: int | None = None,
        env: dict[str, str] | None = None,
        labels: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        s = self.settings
        if principal.role == "user":
            if owner and owner != principal.name:
                raise Forbidden("users can only allocate sandboxes to themselves")
            owner = principal.name
        owner = owner or principal.name
        image = image or s.default_image
        if s.allowed_images and image not in s.allowed_images:
            raise SandboxError(f"image {image!r} is not in the allowed list")
        cpu = cpu if cpu is not None else s.default_cpu
        memory_mb = memory_mb if memory_mb is not None else s.default_memory_mb
        if not (0 < cpu <= s.max_cpu):
            raise SandboxError(f"cpu must be in (0, {s.max_cpu}]")
        if not (64 <= memory_mb <= s.max_memory_mb):
            raise SandboxError(f"memory_mb must be in [64, {s.max_memory_mb}]")
        ttl = min(ttl_seconds or s.default_ttl, s.max_ttl)

        sandbox_id = "sbx-" + secrets.token_hex(6)
        now = time.time()
        with self._create_lock:
            if self.db.count_active() >= s.max_total:
                raise QuotaExceeded(f"cluster limit reached ({s.max_total} active sandboxes)")
            if self.db.count_active(owner) >= s.max_per_owner:
                raise QuotaExceeded(f"{owner} already has {s.max_per_owner} active sandboxes")
            record = {
                "id": sandbox_id,
                "name": name or sandbox_id,
                "owner": owner,
                "created_by": principal.name,
                "status": "creating",
                "image": image,
                "cpu": cpu,
                "memory_mb": memory_mb,
                "runtime": self.runtime.name,
                "handle": None,
                "labels": labels or {},
                "created_at": now,
                "expires_at": now + ttl,
                "last_active_at": now,
            }
            self.db.insert_sandbox(record)

        spec = SandboxSpec(
            sandbox_id=sandbox_id,
            image=image,
            cpu=cpu,
            memory_mb=memory_mb,
            pids_limit=s.pids_limit,
            network=s.network,
            env=env or {},
            labels={"intrallm.sandbox.owner": owner, "intrallm.sandbox.created_by": principal.name},
            user=s.container_user,
        )
        try:
            handle = self.runtime.create(spec)
        except Exception as e:
            self.db.update_sandbox(sandbox_id, status="error", terminated_at=time.time(), terminate_reason=str(e)[:500])
            self.db.audit(principal.name, "create_failed", sandbox_id, error=str(e)[:500])
            raise SandboxError(f"failed to start sandbox: {e}") from e
        self.db.update_sandbox(sandbox_id, status="running", handle=handle)
        self.db.audit(principal.name, "create", sandbox_id, owner=owner, image=image, cpu=cpu, memory_mb=memory_mb)
        return self.db.get_sandbox(sandbox_id)

    def get(self, principal: Principal, sandbox_id: str) -> dict[str, Any]:
        sb = self.db.get_sandbox(sandbox_id)
        if sb is None or not principal.can_access(sb):
            raise NotFound(f"sandbox {sandbox_id} not found")
        return sb

    def _running(self, principal: Principal, sandbox_id: str) -> dict[str, Any]:
        sb = self.get(principal, sandbox_id)
        if sb["status"] != "running":
            raise Conflict(f"sandbox {sandbox_id} is {sb['status']}")
        return sb

    def list(self, principal: Principal, active_only: bool = False, owner: str | None = None) -> list[dict]:
        if principal.role == "user":
            return self.db.list_sandboxes(owner=principal.name, active_only=active_only)
        if principal.role == "agent":
            return self.db.list_sandboxes(created_by=principal.name, owner=owner, active_only=active_only)
        return self.db.list_sandboxes(owner=owner, active_only=active_only)

    def exec(
        self,
        principal: Principal,
        sandbox_id: str,
        command: str,
        timeout: int | None = None,
        workdir: str | None = None,
        env: dict[str, str] | None = None,
    ) -> ExecResult:
        sb = self._running(principal, sandbox_id)
        timeout = min(timeout or self.settings.exec_timeout, self.settings.max_exec_timeout)
        self.db.touch_sandbox(sandbox_id, exec_inc=1)
        try:
            res = self.runtime.exec(sb["handle"], command, timeout, workdir, env, self.settings.max_output_bytes)
        except RuntimeError_ as e:
            raise SandboxError(str(e)) from e
        self.db.touch_sandbox(sandbox_id)
        self.db.audit(
            principal.name,
            "exec",
            sandbox_id,
            command=command[:2000],
            exit_code=res.exit_code,
            duration_ms=res.duration_ms,
            timed_out=res.timed_out,
        )
        return res

    def write_file(self, principal: Principal, sandbox_id: str, path: str, data: bytes) -> None:
        sb = self._running(principal, sandbox_id)
        if len(data) > self.settings.max_file_bytes:
            raise SandboxError(f"file exceeds {self.settings.max_file_bytes} bytes")
        try:
            self.runtime.write_file(sb["handle"], path, data)
        except RuntimeError_ as e:
            raise SandboxError(str(e)) from e
        self.db.touch_sandbox(sandbox_id)
        self.db.audit(principal.name, "write_file", sandbox_id, path=path, size=len(data))

    def read_file(self, principal: Principal, sandbox_id: str, path: str) -> bytes:
        sb = self._running(principal, sandbox_id)
        try:
            data = self.runtime.read_file(sb["handle"], path, self.settings.max_file_bytes)
        except RuntimeError_ as e:
            raise NotFound(str(e)) from e
        self.db.touch_sandbox(sandbox_id)
        return data

    def list_files(self, principal: Principal, sandbox_id: str, path: str):
        sb = self._running(principal, sandbox_id)
        try:
            entries = self.runtime.list_dir(sb["handle"], path)
        except RuntimeError_ as e:
            raise NotFound(str(e)) from e
        self.db.touch_sandbox(sandbox_id)
        return entries

    def extend(self, principal: Principal, sandbox_id: str, seconds: int) -> dict[str, Any]:
        sb = self._running(principal, sandbox_id)
        max_expiry = sb["created_at"] + self.settings.max_ttl
        new_expiry = min(max(sb["expires_at"], time.time()) + seconds, max_expiry)
        self.db.update_sandbox(sandbox_id, expires_at=new_expiry)
        self.db.touch_sandbox(sandbox_id)
        self.db.audit(principal.name, "extend", sandbox_id, seconds=seconds)
        return self.db.get_sandbox(sandbox_id)

    def reassign(self, principal: Principal, sandbox_id: str, owner: str) -> dict[str, Any]:
        if principal.role == "user":
            raise Forbidden("only admins and agents can reassign sandboxes")
        sb = self._running(principal, sandbox_id)
        self.db.update_sandbox(sandbox_id, owner=owner)
        self.db.audit(principal.name, "reassign", sandbox_id, old_owner=sb["owner"], owner=owner)
        return self.db.get_sandbox(sandbox_id)

    def destroy(self, principal: Principal, sandbox_id: str, reason: str = "stopped by user") -> dict[str, Any]:
        sb = self.get(principal, sandbox_id)
        if sb["status"] not in ("creating", "running"):
            return sb
        self._terminate(sb, actor=principal.name, reason=reason)
        return self.db.get_sandbox(sandbox_id)

    def _terminate(self, sb: dict[str, Any], actor: str, reason: str) -> None:
        if sb["handle"]:
            try:
                self.runtime.destroy(sb["handle"])
            except Exception:
                log.exception("failed to destroy %s", sb["id"])
        self.db.update_sandbox(sb["id"], status="terminated", terminated_at=time.time(), terminate_reason=reason)
        self.db.audit(actor, "destroy", sb["id"], reason=reason)
        self.metrics.forget(sb["id"])

    # --- dashboard -------------------------------------------------------
    def summary(self, principal: Principal) -> dict[str, Any]:
        active = self.list(principal, active_only=True)
        owners: dict[str, dict[str, Any]] = {}
        total_cpu = total_mem = 0.0
        alloc_cpu = alloc_mem = 0.0
        for sb in active:
            latest = self.metrics.get_latest(sb["id"]) or {}
            o = owners.setdefault(sb["owner"], {"owner": sb["owner"], "sandboxes": 0, "cpu_percent": 0.0, "memory_bytes": 0})
            o["sandboxes"] += 1
            o["cpu_percent"] += latest.get("cpu_percent", 0.0)
            o["memory_bytes"] += latest.get("memory_bytes", 0)
            total_cpu += latest.get("cpu_percent", 0.0)
            total_mem += latest.get("memory_bytes", 0)
            alloc_cpu += sb["cpu"]
            alloc_mem += sb["memory_mb"] * 1024 * 1024
        host_hist = self.metrics.host_history()
        return {
            "runtime": self.runtime.name,
            "running": sum(1 for sb in active if sb["status"] == "running"),
            "active": len(active),
            "owners": sorted(owners.values(), key=lambda o: -o["sandboxes"]),
            "status_counts": self.db.status_counts() if principal.is_admin else None,
            "usage": {
                "cpu_percent": round(total_cpu, 2),
                "memory_bytes": int(total_mem),
                "allocated_cpu": alloc_cpu,
                "allocated_memory_bytes": int(alloc_mem),
            },
            "quota": {"max_total": self.settings.max_total, "max_per_owner": self.settings.max_per_owner},
            "host": (host_hist[-1] if host_hist else host_snapshot()) if principal.is_admin else None,
            "host_history": host_hist if principal.is_admin else [],
        }

    def view(self, sb: dict[str, Any]) -> dict[str, Any]:
        """Public representation of a sandbox record with its latest stats."""
        out = {k: v for k, v in sb.items() if k != "handle"}
        out["stats"] = self.metrics.get_latest(sb["id"]) if sb["status"] == "running" else None
        return out
