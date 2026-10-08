"""Runtime abstraction: the thing that actually runs sandbox workloads."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


class RuntimeError_(Exception):
    """Raised by runtimes for failures inside a sandbox (bad path, dead container...)."""


@dataclass
class SandboxSpec:
    sandbox_id: str
    image: str
    cpu: float
    memory_mb: int
    pids_limit: int
    network: str
    env: dict[str, str] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)
    user: str = ""
    # None = keep the image's own CMD (desktop images start their X server there).
    command: list[str] | None = field(default_factory=lambda: ["sleep", "infinity"])
    shm_size_mb: int | None = None
    # With network == "isolated": containers (e.g. the egress proxy) to attach to the
    # sandbox's own private network. They are the only peers the sandbox can reach.
    network_peers: list[str] = field(default_factory=list)


@dataclass
class ExecResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool = False
    truncated: bool = False


@dataclass
class FileEntry:
    name: str
    type: str  # "file" | "dir" | "link" | "other"
    size: int
    mtime: float


@dataclass
class ResourceStats:
    cpu_percent: float  # 100 == one full core
    memory_bytes: int
    memory_limit_bytes: int
    pids: int = 0
    net_rx_bytes: int = 0
    net_tx_bytes: int = 0


def truncate(data: bytes, limit: int) -> tuple[str, bool]:
    if len(data) > limit:
        return data[:limit].decode("utf-8", "replace"), True
    return data.decode("utf-8", "replace"), False


class Runtime(ABC):
    name: str = "base"

    @abstractmethod
    def create(self, spec: SandboxSpec) -> str:
        """Start a sandbox; returns an opaque runtime handle."""

    @abstractmethod
    def exec(
        self,
        handle: str,
        command: str,
        timeout: int,
        workdir: str | None = None,
        env: dict[str, str] | None = None,
        max_output: int = 1_000_000,
    ) -> ExecResult: ...

    @abstractmethod
    def write_file(self, handle: str, path: str, data: bytes) -> None: ...

    @abstractmethod
    def read_file(self, handle: str, path: str, max_bytes: int) -> bytes: ...

    @abstractmethod
    def list_dir(self, handle: str, path: str) -> list[FileEntry]: ...

    @abstractmethod
    def stats(self, handle: str) -> ResourceStats | None: ...

    @abstractmethod
    def is_alive(self, handle: str) -> bool: ...

    @abstractmethod
    def destroy(self, handle: str) -> None: ...

    def list_handles(self) -> list[str]:
        """Handles of all sandboxes this runtime knows about (for orphan cleanup)."""
        return []
