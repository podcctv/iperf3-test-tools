import asyncio
import hashlib
import hmac
from typing import List

import httpx
from fastapi import Body, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .config import settings
from .database import engine, get_db
from .models import Base, Node, TestResult
from .schemas import NodeCreate, NodeRead, NodeWithStatus, TestCreate, TestRead

Base.metadata.create_all(bind=engine)

app = FastAPI(title="iperf3 master api")


def _dashboard_token(password: str) -> str:
    secret = settings.dashboard_secret.encode()
    return hmac.new(secret, password.encode(), hashlib.sha256).hexdigest()


def _is_authenticated(request: Request) -> bool:
    stored = request.cookies.get(settings.dashboard_cookie_name)
    return stored == _dashboard_token(settings.dashboard_password)


def _login_html() -> str:
    return """
<!DOCTYPE html>
<html lang=\"en\">
<head>
  <meta charset=\"UTF-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\" />
  <title>iperf3 Master Dashboard</title>
  <style>
    body { font-family: system-ui, sans-serif; background: #0f172a; color: #e2e8f0; margin: 0; padding: 0; }
    .container { max-width: 1080px; margin: 0 auto; padding: 32px; }
    .card { background: #1e293b; border: 1px solid #334155; border-radius: 12px; padding: 24px; box-shadow: 0 10px 30px rgba(0,0,0,0.25); margin-bottom: 20px; }
    h1, h2, h3 { margin: 0 0 12px; color: #f8fafc; }
    label { display: block; margin: 10px 0 6px; font-weight: 600; }
    input, select, button, textarea { width: 100%; padding: 10px 12px; border-radius: 8px; border: 1px solid #334155; background: #0b1220; color: #e2e8f0; }
    button { background: linear-gradient(120deg, #22c55e, #16a34a); color: #0b1220; font-weight: 700; cursor: pointer; border: none; transition: transform 0.1s ease, box-shadow 0.1s ease; }
    button:hover { transform: translateY(-1px); box-shadow: 0 6px 14px rgba(34,197,94,0.35); }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; }
    .badge { padding: 4px 10px; border-radius: 999px; font-size: 12px; display: inline-block; }
    .badge.online { background: #065f46; color: #d1fae5; }
    .badge.offline { background: #7f1d1d; color: #fee2e2; }
    .muted { color: #94a3b8; font-size: 14px; }
    pre { background: #0b1220; padding: 12px; border-radius: 8px; overflow: auto; border: 1px solid #1f2937; }
    .hidden { display: none; }
    .inline { display: inline-flex; gap: 8px; align-items: center; }
    .alert { padding: 12px; border-radius: 8px; margin-bottom: 12px; }
    .alert.error { background: #7f1d1d; color: #fee2e2; }
    .alert.success { background: #064e3b; color: #bbf7d0; }
    .topbar { display: flex; justify-content: space-between; align-items: center; gap: 12px; margin-bottom: 20px; }
    .topbar button { width: auto; padding: 10px 14px; background: #f97316; color: #0b1220; }
  </style>
</head>
<body>
  <div class=\"container\">
    <div class=\"card\" id=\"login-card\">
      <h1>iperf3 Master Dashboard</h1>
      <p class=\"muted\">Enter the dashboard password to continue.</p>
      <div id=\"login-alert\" class=\"alert error hidden\"></div>
      <label for=\"password\">Password</label>
      <input id=\"password\" type=\"password\" placeholder=\"Enter dashboard password\" />
      <button id=\"login-btn\" style=\"margin-top: 14px;\">Unlock Dashboard</button>
    </div>

    <div class=\"card hidden\" id=\"app-card\">
      <div class=\"topbar\">
        <div>
          <h1 style=\"margin: 0;\">iperf3 Master Dashboard</h1>
          <p class=\"muted\" id=\"auth-hint\"></p>
        </div>
        <button id=\"logout-btn\">Logout</button>
      </div>

      <div class=\"grid\">
        <div class=\"card\">
          <h2>Add Node</h2>
          <div id=\"add-node-alert\" class=\"alert error hidden\"></div>
          <label>Name</label>
          <input id=\"node-name\" placeholder=\"node-a\" />
          <label>IP Address</label>
          <input id=\"node-ip\" placeholder=\"10.0.0.11\" />
          <label>Agent Port</label>
          <input id=\"node-port\" type=\"number\" value=\"8000\" />
          <label>Description (optional)</label>
          <textarea id=\"node-desc\" rows=\"2\"></textarea>
          <button id=\"save-node\" style=\"margin-top: 12px;\">Save Node</button>
        </div>

        <div class=\"card\">
          <h2>Run Test</h2>
          <div id=\"test-alert\" class=\"alert error hidden\"></div>
          <label>Source Node</label>
          <select id=\"src-select\"></select>
          <label>Destination Node</label>
          <select id=\"dst-select\"></select>
          <label>Protocol</label>
          <select id=\"protocol\"><option value=\"tcp\">TCP</option><option value=\"udp\">UDP</option></select>
          <label>Duration (seconds)</label>
          <input id=\"duration\" type=\"number\" value=\"10\" />
          <label>Parallel Streams</label>
          <input id=\"parallel\" type=\"number\" value=\"1\" />
          <label>Port</label>
          <input id=\"test-port\" type=\"number\" value=\"5201\" />
          <button id=\"run-test\" style=\"margin-top: 12px;\">Start Test</button>
        </div>
      </div>

      <div class=\"grid\" style=\"margin-top: 16px;\">
        <div class=\"card\">
          <div class=\"inline\" style=\"justify-content: space-between; width: 100%;\">
            <h2>Nodes</h2>
            <button id=\"refresh-nodes\" style=\"width: auto;\">Refresh</button>
          </div>
          <div id=\"nodes-list\" class=\"muted\">No nodes yet.</div>
        </div>
        <div class=\"card\">
          <div class=\"inline\" style=\"justify-content: space-between; width: 100%;\">
            <h2>Recent Tests</h2>
            <button id=\"refresh-tests\" style=\"width: auto;\">Refresh</button>
          </div>
          <div id=\"tests-list\" class=\"muted\">No tests yet.</div>
        </div>
      </div>
    </div>
  </div>

  <script>
    const loginCard = document.getElementById('login-card');
    const appCard = document.getElementById('app-card');
    const loginAlert = document.getElementById('login-alert');
    const authHint = document.getElementById('auth-hint');

    const nodeName = document.getElementById('node-name');
    const nodeIp = document.getElementById('node-ip');
    const nodePort = document.getElementById('node-port');
    const nodeDesc = document.getElementById('node-desc');
    const nodesList = document.getElementById('nodes-list');
    const testsList = document.getElementById('tests-list');
    const srcSelect = document.getElementById('src-select');
    const dstSelect = document.getElementById('dst-select');
    const addNodeAlert = document.getElementById('add-node-alert');
    const testAlert = document.getElementById('test-alert');

    function show(el) { el.classList.remove('hidden'); }
    function hide(el) { el.classList.add('hidden'); }
    function setAlert(el, message) { el.textContent = message; show(el); }
    function clearAlert(el) { el.textContent = ''; hide(el); }

    async function checkAuth() {
      const res = await fetch('/auth/status');
      const data = await res.json();
      if (data.authenticated) {
        loginCard.classList.add('hidden');
        appCard.classList.remove('hidden');
        authHint.textContent = 'Authenticated. Use the controls below to manage nodes and run tests.';
        await refreshNodes();
        await refreshTests();
      } else {
        appCard.classList.add('hidden');
        loginCard.classList.remove('hidden');
      }
    }

    async function login() {
      clearAlert(loginAlert);
      const password = document.getElementById('password').value;
      const res = await fetch('/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ password })
      });
      if (res.ok) {
        document.getElementById('password').value = '';
        await checkAuth();
      } else {
        setAlert(loginAlert, 'Invalid password.');
      }
    }

    async function logout() {
      await fetch('/auth/logout', { method: 'POST' });
      await checkAuth();
    }

    async function refreshNodes() {
      const res = await fetch('/nodes/status');
      const nodes = await res.json();
      srcSelect.innerHTML = '';
      dstSelect.innerHTML = '';
      if (!nodes.length) {
        nodesList.textContent = 'No nodes yet.';
        return;
      }

      nodesList.innerHTML = '';
      nodes.forEach((node) => {
        const badge = `<span class=\"badge ${node.status}\">${node.status}</span>`;
        const server = node.server_running ? 'running' : 'stopped';
        const item = document.createElement('div');
        item.style.borderBottom = '1px solid #1f2937';
        item.style.padding = '8px 0';
        item.innerHTML = `<div class=\"inline\" style=\"justify-content: space-between; width: 100%;\">` +
          `<div><strong>${node.name}</strong> <span class=\"muted\">(${node.ip}:${node.agent_port})</span><br/>` +
          `<span class=\"muted\">Server: ${server}</span></div>` +
          `${badge}</div>`;
        nodesList.appendChild(item);

        const optionA = document.createElement('option');
        optionA.value = node.id;
        optionA.textContent = `${node.name} (${node.ip})`;
        srcSelect.appendChild(optionA);

        const optionB = document.createElement('option');
        optionB.value = node.id;
        optionB.textContent = `${node.name} (${node.ip})`;
        dstSelect.appendChild(optionB);
      });
    }

    async function refreshTests() {
      const res = await fetch('/tests');
      const tests = await res.json();
      if (!tests.length) {
        testsList.textContent = 'No tests yet.';
        return;
      }
      testsList.innerHTML = '';
      tests.slice().reverse().forEach((test) => {
        const div = document.createElement('div');
        div.style.borderBottom = '1px solid #1f2937';
        div.style.padding = '8px 0';
        const created = test.created_at || 'unknown time';
        div.innerHTML = `<strong>Test #${test.id}</strong> <span class=\"muted\">(${test.protocol.toUpperCase()} @ port ${test.params.port})</span><br/>` +
          `<span class=\"muted\">Source ID: ${test.src_node_id} → Dest ID: ${test.dst_node_id} | Duration: ${test.params.duration}s | Parallel: ${test.params.parallel}</span><br/>` +
          `<details style=\"margin-top:6px;\"><summary>Raw result</summary><pre>${JSON.stringify(test.raw_result, null, 2)}</pre></details>`;
        testsList.appendChild(div);
      });
    }

    async function saveNode() {
      clearAlert(addNodeAlert);
      const payload = {
        name: nodeName.value,
        ip: nodeIp.value,
        agent_port: Number(nodePort.value || 8000),
        description: nodeDesc.value
      };

      const res = await fetch('/nodes', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });

      if (!res.ok) {
        setAlert(addNodeAlert, 'Failed to save node. Check the fields and try again.');
        return;
      }

      nodeName.value = '';
      nodeIp.value = '';
      nodeDesc.value = '';
      await refreshNodes();
      clearAlert(addNodeAlert);
    }

    async function runTest() {
      clearAlert(testAlert);
      const payload = {
        src_node_id: Number(srcSelect.value),
        dst_node_id: Number(dstSelect.value),
        protocol: document.getElementById('protocol').value,
        duration: Number(document.getElementById('duration').value),
        parallel: Number(document.getElementById('parallel').value),
        port: Number(document.getElementById('test-port').value)
      };

      const res = await fetch('/tests', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });

      if (!res.ok) {
        setAlert(testAlert, 'Failed to start test. Ensure nodes exist and parameters are valid.');
        return;
      }

      await refreshTests();
      clearAlert(testAlert);
    }

    document.getElementById('login-btn').addEventListener('click', login);
    document.getElementById('logout-btn').addEventListener('click', logout);
    document.getElementById('save-node').addEventListener('click', saveNode);
    document.getElementById('run-test').addEventListener('click', runTest);
    document.getElementById('refresh-nodes').addEventListener('click', refreshNodes);
    document.getElementById('refresh-tests').addEventListener('click', refreshTests);
    document.getElementById('password').addEventListener('keyup', (e) => { if (e.key === 'Enter') login(); });

    checkAuth();
  </script>
</body>
</html>
    """


