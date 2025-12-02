import asyncio
import hashlib
import hmac
import logging
from datetime import datetime, timezone
from typing import Dict, List

import httpx
from fastapi import Body, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from sqlalchemy import or_, select, text
from sqlalchemy.orm import Session

from .config import settings
from .database import SessionLocal, engine, get_db
from .agent_store import AgentConfigStore
from .models import Base, Node, TestResult, TestSchedule
from .remote_agent import fetch_agent_logs, redeploy_agent, remove_agent_container, RemoteCommandError
from .schemas import (
    AgentActionResult,
    AgentConfigCreate,
    AgentConfigRead,
    AgentConfigUpdate,
    NodeCreate,
    NodeUpdate,
    NodeRead,
    NodeWithStatus,
    TestScheduleCreate,
    TestScheduleRead,
    TestScheduleUpdate,
    TestCreate,
    TestRead,
)
from .state_store import StateStore

Base.metadata.create_all(bind=engine)


def _ensure_iperf_port_column() -> None:
    if engine.dialect.name != "sqlite":
        return

    with engine.connect() as connection:
        columns = connection.exec_driver_sql("PRAGMA table_info(nodes)").fetchall()
        if not any(col[1] == "iperf_port" for col in columns):
            connection.exec_driver_sql("ALTER TABLE nodes ADD COLUMN iperf_port INTEGER DEFAULT 5201")
            connection.commit()


def _ensure_test_result_columns() -> None:
    dialect = engine.dialect.name
    with engine.connect() as connection:
        if dialect == "sqlite":
            columns = connection.exec_driver_sql("PRAGMA table_info(test_results)").fetchall()
            column_names = {col[1] for col in columns}
            if "summary" not in column_names:
                connection.exec_driver_sql("ALTER TABLE test_results ADD COLUMN summary JSON")
            if "created_at" not in column_names:
                connection.exec_driver_sql("ALTER TABLE test_results ADD COLUMN created_at DATETIME")
        elif dialect == "postgresql":
            result = connection.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name='test_results'"
                )
            )
            column_names = {row[0] for row in result}
            if "summary" not in column_names:
                connection.execute(text("ALTER TABLE test_results ADD COLUMN summary JSONB"))
            if "created_at" not in column_names:
                connection.execute(text("ALTER TABLE test_results ADD COLUMN created_at TIMESTAMPTZ"))
        connection.commit()


_ensure_iperf_port_column()
_ensure_test_result_columns()

state_store = StateStore(settings.state_file, settings.state_recent_tests)
logger = logging.getLogger(__name__)


def _bootstrap_state() -> None:
    db = SessionLocal()
    try:
        state_store.restore(db)
    finally:
        db.close()


_bootstrap_state()

app = FastAPI(title="iperf3 master api")
agent_store = AgentConfigStore(settings.agent_config_file)
health_monitor = NodeHealthMonitor(settings.health_check_interval)


@app.on_event("startup")
async def _on_startup() -> None:
    await health_monitor.start()


@app.on_event("shutdown")
async def _on_shutdown() -> None:
    await health_monitor.stop()


def _persist_state(db: Session) -> None:
    state_store.persist(db)


def _summarize_metrics(raw: dict | None) -> dict | None:
    if not raw:
        return None

    body = raw.get("iperf_result") if isinstance(raw, dict) else None
    result = body or raw
    end = result.get("end", {}) if isinstance(result, dict) else {}
    sum_received = end.get("sum_received") or end.get("sum") or {}
    sum_sent = end.get("sum_sent") or end.get("sum") or {}
    streams = end.get("streams") or []
    first_stream = streams[0] if streams else None
    receiver_stream = (first_stream or {}).get("receiver") if isinstance(first_stream, dict) else None
    sender_stream = (first_stream or {}).get("sender") if isinstance(first_stream, dict) else None

    def _metric(*values):
        for value in values:
            if value is not None:
                return value
        return None

    bits_per_second = _metric(
        (sum_received or {}).get("bits_per_second"),
        (receiver_stream or {}).get("bits_per_second") if receiver_stream else None,
        (sum_sent or {}).get("bits_per_second"),
        (sender_stream or {}).get("bits_per_second") if sender_stream else None,
    )

    jitter_ms = _metric(
        (sum_received or {}).get("jitter_ms"),
        (sum_sent or {}).get("jitter_ms"),
        (receiver_stream or {}).get("jitter_ms") if receiver_stream else None,
        (sender_stream or {}).get("jitter_ms") if sender_stream else None,
    )

    lost_percent = _metric(
        (sum_received or {}).get("lost_percent"),
        (sum_sent or {}).get("lost_percent"),
        (receiver_stream or {}).get("lost_percent") if receiver_stream else None,
        (sender_stream or {}).get("lost_percent") if sender_stream else None,
    )

    if lost_percent is None and sum_received:
        lost_packets = sum_received.get("lost_packets")
        packets = sum_received.get("packets")
        if lost_packets is not None and packets:
            lost_percent = (lost_packets / packets) * 100

    latency_ms = _metric(
        (sender_stream or {}).get("mean_rtt") if sender_stream else None,
        (sender_stream or {}).get("rtt") if sender_stream else None,
        (receiver_stream or {}).get("mean_rtt") if receiver_stream else None,
        (receiver_stream or {}).get("rtt") if receiver_stream else None,
    )

    if latency_ms and latency_ms > 1000:
        latency_ms = latency_ms / 1000

    return {
        "bits_per_second": bits_per_second,
        "jitter_ms": jitter_ms,
        "lost_percent": lost_percent,
        "latency_ms": latency_ms,
    }


