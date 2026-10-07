import time


def test_requires_auth(client):
    assert client.get("/api/v1/sandboxes", headers={"Authorization": ""}).status_code == 401
    assert client.get("/api/v1/sandboxes", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_lifecycle(client):
    r = client.post("/api/v1/sandboxes", json={"owner": "alice", "name": "demo"})
    assert r.status_code == 201, r.text
    sb = r.json()
    assert sb["owner"] == "alice" and sb["status"] == "running" and "handle" not in sb
    sid = sb["id"]

    r = client.put(f"/api/v1/sandboxes/{sid}/files", json={"path": "hello.py", "content": "print(6*7)"})
    assert r.json()["bytes"] == 10
    r = client.post(f"/api/v1/sandboxes/{sid}/exec", json={"command": "python3 hello.py"})
    assert r.json()["exit_code"] == 0 and r.json()["stdout"].strip() == "42"
    assert client.get(f"/api/v1/sandboxes/{sid}/files", params={"path": "/workspace/hello.py"}).json()["content"] == "print(6*7)"
    assert [e["name"] for e in client.get(f"/api/v1/sandboxes/{sid}/ls").json()] == ["hello.py"]

    r = client.post(f"/api/v1/sandboxes/{sid}/exec", json={"command": "sleep 5", "timeout": 1})
    assert r.json()["timed_out"] is True

    assert client.get(f"/api/v1/sandboxes/{sid}").json()["exec_count"] == 2
    actions = [a["action"] for a in client.get(f"/api/v1/sandboxes/{sid}/audit").json()]
    assert actions[-1] == "create" and "exec" in actions and "write_file" in actions

    r = client.delete(f"/api/v1/sandboxes/{sid}")
    assert r.json()["status"] == "terminated"
    assert client.post(f"/api/v1/sandboxes/{sid}/exec", json={"command": "true"}).status_code == 409


def test_path_escape_blocked(client):
    sid = client.post("/api/v1/sandboxes", json={"owner": "alice"}).json()["id"]
    r = client.put(f"/api/v1/sandboxes/{sid}/files", json={"path": "../../evil", "content": "x"})
    assert r.status_code == 400


def test_quotas(client):
    for _ in range(2):
        assert client.post("/api/v1/sandboxes", json={"owner": "bob"}).status_code == 201
    r = client.post("/api/v1/sandboxes", json={"owner": "bob"})
    assert r.status_code == 429 and "bob" in r.json()["detail"]
    assert client.post("/api/v1/sandboxes", json={"owner": "carol"}).status_code == 201
    assert client.post("/api/v1/sandboxes", json={"owner": "dave"}).status_code == 429  # max_total=3


def test_resource_validation(client):
    assert client.post("/api/v1/sandboxes", json={"cpu": 100}).status_code == 400
    assert client.post("/api/v1/sandboxes", json={"memory_mb": 1}).status_code == 400


def test_role_isolation(client, make_token):
    agent = make_token("intrallm-agent", "agent")
    other_agent = make_token("other-agent", "agent")
    alice = make_token("alice", "user")
    bob = make_token("bob", "user")

    sid = client.post("/api/v1/sandboxes", json={"owner": "alice"}, headers=agent).json()["id"]
    assert client.get(f"/api/v1/sandboxes/{sid}", headers=alice).status_code == 200
    assert client.get(f"/api/v1/sandboxes/{sid}", headers=bob).status_code == 404
    assert client.get(f"/api/v1/sandboxes/{sid}", headers=other_agent).status_code == 404
    assert [s["id"] for s in client.get("/api/v1/sandboxes", headers=alice).json()] == [sid]
    assert client.get("/api/v1/sandboxes", headers=bob).json() == []

    # users cannot allocate to others, and cannot manage tokens
    assert client.post("/api/v1/sandboxes", json={"owner": "alice"}, headers=bob).status_code == 403
    assert client.post("/api/v1/sandboxes", json={}, headers=bob).json()["owner"] == "bob"
    assert client.get("/api/v1/tokens", headers=bob).status_code == 403

    # summary: users don't see host data
    s = client.get("/api/v1/dashboard/summary", headers=alice).json()
    assert s["host"] is None and s["running"] == 1
    assert client.get("/api/v1/dashboard/summary").json()["host"]["cpu_count"] >= 1


def test_revoke_token(client, make_token):
    h = make_token("eve", "user")
    tid = client.get("/api/v1/tokens").json()[0]["id"]
    assert client.delete(f"/api/v1/tokens/{tid}").status_code == 200
    assert client.get("/api/v1/whoami", headers=h).status_code == 401


def test_metrics_and_summary(client, app):
    sid = client.post("/api/v1/sandboxes", json={"owner": "alice"}).json()["id"]
    client.post(f"/api/v1/sandboxes/{sid}/exec", json={"command": "true"})
    app.state.manager.sample_metrics()
    assert len(client.get(f"/api/v1/sandboxes/{sid}/metrics").json()) == 1
    s = client.get("/api/v1/dashboard/summary").json()
    assert s["running"] == 1 and s["owners"][0]["owner"] == "alice"
    assert len(s["host_history"]) == 1
    prom = client.get("/metrics").text
    assert "intrallm_sandbox_active 1" in prom and f'sandbox="{sid}"' in prom


def test_reaper_ttl_and_idle(client, app, settings):
    m = app.state.manager
    sid = client.post("/api/v1/sandboxes", json={"owner": "alice"}).json()["id"]
    m.db.update_sandbox(sid, expires_at=time.time() - 1)
    m.reap()
    sb = client.get(f"/api/v1/sandboxes/{sid}").json()
    assert sb["status"] == "terminated" and sb["terminate_reason"] == "ttl expired"

    sid2 = client.post("/api/v1/sandboxes", json={"owner": "alice"}).json()["id"]
    m.db.update_sandbox(sid2, last_active_at=time.time() - settings.idle_timeout - 1)
    m.reap()
    assert client.get(f"/api/v1/sandboxes/{sid2}").json()["terminate_reason"] == "idle timeout"


def test_extend_and_reassign(client):
    sb = client.post("/api/v1/sandboxes", json={"owner": "alice", "ttl_seconds": 60}).json()
    ext = client.post(f"/api/v1/sandboxes/{sb['id']}/extend", json={"seconds": 600}).json()
    assert ext["expires_at"] > sb["expires_at"] + 500
    assert client.post(f"/api/v1/sandboxes/{sb['id']}/reassign", json={"owner": "bob"}).json()["owner"] == "bob"


def test_dashboard_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "IntraLLM Sandbox" in r.text
    assert client.get("/static/app.js").status_code == 200
