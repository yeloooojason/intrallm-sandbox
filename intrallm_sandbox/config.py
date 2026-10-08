"""Settings loaded from environment variables (prefix ``SANDBOX_``)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str) -> str:
    return os.environ.get(f"SANDBOX_{name}", default)


def _env_int(name: str, default: int) -> int:
    return int(_env(name, str(default)))


def _env_float(name: str, default: float) -> float:
    return float(_env(name, str(default)))


def _env_bool(name: str, default: bool) -> bool:
    return _env(name, "1" if default else "0").lower() in ("1", "true", "yes", "on")


@dataclass
class Settings:
    # "docker" for production, "local" for development without Docker (NOT isolated).
    runtime: str = field(default_factory=lambda: _env("RUNTIME", "docker"))
    db_path: str = field(default_factory=lambda: _env("DB_PATH", "./data/sandbox.db"))
    local_root: str = field(default_factory=lambda: _env("LOCAL_ROOT", "./data/local-sandboxes"))

    # Bootstrap admin token; required to manage other tokens.
    admin_token: str = field(default_factory=lambda: _env("ADMIN_TOKEN", ""))

    # Sandbox defaults
    default_image: str = field(default_factory=lambda: _env("DEFAULT_IMAGE", "intrallm/sandbox:latest"))
    allowed_images: list[str] = field(
        default_factory=lambda: [i for i in _env("ALLOWED_IMAGES", "").split(",") if i]
    )
    default_cpu: float = field(default_factory=lambda: _env_float("DEFAULT_CPU", 1.0))
    default_memory_mb: int = field(default_factory=lambda: _env_int("DEFAULT_MEMORY_MB", 1024))
    max_cpu: float = field(default_factory=lambda: _env_float("MAX_CPU", 4.0))
    max_memory_mb: int = field(default_factory=lambda: _env_int("MAX_MEMORY_MB", 8192))
    pids_limit: int = field(default_factory=lambda: _env_int("PIDS_LIMIT", 256))
    # Docker network mode for sandboxes. "none" = no network access (safest).
    network: str = field(default_factory=lambda: _env("NETWORK", "none"))
    container_user: str = field(default_factory=lambda: _env("CONTAINER_USER", ""))

    # Desktop template (virtual screen + Chromium, driven by computer-use / Playwright tools)
    desktop_image: str = field(default_factory=lambda: _env("DESKTOP_IMAGE", "intrallm/sandbox-desktop:latest"))
    desktop_cpu: float = field(default_factory=lambda: _env_float("DESKTOP_CPU", 2.0))
    desktop_memory_mb: int = field(default_factory=lambda: _env_int("DESKTOP_MEMORY_MB", 2048))
    desktop_shm_mb: int = field(default_factory=lambda: _env_int("DESKTOP_SHM_MB", 1024))
    desktop_pids_limit: int = field(default_factory=lambda: _env_int("DESKTOP_PIDS_LIMIT", 1024))
    # The browser needs network. Point this at an internal Docker network whose only
    # way out is the egress proxy (see docker-compose.yml), never plain "bridge" in prod.
    # "isolated" = a private internal network per sandbox with only DESKTOP_NETWORK_PEERS
    # attached (recommended, see docker-compose.yml); or any Docker network name.
    desktop_network: str = field(default_factory=lambda: _env("DESKTOP_NETWORK", "bridge"))
    desktop_network_peers: list[str] = field(
        default_factory=lambda: [p for p in _env("DESKTOP_NETWORK_PEERS", "").split(",") if p]
    )
    desktop_proxy: str = field(default_factory=lambda: _env("DESKTOP_PROXY", ""))
    desktop_home: str = field(default_factory=lambda: _env("DESKTOP_HOME", "about:blank"))
    max_desktops_per_owner: int = field(default_factory=lambda: _env_int("MAX_DESKTOPS_PER_OWNER", 2))
    desktop_action_timeout: int = field(default_factory=lambda: _env_int("DESKTOP_ACTION_TIMEOUT", 90))

    # Lifecycle (seconds)
    default_ttl: int = field(default_factory=lambda: _env_int("DEFAULT_TTL", 3600))
    max_ttl: int = field(default_factory=lambda: _env_int("MAX_TTL", 24 * 3600))
    idle_timeout: int = field(default_factory=lambda: _env_int("IDLE_TIMEOUT", 1800))
    exec_timeout: int = field(default_factory=lambda: _env_int("EXEC_TIMEOUT", 60))
    max_exec_timeout: int = field(default_factory=lambda: _env_int("MAX_EXEC_TIMEOUT", 600))
    max_output_bytes: int = field(default_factory=lambda: _env_int("MAX_OUTPUT_BYTES", 1_000_000))
    max_file_bytes: int = field(default_factory=lambda: _env_int("MAX_FILE_BYTES", 10_000_000))

    # Quotas
    max_per_owner: int = field(default_factory=lambda: _env_int("MAX_PER_OWNER", 5))
    max_total: int = field(default_factory=lambda: _env_int("MAX_TOTAL", 100))

    # Background loops (seconds)
    metrics_interval: float = field(default_factory=lambda: _env_float("METRICS_INTERVAL", 5))
    metrics_history: int = field(default_factory=lambda: _env_int("METRICS_HISTORY", 360))
    reaper_interval: float = field(default_factory=lambda: _env_float("REAPER_INTERVAL", 30))
    background: bool = field(default_factory=lambda: _env_bool("BACKGROUND", True))


def get_settings() -> Settings:
    return Settings()