def _dashboard_token(password: str) -> str:
    secret = settings.dashboard_secret.encode()
    return hmac.new(secret, password.encode(), hashlib.sha256).hexdigest()


def _is_authenticated(request: Request) -> bool:
    stored = request.cookies.get(settings.dashboard_cookie_name)
    return stored == _dashboard_token(settings.dashboard_password)


class NodeHealthMonitor:
    def __init__(self, interval_seconds: int = 30) -> None:
        self.interval_seconds = interval_seconds
        self._task: asyncio.Task | None = None
        self._cache: Dict[int, NodeWithStatus] = {}
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        if self._task:
            return

        await self.refresh()
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _run(self) -> None:
        while True:
            try:
                await self.refresh()
            except Exception:
                logger.exception("Failed to refresh node health")
            await asyncio.sleep(self.interval_seconds)

    async def refresh(self, nodes: List[Node] | None = None) -> List[NodeWithStatus]:
        async with self._lock:
            if nodes is None:
                db = SessionLocal()
                try:
                    nodes = db.scalars(select(Node)).all()
                finally:
                    db.close()

            statuses = await asyncio.gather(*[_check_node_health(node) for node in nodes])
            now_ts = int(datetime.now(timezone.utc).timestamp())
            for status in statuses:
                status.checked_at = now_ts
            self._cache = {status.id: status for status in statuses}
            return statuses

    async def get_statuses(self) -> List[NodeWithStatus]:
        db = SessionLocal()
        try:
            nodes = db.scalars(select(Node)).all()
        finally:
            db.close()

        cached_ids = set(self._cache.keys())
        current_ids = {node.id for node in nodes}
        if not self._cache or cached_ids != current_ids:
            return await self.refresh(nodes)
        return list(self._cache.values())

    async def check_node(self, node: Node) -> NodeWithStatus:
        status = await _check_node_health(node)
        status.checked_at = int(datetime.now(timezone.utc).timestamp())
        self._cache[node.id] = status
        return status


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
    .pill { display: inline-flex; align-items: center; gap: 6px; padding: 6px 12px; border-radius: 999px; font-size: 13px; font-weight: 700; border: none; cursor: pointer; }
    .pill.tag { cursor: default; }
    .pill.success { background: #22c55e; color: #0b1220; }
    .pill.warn { background: #f59e0b; color: #0b1220; }
    .pill.info { background: #38bdf8; color: #0b1220; }
    .pill.danger { background: #ef4444; color: #0b1220; }
    .pill.muted { background: #475569; color: #e2e8f0; }
    .muted { color: #94a3b8; font-size: 14px; }
    pre { background: #0b1220; padding: 12px; border-radius: 8px; overflow: auto; border: 1px solid #1f2937; }
    .hidden { display: none; }
    .inline { display: inline-flex; gap: 8px; align-items: center; }
    .alert { padding: 12px; border-radius: 8px; margin-bottom: 12px; }
    .alert.error { background: #7f1d1d; color: #fee2e2; }
    .alert.success { background: #064e3b; color: #bbf7d0; }
    .topbar { display: flex; justify-content: space-between; align-items: center; gap: 12px; margin-bottom: 20px; }
    .topbar button { width: auto; padding: 10px 14px; background: #f97316; color: #0b1220; }
    table { width: 100%; border-collapse: collapse; }
    th, td { padding: 8px 10px; border-bottom: 1px solid #1f2937; text-align: left; }
    th { color: #cbd5e1; font-weight: 700; }
    .flex-between { display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap; }
    .muted-sm { color: #94a3b8; font-size: 13px; }
    .node-row { border-bottom: 1px solid #1f2937; padding: 10px 0; }
    .input-inline { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 8px; width: 100%; }
    .test-block { border-bottom: 1px solid #1f2937; padding: 10px 0; }
    .table-scroll { overflow: auto; }
    .raw-table { width: 100%; border-collapse: collapse; }
    .raw-table th, .raw-table td { padding: 6px 8px; border-bottom: 1px solid #1f2937; }
    .row-list { display: flex; flex-direction: column; gap: 12px; }
    .config-warning { color: #fbbf24; font-size: 13px; margin-top: 6px; display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
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
          <label>iperf Port</label>
          <input id=\"node-iperf-port\" type=\"number\" value=\"5201\" />
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

      <div class=\"grid\" style=\"margin-top: 16px; grid-template-columns: 1fr;\">
        <div class=\"card\">
          <div class=\"inline\" style=\"justify-content: space-between; width: 100%;\">
            <h2>Nodes</h2>
            <button id=\"refresh-nodes\" style=\"width: auto;\">Refresh</button>
          </div>
          <div id=\"nodes-list\" class=\"muted row-list\">No nodes yet.</div>
        </div>
      </div>

      <div class=\"grid\" style=\"margin-top: 8px; grid-template-columns: 1fr;\">
        <div class=\"card\">
          <div class=\"inline\" style=\"justify-content: space-between; width: 100%;\">
            <h2>Recent Tests</h2>
            <div class=\"inline\" style=\"gap: 8px;\">
              <button id=\"refresh-tests\" style=\"width: auto;\">Refresh</button>
              <button id=\"delete-all-tests\" style=\"width: auto; background: #ef4444;\">Delete All</button>
            </div>
          </div>
          <div id=\"tests-list\" class=\"muted row-list\">No tests yet.</div>
        </div>
      </div>

      <div class=\"card\">
        <div class=\"inline\" style=\"justify-content: space-between; width: 100%;\">
          <h2>Scheduled Tests</h2>
          <button id=\"refresh-schedules\" style=\"width: auto;\">Refresh</button>
        </div>
        <div class=\"grid\">
          <div>
            <label>Schedule Name</label>
            <input id=\"schedule-name\" placeholder=\"nightly tcp baseline\" />
          </div>
          <div>
            <label>Source Node</label>
            <select id=\"schedule-src\"></select>
          </div>
          <div>
            <label>Destination Node</label>
            <select id=\"schedule-dst\"></select>
          </div>
          <div>
            <label>Protocol</label>
            <select id=\"schedule-protocol\"><option value=\"tcp\">TCP</option><option value=\"udp\">UDP</option></select>
          </div>
          <div>
            <label>Duration (s)</label>
            <input id=\"schedule-duration\" type=\"number\" value=\"10\" />
          </div>
          <div>
            <label>Parallel Streams</label>
            <input id=\"schedule-parallel\" type=\"number\" value=\"1\" />
          </div>
          <div>
            <label>Port</label>
            <input id=\"schedule-port\" type=\"number\" value=\"5201\" />
          </div>
          <div>
            <label>Interval (minutes)</label>
            <input id=\"schedule-interval\" type=\"number\" value=\"60\" />
          </div>
          <div>
            <label>Notes (optional)</label>
            <input id=\"schedule-notes\" placeholder=\"for weekly report\" />
          </div>
        </div>
        <button id=\"save-schedule\" style=\"margin-top: 12px;\">Save Schedule</button>
        <div id=\"schedules-list\" class=\"muted\" style=\"margin-top: 12px;\">No schedules yet.</div>
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
      const nodeIperf = document.getElementById('node-iperf-port');
      const nodeDesc = document.getElementById('node-desc');
      const nodesList = document.getElementById('nodes-list');
      const testsList = document.getElementById('tests-list');
      const schedulesList = document.getElementById('schedules-list');
      const saveNodeBtn = document.getElementById('save-node');
      const saveScheduleBtn = document.getElementById('save-schedule');
      const srcSelect = document.getElementById('src-select');
      const dstSelect = document.getElementById('dst-select');
      const scheduleSrcSelect = document.getElementById('schedule-src');
      const scheduleDstSelect = document.getElementById('schedule-dst');
      const scheduleProtocol = document.getElementById('schedule-protocol');
      const scheduleDuration = document.getElementById('schedule-duration');
      const scheduleParallel = document.getElementById('schedule-parallel');
      const schedulePort = document.getElementById('schedule-port');
      const scheduleInterval = document.getElementById('schedule-interval');
      const scheduleNotes = document.getElementById('schedule-notes');
      const scheduleName = document.getElementById('schedule-name');
      const addNodeAlert = document.getElementById('add-node-alert');
      const testAlert = document.getElementById('test-alert');
      const deleteAllTestsBtn = document.getElementById('delete-all-tests');
      const testPortInput = document.getElementById('test-port');
      let nodeCache = [];
      let editingNodeId = null;


    function show(el) { el.classList.remove('hidden'); }
    function hide(el) { el.classList.add('hidden'); }
    function setAlert(el, message) { el.textContent = message; show(el); }
    function clearAlert(el) { el.textContent = ''; hide(el); }


    function resetNodeForm() {
      nodeName.value = '';
      nodeIp.value = '';
      nodePort.value = 8000;
      nodeIperf.value = 5201;
      nodeDesc.value = '';
      editingNodeId = null;
      saveNodeBtn.textContent = 'Save Node';
    }


    async function removeNode(nodeId) {
      clearAlert(addNodeAlert);
      const confirmDelete = confirm('Delete this node and any related tests?');
      if (!confirmDelete) return;

      const res = await fetch(`/nodes/${nodeId}`, { method: 'DELETE' });
      if (!res.ok) {
        setAlert(addNodeAlert, 'Failed to delete node.');
        return;
      }

      if (editingNodeId === nodeId) {
        resetNodeForm();
      }

      await refreshNodes();
      await refreshTests();
    }


    async function checkAuth() {
      const res = await fetch('/auth/status');
      const data = await res.json();
        if (data.authenticated) {
          loginCard.classList.add('hidden');
          appCard.classList.remove('hidden');
          authHint.textContent = 'Authenticated. Use the controls below to manage nodes and run tests.';
          await refreshNodes();
          await refreshTests();
          await refreshSchedules();
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

    function syncTestPort() {
      const selected = nodeCache.find((n) => n.id === Number(dstSelect.value));
      if (selected && testPortInput) {
        const preferredPort = selected.detected_iperf_port || selected.iperf_port || selected.agent_port || 5201;
        testPortInput.value = preferredPort;
      }
    }


    async function saveNodeInline(nodeId, payload) {
      const res = await fetch(`/nodes/${nodeId}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });

      if (!res.ok) {
        setAlert(addNodeAlert, 'Failed to update node. Check the fields and try again.');
        return false;
      }
      editingNodeId = null;
      await refreshNodes();
      clearAlert(addNodeAlert);
      return true;
    }


    async function refreshNodes() {
      const res = await fetch('/nodes/status');
      nodeCache = await res.json();
      srcSelect.innerHTML = '';
      dstSelect.innerHTML = '';
      scheduleSrcSelect.innerHTML = '';
      scheduleDstSelect.innerHTML = '';
      if (!nodeCache.length) {
        nodesList.textContent = 'No nodes yet.';
        return;
      }

      nodesList.innerHTML = '';
      nodeCache.forEach((node) => {
        const badge = `<span class=\"badge ${node.status}\">${node.status}</span>`;
        const server = node.server_running ? 'running' : 'stopped';
        const isEditing = editingNodeId === node.id;
        const item = document.createElement('div');
        item.className = 'node-row';

        const detectedPort = node.detected_iperf_port;
        const portLabel = detectedPort && detectedPort !== node.iperf_port
          ? `iperf ${node.iperf_port} • agent reports ${detectedPort}`
          : `iperf ${node.iperf_port || detectedPort || 'n/a'}`;

        const header = document.createElement('div');
        header.className = 'flex-between';
        const info = document.createElement('div');
        info.innerHTML = `<strong>${node.name}</strong> <span class=\"muted\">(${node.ip}:${node.agent_port}, ${portLabel})</span><br/>` +
          `<span class=\"muted-sm\">${node.description || 'No description'} | Server: ${server}</span>`;
        header.appendChild(info);

        const actions = document.createElement('div');
        actions.className = 'inline';
        actions.style.gap = '8px';

        const statusBadge = document.createElement('span');
        statusBadge.className = `pill tag ${node.status === 'online' ? 'success' : 'danger'}`;
        statusBadge.textContent = node.status;
        actions.appendChild(statusBadge);

        const editBtn = document.createElement('button');
        editBtn.textContent = isEditing ? 'Editing' : 'Edit';
        editBtn.disabled = isEditing;
        editBtn.style.width = 'auto';
        editBtn.className = 'pill info';
        editBtn.onclick = () => { editingNodeId = node.id; refreshNodes(); };
        actions.appendChild(editBtn);

        const deleteBtn = document.createElement('button');
        deleteBtn.textContent = 'Delete';
        deleteBtn.style.width = 'auto';
        deleteBtn.className = 'pill danger';
        deleteBtn.onclick = () => removeNode(node.id);
        actions.appendChild(deleteBtn);

        header.appendChild(actions);
        item.appendChild(header);

        if (detectedPort && detectedPort !== node.iperf_port) {
          const warning = document.createElement('div');
          warning.className = 'config-warning';
          warning.textContent = `Agent reports iperf port ${detectedPort}. Configured ${node.iperf_port || 'n/a'}.`;

          const applyBtn = document.createElement('button');
          applyBtn.className = 'pill warn';
          applyBtn.style.width = 'auto';
          applyBtn.textContent = 'Apply detected port';
          applyBtn.onclick = () => saveNodeInline(node.id, { iperf_port: detectedPort });

          warning.appendChild(applyBtn);
          item.appendChild(warning);
        }

        if (isEditing) {
          const form = document.createElement('div');
          form.className = 'input-inline';

          const nameInput = document.createElement('input');
          nameInput.value = node.name || '';
          nameInput.placeholder = 'Name';

          const ipInput = document.createElement('input');
          ipInput.value = node.ip || '';
          ipInput.placeholder = 'IP';

          const agentPortInput = document.createElement('input');
          agentPortInput.type = 'number';
          agentPortInput.value = node.agent_port || 8000;
          agentPortInput.placeholder = 'Agent port';

          const iperfPortInput = document.createElement('input');
          iperfPortInput.type = 'number';
          iperfPortInput.value = node.iperf_port || 5201;
          iperfPortInput.placeholder = 'iperf port';

          const descInput = document.createElement('input');
          descInput.value = node.description || '';
          descInput.placeholder = 'Description';

          form.appendChild(nameInput);
          form.appendChild(ipInput);
          form.appendChild(agentPortInput);
          form.appendChild(iperfPortInput);
          form.appendChild(descInput);
          item.appendChild(form);

          const buttonBar = document.createElement('div');
          buttonBar.className = 'inline';
          buttonBar.style.gap = '10px';
          buttonBar.style.marginTop = '8px';

          const saveBtn = document.createElement('button');
          saveBtn.textContent = 'Save';
          saveBtn.style.width = 'auto';
          saveBtn.onclick = () => saveNodeInline(node.id, {
            name: nameInput.value,
            ip: ipInput.value,
            agent_port: Number(agentPortInput.value || 8000),
            iperf_port: Number(iperfPortInput.value || 5201),
            description: descInput.value,
          });
          buttonBar.appendChild(saveBtn);

          const cancelBtn = document.createElement('button');
          cancelBtn.textContent = 'Cancel';
          cancelBtn.style.width = 'auto';
          cancelBtn.style.background = '#64748b';
          cancelBtn.onclick = () => { editingNodeId = null; refreshNodes(); };
          buttonBar.appendChild(cancelBtn);

          item.appendChild(buttonBar);
        }

        nodesList.appendChild(item);

        const optionA = document.createElement('option');
        optionA.value = node.id;
        optionA.textContent = `${node.name} (${node.ip} | iperf ${node.iperf_port})`;
        srcSelect.appendChild(optionA);

        const optionB = document.createElement('option');
        optionB.value = node.id;
        optionB.textContent = `${node.name} (${node.ip} | iperf ${node.iperf_port})`;
        dstSelect.appendChild(optionB);

        const schedSrcOption = optionA.cloneNode(true);
        const schedDstOption = optionB.cloneNode(true);
        scheduleSrcSelect.appendChild(schedSrcOption);
        scheduleDstSelect.appendChild(schedDstOption);
      });

      syncTestPort();
    }

    function summarizeTestMetrics(raw) {
      const body = (raw && raw.iperf_result) || raw || {};
      const end = (body && body.end) || {};
      const sumReceived = end.sum_received || end.sum;
      const sumSent = end.sum_sent || end.sum;
      const firstStream = (end.streams && end.streams.length) ? end.streams[0] : null;
      const receiverStream = firstStream && firstStream.receiver ? firstStream.receiver : null;
      const senderStream = firstStream && firstStream.sender ? firstStream.sender : null;

      const bitsPerSecond =
        (sumReceived && sumReceived.bits_per_second) ||
        (receiverStream && receiverStream.bits_per_second) ||
        (sumSent && sumSent.bits_per_second) ||
        (senderStream && senderStream.bits_per_second);

      const jitterMs =
        (sumReceived && sumReceived.jitter_ms) ||
        (sumSent && sumSent.jitter_ms) ||
        (receiverStream && receiverStream.jitter_ms) ||
        (senderStream && senderStream.jitter_ms);

      const lostPercent =
        (sumReceived && (sumReceived.lost_percent ?? (sumReceived.lost_packets && sumReceived.packets ? (sumReceived.lost_packets / sumReceived.packets) * 100 : undefined))) ||
        (sumSent && sumSent.lost_percent) ||
        (receiverStream && receiverStream.lost_percent) ||
        (senderStream && senderStream.lost_percent);

      let latencyMs =
        (senderStream && (senderStream.mean_rtt || senderStream.rtt)) ||
        (receiverStream && (receiverStream.mean_rtt || receiverStream.rtt));
      if (latencyMs && latencyMs > 1000) {
        latencyMs = latencyMs / 1000; // convert microseconds to milliseconds if needed
      }

      return { bitsPerSecond, jitterMs, lostPercent, latencyMs };
    }


    function formatMetric(value, decimals = 2) {
      if (value === undefined || value === null || Number.isNaN(value)) return 'N/A';
      return Number(value).toFixed(decimals);
    }


    function renderRawResult(raw) {
      const wrap = document.createElement('div');
      wrap.className = 'table-scroll';

      if (!raw) {
        wrap.textContent = 'No raw result available.';
        return wrap;
      }

      const result = raw.iperf_result || raw;
      const end = result.end || {};
      const sumSent = end.sum_sent || {};
      const sumReceived = end.sum_received || {};

      const summaryTable = document.createElement('table');
      summaryTable.className = 'raw-table';

      const addSummaryRow = (label, value) => {
        const row = document.createElement('tr');
        const l = document.createElement('th');
        l.textContent = label;
        const v = document.createElement('td');
        v.textContent = value;
        row.appendChild(l);
        row.appendChild(v);
        summaryTable.appendChild(row);
      };

      addSummaryRow('Status', raw.status || 'unknown');
      addSummaryRow('Sender rate (Mbps)', sumSent.bits_per_second ? formatMetric(sumSent.bits_per_second / 1e6) : 'N/A');
      addSummaryRow('Receiver rate (Mbps)', sumReceived.bits_per_second ? formatMetric(sumReceived.bits_per_second / 1e6) : 'N/A');
      addSummaryRow('Sender congestion', end.sender_tcp_congestion || 'N/A');
      addSummaryRow('Receiver congestion', end.receiver_tcp_congestion || 'N/A');
      wrap.appendChild(summaryTable);

      const intervals = result.intervals || [];
      if (!intervals.length) {
        const fallback = document.createElement('pre');
        fallback.textContent = JSON.stringify(result, null, 2);
        wrap.appendChild(fallback);
        return wrap;
      }

      const intervalTable = document.createElement('table');
      intervalTable.className = 'raw-table';
      const headerRow = document.createElement('tr');
      ['Interval (s)', 'Rate (Mbps)', 'Retrans', 'RTT (ms)', 'CWND', 'Window'].forEach((label) => {
        const th = document.createElement('th');
        th.textContent = label;
        headerRow.appendChild(th);
      });
      intervalTable.appendChild(headerRow);

      intervals.forEach((interval, idx) => {
        const stream = (interval.streams && interval.streams[0]) || interval.sum || {};
        const start = stream.start ?? 0;
        const endTime = stream.end ?? (stream.seconds ? start + stream.seconds : start);
        const rate = stream.bits_per_second ? `${formatMetric(stream.bits_per_second / 1e6)} Mbps` : 'N/A';
        let rtt = stream.rtt ?? stream.mean_rtt;
        if (rtt && rtt > 1000) rtt = rtt / 1000;

        const cells = [
          `${formatMetric(start, 3)} - ${formatMetric(endTime, 3)}`,
          rate,
          stream.retransmits ?? 'N/A',
          rtt ? `${formatMetric(rtt)}` : 'N/A',
          stream.snd_cwnd ? `${stream.snd_cwnd}` : 'N/A',
          stream.snd_wnd ? `${stream.snd_wnd}` : 'N/A',
        ];

        const row = document.createElement('tr');
        cells.forEach((value) => {
          const td = document.createElement('td');
          td.textContent = value;
          row.appendChild(td);
        });
        intervalTable.appendChild(row);
      });

      wrap.appendChild(intervalTable);
      return wrap;
    }


    async function deleteTestResult(testId) {
      clearAlert(testAlert);
      const res = await fetch(`/tests/${testId}`, { method: 'DELETE' });
      if (!res.ok) {
        setAlert(testAlert, 'Failed to delete test result.');
        return;
      }
      await refreshTests();
    }


    async function clearAllTests() {
      clearAlert(testAlert);
      const res = await fetch('/tests', { method: 'DELETE' });
      if (!res.ok) {
        setAlert(testAlert, 'Failed to delete all tests.');
        return;
      }
      await refreshTests();
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
        const metrics = summarizeTestMetrics(test.raw_result || {});
        const latencyValue = metrics.latencyMs !== undefined && metrics.latencyMs !== null ? metrics.latencyMs : null;
        const path = `${test.src_node_id} → ${test.dst_node_id}`;

        const block = document.createElement('div');
        block.className = 'test-block';

        const header = document.createElement('div');
        header.className = 'flex-between';

        const summary = document.createElement('div');
        summary.innerHTML = `<strong>#${test.id} ${path}</strong> · ${test.protocol.toUpperCase()} · port ${test.params.port} · duration ${test.params.duration}s<br/>` +
          `<span class=\"muted-sm\">Rate: ${metrics.bitsPerSecond ? formatMetric(metrics.bitsPerSecond / 1e6, 2) + ' Mbps' : 'N/A'} | Latency: ${latencyValue !== null ? formatMetric(latencyValue) + ' ms' : 'N/A'} | Loss: ${metrics.lostPercent !== undefined && metrics.lostPercent !== null ? formatMetric(metrics.lostPercent) + '%' : 'N/A'}</span>`;
        header.appendChild(summary);

        const actions = document.createElement('div');
        actions.className = 'inline';
        actions.style.gap = '8px';
        const deleteBtn = document.createElement('button');
        deleteBtn.textContent = 'Delete';
        deleteBtn.style.width = 'auto';
        deleteBtn.className = 'pill danger';
        deleteBtn.onclick = () => deleteTestResult(test.id);
        actions.appendChild(deleteBtn);
        header.appendChild(actions);

        block.appendChild(header);

        const rawTable = renderRawResult(test.raw_result);
        rawTable.style.marginTop = '8px';
        block.appendChild(rawTable);

        testsList.appendChild(block);
      });
    }


    async function deleteSchedule(scheduleId) {
      const res = await fetch(`/schedules/${scheduleId}`, { method: 'DELETE' });
      if (!res.ok) {
        setAlert(testAlert, 'Failed to delete schedule.');
        return;
      }
      await refreshSchedules();
    }


    async function refreshSchedules() {
      const res = await fetch('/schedules');
      const schedules = await res.json();
      if (!schedules.length) {
        schedulesList.textContent = 'No schedules yet.';
        return;
      }
      schedulesList.innerHTML = '';
      schedules.forEach((schedule) => {
        const row = document.createElement('div');
        row.className = 'node-row';
        const info = document.createElement('div');
        const intervalMinutes = Math.round(schedule.interval_seconds / 60);
        info.innerHTML = `<strong>${schedule.name}</strong> · ${schedule.protocol.toUpperCase()} every ${intervalMinutes}m<br/>` +
          `<span class="muted-sm">${schedule.src_node_id} → ${schedule.dst_node_id}, duration ${schedule.duration}s, parallel ${schedule.parallel}, port ${schedule.port}${schedule.notes ? ' · ' + schedule.notes : ''}</span>`;
        row.appendChild(info);

        const actions = document.createElement('div');
        actions.className = 'inline';
        const delBtn = document.createElement('button');
        delBtn.textContent = 'Delete';
        delBtn.style.width = 'auto';
        delBtn.style.background = '#ef4444';
        delBtn.onclick = () => deleteSchedule(schedule.id);
        actions.appendChild(delBtn);
        row.appendChild(actions);

        schedulesList.appendChild(row);
      });
    }

    async function saveNode() {
      clearAlert(addNodeAlert);
      const payload = {
        name: nodeName.value,
        ip: nodeIp.value,
        agent_port: Number(nodePort.value || 8000),
        iperf_port: Number(nodeIperf.value || 5201),
        description: nodeDesc.value
      };

      const method = editingNodeId ? 'PUT' : 'POST';
      const url = editingNodeId ? `/nodes/${editingNodeId}` : '/nodes';

      const res = await fetch(url, {
        method,
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });

      if (!res.ok) {
        const msg = editingNodeId ? 'Failed to update node. Check the fields and try again.' : 'Failed to save node. Check the fields and try again.';
        setAlert(addNodeAlert, msg);
        return;
      }

      resetNodeForm();
      await refreshNodes();
      clearAlert(addNodeAlert);
    }

    async function runTest() {
      clearAlert(testAlert);
      const selectedDst = nodeCache.find((n) => n.id === Number(dstSelect.value));
      const payload = {
        src_node_id: Number(srcSelect.value),
        dst_node_id: Number(dstSelect.value),
        protocol: document.getElementById('protocol').value,
        duration: Number(document.getElementById('duration').value),
        parallel: Number(document.getElementById('parallel').value),
        port: Number(testPortInput.value || (selectedDst ? (selectedDst.detected_iperf_port || selectedDst.iperf_port) : 5201))
      };

      const res = await fetch('/tests', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });

      if (!res.ok) {
        const details = await res.text();
        const message = details ? `Failed to start test. ${details}` : 'Failed to start test. Ensure nodes exist and parameters are valid.';
        setAlert(testAlert, message);
        return;
      }

      await refreshTests();
      clearAlert(testAlert);
    }

    async function saveSchedule() {
      clearAlert(testAlert);
      const payload = {
        name: scheduleName.value,
        src_node_id: Number(scheduleSrcSelect.value),
        dst_node_id: Number(scheduleDstSelect.value),
        protocol: scheduleProtocol.value,
        duration: Number(scheduleDuration.value || 10),
        parallel: Number(scheduleParallel.value || 1),
        port: Number(schedulePort.value || 5201),
        interval_seconds: Number(scheduleInterval.value || 60) * 60,
        notes: scheduleNotes.value
      };

      const res = await fetch('/schedules', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });

      if (!res.ok) {
        setAlert(testAlert, 'Failed to save schedule. Check the fields and try again.');
        return;
      }

      scheduleName.value = '';
      scheduleNotes.value = '';
      await refreshSchedules();
      clearAlert(testAlert);
    }

      document.getElementById('login-btn').addEventListener('click', login);
      document.getElementById('logout-btn').addEventListener('click', logout);
      document.getElementById('save-node').addEventListener('click', saveNode);
      document.getElementById('run-test').addEventListener('click', runTest);
      saveScheduleBtn.addEventListener('click', saveSchedule);
      document.getElementById('refresh-nodes').addEventListener('click', refreshNodes);
      document.getElementById('refresh-tests').addEventListener('click', refreshTests);
      document.getElementById('refresh-schedules').addEventListener('click', refreshSchedules);
      deleteAllTestsBtn.addEventListener('click', clearAllTests);
      dstSelect.addEventListener('change', syncTestPort);
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
    checked_at = int(datetime.now(timezone.utc).timestamp())
    try:
        async with httpx.AsyncClient(timeout=settings.request_timeout) as client:
            response = await client.get(url)
            if response.status_code == 200:
                data = response.json()
                detected_port = data.get("port")
                return NodeWithStatus(
                    id=node.id,
                    name=node.name,
                    ip=node.ip,
                    agent_port=node.agent_port,
                    description=node.description,
                    iperf_port=node.iperf_port,
                    status="online",
                    server_running=bool(data.get("server_running")),
                    health_timestamp=data.get("timestamp"),
                    checked_at=checked_at,
                    detected_iperf_port=int(detected_port) if detected_port else None,
                )
    except Exception:
        pass

    return NodeWithStatus(
        id=node.id,
        name=node.name,
        ip=node.ip,
        agent_port=node.agent_port,
        iperf_port=node.iperf_port,
        description=node.description,
        status="offline",
        server_running=None,
        health_timestamp=None,
        checked_at=checked_at,
        detected_iperf_port=None,
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
        iperf_port=node.iperf_port,
        description=node.description,
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    _persist_state(db)
    return obj


@app.get("/nodes", response_model=List[NodeRead])
def list_nodes(db: Session = Depends(get_db)):
    nodes = db.scalars(select(Node)).all()
    return nodes


@app.put("/nodes/{node_id}", response_model=NodeRead)
def update_node(node_id: int, payload: NodeUpdate, db: Session = Depends(get_db)):
    node = db.get(Node, node_id)
    if not node:
        raise HTTPException(status_code=404, detail="node not found")

    updates = payload.model_dump(exclude_unset=True)
    for key, value in updates.items():
        setattr(node, key, value)

    db.commit()
    db.refresh(node)
    _persist_state(db)
    return node


@app.delete("/nodes/{node_id}")
def delete_node(node_id: int, db: Session = Depends(get_db)):
    node = db.get(Node, node_id)
    if not node:
        raise HTTPException(status_code=404, detail="node not found")

    related_tests = db.scalars(
        select(TestResult).where(
            or_(
                TestResult.src_node_id == node_id,
                TestResult.dst_node_id == node_id,
            )
        )
    ).all()
    for test in related_tests:
        db.delete(test)

    db.delete(node)
    db.commit()
    _persist_state(db)
    return {"status": "deleted"}


@app.get("/nodes/status", response_model=List[NodeWithStatus])
async def nodes_with_status(db: Session = Depends(get_db)):
    return await health_monitor.get_statuses()


@app.post("/tests", response_model=TestRead)
async def create_test(test: TestCreate, db: Session = Depends(get_db)):
    src = db.get(Node, test.src_node_id)
    dst = db.get(Node, test.dst_node_id)
    if not src or not dst:
        raise HTTPException(status_code=404, detail="node not found")

    src_status = await health_monitor.check_node(src)
    if src_status.status != "online":
        raise HTTPException(status_code=503, detail="source node is offline or unreachable")

    dst_status = await health_monitor.check_node(dst)
    if dst_status.status != "online":
        raise HTTPException(status_code=503, detail="destination node is offline or unreachable")
    if dst_status.server_running is False:
        raise HTTPException(status_code=503, detail="destination iperf server is not running")

    agent_url = f"http://{src.ip}:{src.agent_port}/run_test"
    payload = {
        "target": dst.ip,
        "port": test.port,
        "duration": test.duration,
        "protocol": test.protocol,
        "parallel": test.parallel,
    }

    try:
        async with httpx.AsyncClient(timeout=test.duration + settings.request_timeout) as client:
            response = await client.post(agent_url, json=payload)
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"failed to reach source agent: {exc}")

    if response.status_code != 200:
        detail = response.text
        try:
            parsed = response.json()
            if isinstance(parsed, dict) and parsed.get("error"):
                detail = parsed.get("error")
        except Exception:
            pass
        raise HTTPException(status_code=502, detail=f"agent returned {response.status_code}: {detail}")

    try:
        raw_data = response.json()
    except ValueError:
        raise HTTPException(
            status_code=502, detail="agent response is not valid JSON"
        )

    if not isinstance(raw_data, dict):
        detail = response.text[:200]
        raise HTTPException(
            status_code=502,
            detail=f"agent response JSON has unexpected format: {detail}",
        )

    try:
        summary = _summarize_metrics(raw_data)
    except Exception as exc:
        snippet = response.text[:200]
        logger.exception("Failed to summarize agent response")
        raise HTTPException(
            status_code=502,
            detail=f"agent response JSON could not be processed: {snippet}",
        ) from exc
    obj = TestResult(
        src_node_id=src.id,
        dst_node_id=dst.id,
        protocol=test.protocol,
        params=payload,
        raw_result=raw_data,
        summary=summary,
        created_at=datetime.now(timezone.utc),
    )
    db.add(obj)
    db.commit()
    db.refresh(obj)
    _persist_state(db)
    return obj


@app.get("/tests", response_model=List[TestRead])
def list_tests(db: Session = Depends(get_db)):
    results = db.scalars(select(TestResult)).all()
    return results


@app.delete("/tests")
def delete_all_tests(db: Session = Depends(get_db)):
    results = db.scalars(select(TestResult)).all()
    for test in results:
        db.delete(test)
    db.commit()
    _persist_state(db)
    return {"status": "deleted", "count": len(results)}


@app.delete("/tests/{test_id}")
def delete_test(test_id: int, db: Session = Depends(get_db)):
    test = db.get(TestResult, test_id)
    if not test:
        raise HTTPException(status_code=404, detail="test not found")

    db.delete(test)
    db.commit()
    _persist_state(db)
    return {"status": "deleted"}


def _get_schedule_or_404(schedule_id: int, db: Session) -> TestSchedule:
    schedule = db.get(TestSchedule, schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="schedule not found")
    return schedule


@app.post("/schedules", response_model=TestScheduleRead)
def create_schedule(schedule: TestScheduleCreate, db: Session = Depends(get_db)):
    obj = TestSchedule(**schedule.model_dump())
    db.add(obj)
    db.commit()
    db.refresh(obj)
    _persist_state(db)
    return obj


@app.get("/schedules", response_model=List[TestScheduleRead])
def list_schedules(db: Session = Depends(get_db)):
    return db.scalars(select(TestSchedule)).all()


@app.put("/schedules/{schedule_id}", response_model=TestScheduleRead)
def update_schedule(schedule_id: int, payload: TestScheduleUpdate, db: Session = Depends(get_db)):
    schedule = _get_schedule_or_404(schedule_id, db)
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(schedule, key, value)
    db.commit()
    db.refresh(schedule)
    _persist_state(db)
    return schedule


@app.delete("/schedules/{schedule_id}")
def delete_schedule(schedule_id: int, db: Session = Depends(get_db)):
    schedule = _get_schedule_or_404(schedule_id, db)
    db.delete(schedule)
    db.commit()
    _persist_state(db)
    return {"status": "deleted"}


def _normalized_agent_payload(data: dict) -> dict:
    default_image = AgentConfigRead.model_fields["image"].default
    if not data.get("image") or data.get("image") == default_image:
        data["image"] = settings.agent_image
    if not data.get("container_name"):
        data["container_name"] = AgentConfigRead.model_fields["container_name"].default
    return data


def _get_agent_or_404(name: str) -> AgentConfigRead:
    config = agent_store.get_config(name)
    if not config:
        raise HTTPException(status_code=404, detail="agent config not found")
    return config


@app.get("/agent-configs", response_model=List[AgentConfigRead])
def list_agent_configs() -> List[AgentConfigRead]:
    return agent_store.list_configs()


@app.post("/agent-configs", response_model=AgentConfigRead)
def create_agent_config(config: AgentConfigCreate) -> AgentConfigRead:
    try:
        return agent_store.upsert(AgentConfigCreate(**_normalized_agent_payload(config.model_dump())))
    except ValueError as exc:  # pragma: no cover - defensive
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.put("/agent-configs/{name}", response_model=AgentConfigRead)
def update_agent_config(name: str, config: AgentConfigUpdate) -> AgentConfigRead:
    payload = config.model_copy(update={"name": name}, deep=True)
    try:
        return agent_store.upsert(AgentConfigUpdate(**_normalized_agent_payload(payload.model_dump(exclude_none=True))))
    except ValueError as exc:  # pragma: no cover - defensive
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/agent-configs/{name}")
def delete_agent_config(name: str) -> dict:
    try:
        agent_store.delete(name)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "deleted"}


@app.post("/agent-configs/{name}/redeploy", response_model=AgentActionResult)
async def redeploy_agent_container(name: str) -> AgentActionResult:
    config = _get_agent_or_404(name)
    loop = asyncio.get_running_loop()
    try:
        message = await loop.run_in_executor(None, lambda: redeploy_agent(config))
    except RemoteCommandError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return AgentActionResult(status="ok", message=message or "redeployed")


@app.post("/agent-configs/{name}/remove-container", response_model=AgentActionResult)
async def remove_agent_container_remote(name: str) -> AgentActionResult:
    config = _get_agent_or_404(name)
    loop = asyncio.get_running_loop()
    try:
        message = await loop.run_in_executor(None, lambda: remove_agent_container(config))
    except RemoteCommandError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return AgentActionResult(status="ok", message=message or "removed")


@app.get("/agent-configs/{name}/logs", response_model=AgentActionResult)
async def get_agent_logs(name: str, lines: int = 200) -> AgentActionResult:
    config = _get_agent_or_404(name)
    loop = asyncio.get_running_loop()
    try:
        logs = await loop.run_in_executor(None, lambda: fetch_agent_logs(config, lines))
    except RemoteCommandError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return AgentActionResult(status="ok", message=f"Fetched last {lines} lines", logs=logs)
