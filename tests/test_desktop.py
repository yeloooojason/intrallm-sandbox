"""Desktop template / computer-use / browser tools, with a fake in-sandbox `desktopctl`."""

import base64
import json
import os
import stat
import sys

import pytest
from fastapi.testclient import TestClient

from intrallm_sandbox.client import AgentToolkit, SandboxClient, prune_screenshots

PNG_1PX = base64.b64encode(
    bytes.fromhex(
        "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
        "1f15c4890000000d49444154789c6360000002000154a24f5d0000000049454e44ae426082"
    )
).decode()

FAKE = f"""#!{sys.executable}
import json, sys
a = json.loads(sys.argv[1])
open("desktopctl.log", "a").write(json.dumps(a) + "\\n")
act = a.get("action")
if act == "explode":
    print(json.dumps({{"ok": False, "error": "boom"}})); sys.exit(1)
if a.get("tool") == "browser":
    print(json.dumps({{"ok": True, "url": "https://oa.intra/", "title": "OA", "snapshot": "[1] button \\"提交\\""}}))
elif act in ("screenshot",) or (a.get("screenshot", True) and act not in ("wait_ready", "cursor_position")):
    print(json.dumps({{"ok": True, "image": {{"media_type": "image/png", "data": "{PNG_1PX}"}}, "width": 1, "height": 1}}))
else:
    print(json.dumps({{"ok": True, "ready": True}}))
"""


@pytest.fixture(autouse=True)
def fake_desktopctl(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    script = bindir / "desktopctl"
    script.write_text(FAKE)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")


def calls(settings, sid):
    log = os.path.join(settings.local_root, sid, "desktopctl.log")
    return [json.loads(line) for line in open(log)]


def test_create_desktop_defaults(client, settings):
    sb = client.post("/api/v1/sandboxes", json={"owner": "alice", "template": "desktop"}).json()
    assert sb["template"] == "desktop" and sb["status"] == "running"
    assert sb["cpu"] == settings.desktop_cpu and sb["memory_mb"] == settings.desktop_memory_mb
    assert sb["image"] == settings.desktop_image
    assert calls(settings, sb["id"])[0]["action"] == "wait_ready"
    assert client.post("/api/v1/sandboxes", json={"template": "gpu"}).status_code == 400


def test_desktop_quota(client, settings):
    settings.max_desktops_per_owner = 1
    assert client.post("/api/v1/sandboxes", json={"owner": "a", "template": "desktop"}).status_code == 201
    r = client.post("/api/v1/sandboxes", json={"owner": "a", "template": "desktop"})
    assert r.status_code == 429 and "desktop" in r.json()["detail"]
    assert client.post("/api/v1/sandboxes", json={"owner": "a"}).status_code == 201  # code sandbox still ok


def test_computer_and_browser_endpoints(client, settings):
    sid = client.post("/api/v1/sandboxes", json={"owner": "alice", "template": "desktop"}).json()["id"]
    r = client.post(f"/api/v1/sandboxes/{sid}/desktop/computer", json={"action": "left_click", "coordinate": [10, 20]})
    assert r.status_code == 200 and r.json()["image"]["media_type"] == "image/png"
    r = client.post(f"/api/v1/sandboxes/{sid}/desktop/browser", json={"action": "navigate", "url": "oa.intra"})
    assert r.json()["title"] == "OA"
    sent = calls(settings, sid)
    assert sent[1] == {"action": "left_click", "coordinate": [10, 20], "tool": "computer"}
    assert sent[2]["tool"] == "browser" and sent[2]["url"] == "oa.intra"

    r = client.get(f"/api/v1/sandboxes/{sid}/desktop/screen")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png" and r.content[:4] == b"\x89PNG"

    r = client.post(f"/api/v1/sandboxes/{sid}/desktop/computer", json={"action": "explode"})
    assert r.status_code == 400 and r.json()["detail"] == "boom"
    assert client.post(f"/api/v1/sandboxes/{sid}/desktop/shell", json={"action": "x"}).status_code == 400


def test_typed_text_not_audited(client):
    sid = client.post("/api/v1/sandboxes", json={"owner": "alice", "template": "desktop"}).json()["id"]
    client.post(f"/api/v1/sandboxes/{sid}/desktop/computer", json={"action": "type", "text": "hunter2"})
    client.post(f"/api/v1/sandboxes/{sid}/desktop/computer", json={"action": "screenshot"})
    audit = client.get(f"/api/v1/sandboxes/{sid}/audit").json()
    typed = [a for a in audit if a["action"] == "computer.type"]
    assert typed and typed[0]["detail"]["text_len"] == 7 and "hunter2" not in json.dumps(audit)
    assert not any(a["action"] == "computer.screenshot" for a in audit)


def test_desktop_actions_need_desktop_template(client):
    sid = client.post("/api/v1/sandboxes", json={"owner": "alice"}).json()["id"]
    r = client.post(f"/api/v1/sandboxes/{sid}/desktop/computer", json={"action": "screenshot"})
    assert r.status_code == 409


def test_agent_toolkit_attaches_screenshot(app, make_token):
    headers = make_token("intrallm-agent", "agent")
    sdk = SandboxClient("http://testserver", "unused")
    sdk._http = TestClient(app, headers=headers)
    kit = AgentToolkit(sdk, owner="alice")
    names = {t["function"]["name"] for t in kit.tools}
    assert {"computer", "browser"} <= names

    sid = kit.run("sandbox_create", {"template": "desktop"})["result"]["id"]
    msgs = kit.messages("call_1", "computer", json.dumps({"sandbox_id": sid, "action": "screenshot"}))
    assert msgs[0]["role"] == "tool" and PNG_1PX not in msgs[0]["content"]
    assert msgs[1]["role"] == "user" and msgs[1]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")

    browser = kit.messages("call_2", "browser", {"sandbox_id": sid, "action": "snapshot"})
    assert len(browser) == 1 and "提交" in browser[0]["content"]

    # an agent acting for bob cannot drive alice's desktop
    assert "not found" in kit.client.invoke("computer", {"sandbox_id": sid, "action": "screenshot"}, owner="bob")["error"]

    history = []
    for i in range(5):
        history += kit.messages(f"c{i}", "computer", {"sandbox_id": sid, "action": "screenshot"})
    prune_screenshots(history, keep=2)
    images = [m for m in history if m["role"] == "user" and any(p["type"] == "image_url" for p in m["content"])]
    assert len(images) == 2
