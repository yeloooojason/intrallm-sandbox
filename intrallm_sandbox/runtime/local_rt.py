"""Local subprocess runtime for development and tests.

WARNING: this provides NO isolation. Commands run as the server's user on the
host. Use it only on a developer machine or in CI; production must use Docker.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import threading
import time
from pathlib import Path

import psutil

from .base import (
    ExecResult,
    FileEntry,
    ResourceStats,
    Runtime,
    RuntimeError_,
    SandboxSpec,
    truncate,
)


class LocalRuntime(Runtime):
    name = "local"

    def __init__(self, root: str) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._procs: dict[str, dict[int, psutil.Process]] = {}
        self._limits: dict[str, int] = {}

    def _dir(self, handle: str) -> Path:
        d = self.root / handle
        if not d.is_dir():
            raise RuntimeError_(f"sandbox dir {handle} not found")
        return d

    def _resolve(self, handle: str, path: str) -> Path:
        base = self._dir(handle)
        # Mirror the container layout: "/workspace/x", "x" and "/x" all map into the sandbox dir.
        rel = path.lstrip("/")
        rel = "" if rel == "workspace" else rel.removeprefix("workspace/")
        p = (base / rel).resolve()
        if p != base and base not in p.parents:
            raise RuntimeError_("path escapes sandbox workspace")
        return p

    def create(self, spec: SandboxSpec) -> str:
        handle = spec.sandbox_id
        (self.root / handle).mkdir(parents=True, exist_ok=True)
        with self._lock:
            self._procs[handle] = {}
            self._limits[handle] = spec.memory_mb * 1024 * 1024
        return handle

    def exec(self, handle, command, timeout, workdir=None, env=None, max_output=1_000_000):
        cwd = self._resolve(handle, workdir or "")
        full_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(self._dir(handle))}
        full_env.update(env or {})
        start = time.monotonic()
        proc = subprocess.Popen(
            ["sh", "-c", command],
            cwd=cwd,
            env=full_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            ps = psutil.Process(proc.pid)
            ps.cpu_percent(None)
            with self._lock:
                self._procs.setdefault(handle, {})[proc.pid] = ps
        except psutil.Error:
            pass
        timed_out = False
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            out, err = proc.communicate()
        finally:
            with self._lock:
                self._procs.get(handle, {}).pop(proc.pid, None)
        stdout, t1 = truncate(out, max_output)
        stderr, t2 = truncate(err, max_output)
        return ExecResult(
            exit_code=-9 if timed_out else proc.returncode,
            stdout=stdout,
            stderr=stderr,
            duration_ms=int((time.monotonic() - start) * 1000),
            timed_out=timed_out,
            truncated=t1 or t2,
        )

    def write_file(self, handle, path, data):
        p = self._resolve(handle, path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)

    def read_file(self, handle, path, max_bytes):
        p = self._resolve(handle, path)
        if not p.is_file():
            raise RuntimeError_(f"no such file: {path}")
        if p.stat().st_size > max_bytes:
            raise RuntimeError_(f"file too large ({p.stat().st_size} bytes > {max_bytes})")
        return p.read_bytes()

    def list_dir(self, handle, path):
        p = self._resolve(handle, path)
        if not p.is_dir():
            raise RuntimeError_(f"not a directory: {path}")
        entries = []
        for child in p.iterdir():
            st = child.lstat()
            kind = "link" if child.is_symlink() else "dir" if child.is_dir() else "file" if child.is_file() else "other"
            entries.append(FileEntry(child.name, kind, st.st_size, st.st_mtime))
        return sorted(entries, key=lambda e: (e.type != "dir", e.name))

    def stats(self, handle):
        with self._lock:
            procs = list(self._procs.get(handle, {}).values())
            limit = self._limits.get(handle, 0)
        cpu = mem = pids = 0
        for ps in procs:
            try:
                family = [ps, *ps.children(recursive=True)]
                for p in family:
                    cpu += p.cpu_percent(None) if p is ps else 0.0
                    mem += p.memory_info().rss
                pids += len(family)
            except psutil.Error:
                continue
        return ResourceStats(cpu_percent=round(cpu, 2), memory_bytes=mem, memory_limit_bytes=limit, pids=pids)

    def is_alive(self, handle):
        return (self.root / handle).is_dir()

    def destroy(self, handle):
        with self._lock:
            procs = self._procs.pop(handle, {})
            self._limits.pop(handle, None)
        for pid in procs:
            try:
                os.killpg(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        shutil.rmtree(self.root / handle, ignore_errors=True)

    def list_handles(self):
        return [p.name for p in self.root.iterdir() if p.is_dir()]
