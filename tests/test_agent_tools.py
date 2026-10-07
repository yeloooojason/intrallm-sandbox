import json

from fastapi.testclient import TestClient

from intrallm_sandbox.client import AgentToolkit, SandboxClient


def invoke(client, headers, tool, args, owner="alice"):
    r = client.post("/api/v1/agent/invoke", json={"tool": tool, "arguments": args, "owner": owner}, headers=headers)
    assert r.status_code == 200, r.text
    return r.json()


def test_tool_schema(client):
    tools = client.get("/api/v1/agent/tools").json()
    names = {t["function"]["name"] for t in tools}
    assert {"sandbox_create", "sandbox_exec", "sandbox_write_file", "sandbox_destroy"} <= names
    for t in tools:
        assert t["type"] == "function" and t["function"]["parameters"]["type"] == "object"


def test_agent_flow(client, make_token):
    agent = make_token("intrallm-agent", "agent")
    r = invoke(client, agent, "sandbox_create", {"name": "analysis"})
    assert r["ok"] and r["result"]["owner"] == "alice"
    sid = r["result"]["id"]

    assert invoke(client, agent, "sandbox_write_file", {"sandbox_id": sid, "path": "a.txt", "content": "hi"})["ok"]
    r = invoke(client, agent, "sandbox_exec", {"sandbox_id": sid, "command": "cat a.txt; exit 3"})
    assert r["result"]["stdout"] == "hi" and r["result"]["exit_code"] == 3
    assert invoke(client, agent, "sandbox_read_file", {"sandbox_id": sid, "path": "a.txt"})["result"]["content"] == "hi"
    assert invoke(client, agent, "sandbox_list_files", {"sandbox_id": sid})["result"][0]["name"] == "a.txt"
    assert [s["id"] for s in invoke(client, agent, "sandbox_list", {})["result"]] == [sid]

    # acting for a different user: alice's sandbox is invisible
    r = invoke(client, agent, "sandbox_exec", {"sandbox_id": sid, "command": "true"}, owner="bob")
    assert not r["ok"] and "not found" in r["error"]
    assert invoke(client, agent, "sandbox_list", {}, owner="bob")["result"] == []

    assert invoke(client, agent, "sandbox_destroy", {"sandbox_id": sid})["result"]["status"] == "terminated"


def test_agent_errors_are_returned(client, make_token):
    agent = make_token("intrallm-agent", "agent")
    assert "unknown tool" in invoke(client, agent, "rm_rf", {})["error"]
    assert "sandbox_id" in invoke(client, agent, "sandbox_exec", {"command": "ls"})["error"]
    sid = invoke(client, agent, "sandbox_create", {})["result"]["id"]
    assert "missing argument" in invoke(client, agent, "sandbox_exec", {"sandbox_id": sid})["error"]
    assert not invoke(client, agent, "sandbox_create", {"cpu": 999})["ok"]


def test_python_toolkit(app, make_token):
    headers = make_token("intrallm-agent", "agent")
    sdk = SandboxClient("http://testserver", headers["Authorization"].split()[1])
    sdk._http = TestClient(app, headers=headers)  # route the SDK through the in-process app
    kit = AgentToolkit(sdk, owner="alice")
    assert any(t["function"]["name"] == "sandbox_exec" for t in kit.tools)
    created = json.loads(kit.call("sandbox_create", "{}"))
    out = json.loads(kit.call("sandbox_exec", json.dumps({"sandbox_id": created["result"]["id"], "command": "echo ok"})))
    assert out["result"]["stdout"].strip() == "ok"
    assert not json.loads(kit.call("sandbox_exec", "{not json"))["ok"]