@app.get("/web", response_class=HTMLResponse)
def dashboard() -> HTMLResponse:
    """Serve the dashboard shell; authentication is handled in-page."""

    return HTMLResponse(content=_login_html())


@app.get("/auth/status")
def auth_status(request: Request) -> dict:
    return {"authenticated": _is_authenticated(request)}


@app.post("/auth/login")
def login(response: Response, payload: dict = Body(...)) -> dict:
    password = str(payload.get("password", ""))
    if password != settings.dashboard_password:
        raise HTTPException(status_code=401, detail="invalid_password")

    response.set_cookie(
        settings.dashboard_cookie_name,
        _dashboard_token(settings.dashboard_password),
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 24,
    )
    return {"status": "ok"}


@app.post("/auth/logout")
def logout(response: Response) -> dict:
    response.delete_cookie(settings.dashboard_cookie_name)
    return {"status": "logged_out"}


@app.get("/")
def root() -> dict:
    """Provide a simple landing response instead of a 404."""

    return {
        "message": "iperf3 master api",
        "docs_url": "/docs",
        "health_url": "/health",
    }


async def _check_node_health(node: Node) -> NodeWithStatus:
    url = f"http://{node.ip}:{node.agent_port}/health"
    try:
        async with httpx.AsyncClient(timeout=settings.request_timeout) as client:
            response = await client.get(url)
            if response.status_code == 200:
                data = response.json()
                return NodeWithStatus(
                    id=node.id,
                    name=node.name,
                    ip=node.ip,
                    agent_port=node.agent_port,
                    description=node.description,
                    status="online",
                    server_running=bool(data.get("server_running")),
                    health_timestamp=data.get("timestamp"),
                )
    except Exception:
        pass

    return NodeWithStatus(
        id=node.id,
        name=node.name,
        ip=node.ip,
        agent_port=node.agent_port,
        description=node.description,
        status="offline",
        server_running=None,
        health_timestamp=None,
    )


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/nodes", response_model=NodeRead)
def create_node(node: NodeCreate, db: Session = Depends(get_db)):
    obj = Node(
        name=node.name,
        ip=node.ip,
        agent_port=node.agent_port,
        description=node.description,
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@app.get("/nodes", response_model=List[NodeRead])
def list_nodes(db: Session = Depends(get_db)):
    nodes = db.scalars(select(Node)).all()
    return nodes


@app.get("/nodes/status", response_model=List[NodeWithStatus])
async def nodes_with_status(db: Session = Depends(get_db)):
    nodes = db.scalars(select(Node)).all()
    results = await asyncio.gather(*[_check_node_health(node) for node in nodes])
    return results


@app.post("/tests", response_model=TestRead)
async def create_test(test: TestCreate, db: Session = Depends(get_db)):
    src = db.get(Node, test.src_node_id)
    dst = db.get(Node, test.dst_node_id)
    if not src or not dst:
        raise HTTPException(status_code=404, detail="node not found")

    agent_url = f"http://{src.ip}:{src.agent_port}/run_test"
    payload = {
        "target": dst.ip,
        "port": test.port,
        "duration": test.duration,
        "protocol": test.protocol,
        "parallel": test.parallel,
    }

    async with httpx.AsyncClient(timeout=test.duration + settings.request_timeout) as client:
        response = await client.post(agent_url, json=payload)
    if response.status_code != 200:
        raise HTTPException(status_code=500, detail=response.text)

    raw_data = response.json()
    obj = TestResult(
        src_node_id=src.id,
        dst_node_id=dst.id,
        protocol=test.protocol,
        params=payload,
        raw_result=raw_data,
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    return obj


@app.get("/tests", response_model=List[TestRead])
def list_tests(db: Session = Depends(get_db)):
    results = db.scalars(select(TestResult)).all()
    return results
