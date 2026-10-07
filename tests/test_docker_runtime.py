"""Integration test against a real Docker daemon; skipped when none is available."""

import pytest

docker = pytest.importorskip("docker")

from intrallm_sandbox.runtime import SandboxSpec  # noqa: E402

IMAGE = "python:3.12-slim"


@pytest.fixture(scope="module")
def rt():
    try:
        from intrallm_sandbox.runtime.docker_rt import DockerRuntime

        runtime = DockerRuntime()
        runtime.client.images.get(IMAGE)
    except Exception as e:
        pytest.skip(f"docker unavailable: {e}")
    return runtime


def test_docker_sandbox(rt):
    spec = SandboxSpec("sbx-pytest", IMAGE, cpu=0.5, memory_mb=256, pids_limit=64, network="none")
    handle = rt.create(spec)
    try:
        assert rt.is_alive(handle)
        rt.write_file(handle, "dir/x.py", b"print('hi')")
        assert rt.read_file(handle, "/workspace/dir/x.py", 1000) == b"print('hi')"
        res = rt.exec(handle, "python3 dir/x.py && echo err >&2", timeout=10)
        assert (res.exit_code, res.stdout, res.stderr) == (0, "hi\n", "err\n")
        assert [e.name for e in rt.list_dir(handle, "/workspace")] == ["dir"]
        assert rt.exec(handle, "sleep 10", timeout=1).timed_out
        # no network
        assert rt.exec(handle, "python3 -c \"import socket; socket.create_connection(('1.1.1.1', 53), 2)\"", 10).exit_code != 0
        stats = rt.stats(handle)
        assert stats is not None and stats.memory_limit_bytes == 256 * 1024 * 1024
        assert handle in rt.list_handles()
    finally:
        rt.destroy(handle)
    assert not rt.is_alive(handle)
