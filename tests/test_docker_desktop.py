"""Integration test for the desktop image (computer use + Playwright on one Chromium).

Skipped unless Docker is running and ``intrallm/sandbox-desktop:latest`` is built.
"""

import base64
import json
import re
import shlex

import pytest

pytest.importorskip("docker")

from intrallm_sandbox.runtime import SandboxSpec  # noqa: E402

IMAGE = "intrallm/sandbox-desktop:latest"
PAGE = """<html><body style="margin:40px">
<input id=q aria-label="query"><button id=b>Go</button><p id=out>none</p>
<script>let n=0;b.onclick=()=>{out.innerText='clicked '+(++n)+' '+q.value}</script></body></html>"""


@pytest.fixture(scope="module")
def desk():
    try:
        from intrallm_sandbox.runtime.docker_rt import DockerRuntime

        rt = DockerRuntime()
        rt.client.images.get(IMAGE)
    except Exception as e:
        pytest.skip(f"desktop image unavailable: {e}")
    spec = SandboxSpec(
        "sbx-pytest-desktop", IMAGE, cpu=2, memory_mb=2048, pids_limit=1024, network="none",
        command=None, shm_size_mb=1024,
    )
    handle = rt.create(spec)

    def ctl(**payload):
        res = rt.exec(handle, "desktopctl " + shlex.quote(json.dumps(payload)), timeout=90, max_output=32 << 20)
        return json.loads(res.stdout)

    try:
        assert ctl(action="wait_ready", timeout=45)["ok"]
        rt.write_file(handle, "page.html", PAGE.encode())
        yield ctl
    finally:
        rt.destroy(handle)


def test_screenshot(desk):
    out = desk(action="screenshot")
    assert out["ok"] and (out["width"], out["height"]) == (1280, 800)
    assert base64.b64decode(out["image"]["data"])[:4] == b"\x89PNG"


def test_browser_and_computer_share_one_browser(desk):
    snap = desk(tool="browser", action="navigate", url="file:///workspace/page.html")
    assert snap["ok"], snap
    m = re.search(r'\[2\] button "Go" @(\d+),(\d+)', snap["snapshot"])
    assert m, snap["snapshot"]

    # Playwright fills the input; computer use clicks the button at its screen position.
    assert desk(tool="browser", action="fill", ref=1, text="你好 abc")["ok"]
    assert desk(action="left_click", coordinate=[int(m[1]), int(m[2])], screenshot=False)["ok"]
    assert "clicked 1 你好 abc" in desk(tool="browser", action="snapshot")["snapshot"]

    # Computer-use typing (incl. CJK) lands in the focused field.
    q = re.search(r'\[1\] input\[text\] "query".* @(\d+),(\d+)', snap["snapshot"])
    desk(action="triple_click", coordinate=[int(q[1]), int(q[2])], screenshot=False)
    assert desk(action="type", text="张伟 42", screenshot=False)["ok"]
    assert "value='张伟 42'" in desk(tool="browser", action="snapshot")["snapshot"]


def test_errors(desk):
    assert "outside" in desk(action="left_click", coordinate=[5000, 1])["error"]
    assert "not found" in desk(tool="browser", action="click", ref=999)["error"]
    assert "unknown" in desk(action="fly")["error"]
    # network=none: the browser cannot reach anything
    assert not desk(tool="browser", action="navigate", url="https://example.com")["ok"]
