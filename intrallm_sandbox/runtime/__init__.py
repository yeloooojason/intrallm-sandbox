from .base import ExecResult, FileEntry, ResourceStats, Runtime, RuntimeError_, SandboxSpec


def build_runtime(settings) -> Runtime:
    if settings.runtime == "docker":
        from .docker_rt import DockerRuntime

        return DockerRuntime()
    if settings.runtime == "local":
        from .local_rt import LocalRuntime

        return LocalRuntime(settings.local_root)
    raise ValueError(f"unknown runtime {settings.runtime!r} (expected 'docker' or 'local')")


__all__ = [
    "ExecResult",
    "FileEntry",
    "ResourceStats",
    "Runtime",
    "RuntimeError_",
    "SandboxSpec",
    "build_runtime",
]
