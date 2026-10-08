"""Docker-backed runtime: every sandbox is a hardened, resource-limited container."""

from __future__ import annotations

import io
import posixpath
import shlex
import tarfile
import time

from .base import (
    ExecResult,
    FileEntry,
    ResourceStats,
    Runtime,
    RuntimeError_,
    SandboxSpec,
    truncate,
)

LABEL = "intrallm.sandbox"
NET_LABEL = "intrallm.sandbox.network"
WORKDIR = "/workspace"
ISOLATED = "isolated"


def _abs(path: str) -> str:
    path = path or "."
    if not path.startswith("/"):
        path = posixpath.join(WORKDIR, path)
    return posixpath.normpath(path)


class DockerRuntime(Runtime):
    name = "docker"

    def __init__(self) -> None:
        import docker  # imported lazily so the local runtime works without the SDK

        self._docker = docker
        self.client = docker.from_env()
        self.client.ping()

    def _container(self, handle: str):
        try:
            return self.client.containers.get(handle)
        except self._docker.errors.NotFound as e:
            raise RuntimeError_(f"container {handle[:12]} not found") from e

    def _isolated_network(self, spec: SandboxSpec) -> str:
        """A private internal network per sandbox: no route out, no other sandboxes,
        only the configured peers (the egress proxy)."""
        name = f"intrallm-net-{spec.sandbox_id}"
        net = self.client.networks.create(
            name, driver="bridge", internal=True, labels={LABEL: "1", NET_LABEL: name}
        )
        try:
            for peer in spec.network_peers:
                net.connect(peer)
        except Exception:
            self._remove_network(name)
            raise
        return name

    def _remove_network(self, name: str) -> None:
        try:
            net = self.client.networks.get(name)
        except self._docker.errors.NotFound:
            return
        net.reload()
        for c in net.containers:
            try:
                net.disconnect(c, force=True)
            except self._docker.errors.APIError:
                pass
        try:
            net.remove()
        except self._docker.errors.APIError:
            pass

    def create(self, spec: SandboxSpec) -> str:
        network = spec.network
        labels = {LABEL: "1", "intrallm.sandbox.id": spec.sandbox_id, **spec.labels}
        if network == ISOLATED:
            network = self._isolated_network(spec)
            labels[NET_LABEL] = network
        kwargs = dict(
            image=spec.image,
            name=f"intrallm-sbx-{spec.sandbox_id}",
            detach=True,
            labels=labels,
            environment=spec.env,
            working_dir=WORKDIR,
            nano_cpus=int(spec.cpu * 1e9),
            mem_limit=f"{spec.memory_mb}m",
            memswap_limit=f"{spec.memory_mb}m",
            pids_limit=spec.pids_limit,
            network_mode=network,
            cap_drop=["ALL"],
            security_opt=["no-new-privileges:true"],
            init=True,
            auto_remove=False,
        )
        if spec.command is not None:
            kwargs["command"] = spec.command
        if spec.shm_size_mb:
            kwargs["shm_size"] = f"{spec.shm_size_mb}m"
        if spec.user:
            kwargs["user"] = spec.user
        try:
            try:
                container = self.client.containers.run(**kwargs)
            except self._docker.errors.ImageNotFound:
                self.client.images.pull(spec.image)
                container = self.client.containers.run(**kwargs)
        except Exception:
            if NET_LABEL in labels:
                self._remove_network(labels[NET_LABEL])
            raise
        # Ensure the workspace exists even on images that don't ship it.
        container.exec_run(["mkdir", "-p", WORKDIR])
        return container.id

    def exec(self, handle, command, timeout, workdir=None, env=None, max_output=1_000_000):
        c = self._container(handle)
        # Docker exec has no native timeout; coreutils `timeout` enforces it in-container.
        argv = ["timeout", "-s", "KILL", str(int(timeout)), "sh", "-c", command]
        start = time.monotonic()
        res = c.exec_run(argv, workdir=_abs(workdir or WORKDIR), environment=env or {}, demux=True)
        duration = int((time.monotonic() - start) * 1000)
        out, err = res.output if res.output else (b"", b"")
        stdout, t1 = truncate(out or b"", max_output)
        stderr, t2 = truncate(err or b"", max_output)
        code = res.exit_code if res.exit_code is not None else -1
        return ExecResult(
            exit_code=code,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration,
            timed_out=code == 137 and duration >= timeout * 1000 - 50,
            truncated=t1 or t2,
        )

    def write_file(self, handle, path, data):
        c = self._container(handle)
        path = _abs(path)
        directory, name = posixpath.split(path)
        c.exec_run(["mkdir", "-p", directory])
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w") as tar:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = int(time.time())
            info.mode = 0o644
            tar.addfile(info, io.BytesIO(data))
        if not c.put_archive(directory, buf.getvalue()):
            raise RuntimeError_(f"failed to write {path}")

    def read_file(self, handle, path, max_bytes):
        c = self._container(handle)
        try:
            stream, stat = c.get_archive(_abs(path))
        except self._docker.errors.NotFound as e:
            raise RuntimeError_(f"no such file: {path}") from e
        if stat.get("size", 0) > max_bytes:
            raise RuntimeError_(f"file too large ({stat['size']} bytes > {max_bytes})")
        buf = io.BytesIO(b"".join(stream))
        with tarfile.open(fileobj=buf) as tar:
            member = tar.next()
            if member is None or not member.isfile():
                raise RuntimeError_(f"not a regular file: {path}")
            return tar.extractfile(member).read()

    def list_dir(self, handle, path):
        c = self._container(handle)
        cmd = f"find {shlex.quote(_abs(path))} -mindepth 1 -maxdepth 1 -printf '%y\\t%s\\t%T@\\t%f\\n'"
        res = c.exec_run(["sh", "-c", cmd], demux=True)
        out, err = res.output
        if res.exit_code != 0:
            raise RuntimeError_((err or b"").decode(errors="replace").strip() or "list failed")
        kinds = {"f": "file", "d": "dir", "l": "link"}
        entries = []
        for line in (out or b"").decode(errors="replace").splitlines():
            parts = line.split("\t", 3)
            if len(parts) == 4:
                entries.append(FileEntry(parts[3], kinds.get(parts[0], "other"), int(parts[1]), float(parts[2])))
        return sorted(entries, key=lambda e: (e.type != "dir", e.name))

    def stats(self, handle):
        try:
            s = self._container(handle).stats(stream=False)
        except Exception:
            return None
        cpu = s.get("cpu_stats", {})
        pre = s.get("precpu_stats", {})
        cpu_delta = cpu.get("cpu_usage", {}).get("total_usage", 0) - pre.get("cpu_usage", {}).get("total_usage", 0)
        sys_delta = cpu.get("system_cpu_usage", 0) - pre.get("system_cpu_usage", 0)
        ncpu = cpu.get("online_cpus") or len(cpu.get("cpu_usage", {}).get("percpu_usage") or [1])
        cpu_pct = (cpu_delta / sys_delta) * ncpu * 100.0 if sys_delta > 0 and cpu_delta > 0 else 0.0
        mem = s.get("memory_stats", {})
        usage = mem.get("usage", 0) - mem.get("stats", {}).get("inactive_file", 0)
        rx = tx = 0
        for n in (s.get("networks") or {}).values():
            rx += n.get("rx_bytes", 0)
            tx += n.get("tx_bytes", 0)
        return ResourceStats(
            cpu_percent=round(cpu_pct, 2),
            memory_bytes=max(usage, 0),
            memory_limit_bytes=mem.get("limit", 0),
            pids=s.get("pids_stats", {}).get("current", 0),
            net_rx_bytes=rx,
            net_tx_bytes=tx,
        )

    def is_alive(self, handle):
        try:
            return self._container(handle).status == "running"
        except RuntimeError_:
            return False

    def destroy(self, handle):
        try:
            c = self._container(handle)
        except RuntimeError_:
            return
        network = c.labels.get(NET_LABEL)
        c.remove(force=True)
        if network:
            self._remove_network(network)

    def list_handles(self):
        return [c.id for c in self.client.containers.list(all=True, filters={"label": LABEL})]
