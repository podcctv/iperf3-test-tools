import asyncio
import hashlib
import hmac
import json
import logging
from datetime import datetime, timezone
from typing import Dict, List

import httpx
from fastapi import Body, Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse
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


def _hydrate_agent_store() -> None:
    db = SessionLocal()
    try:
        for node in db.scalars(select(Node)).all():
            try:
                agent_store.upsert(_agent_config_from_node(node))
            except Exception:  # pragma: no cover - defensive hydration
                logger.exception("Failed to sync agent config for node %s", node.name)
    finally:
        db.close()


_hydrate_agent_store()


def _persist_state(db: Session) -> None:
    try:
        state_store.persist(db)
    except Exception:
        logger.exception("Failed to persist state to %s", settings.state_file)


def _agent_config_from_node(node: Node) -> AgentConfigCreate:
    return AgentConfigCreate(
        name=node.name,
        host=node.ip,
        agent_port=node.agent_port,
        iperf_port=node.iperf_port,
        description=node.description,
    )


def _sync_agent_config(node: Node, previous_name: str | None = None) -> None:
    """Ensure the agent config inventory mirrors the current node state."""

    agent_store.upsert(_agent_config_from_node(node))

    if previous_name and previous_name != node.name:
        try:
            agent_store.delete(previous_name)
        except KeyError:  # pragma: no cover - defensive cleanup
            pass


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


health_monitor = NodeHealthMonitor(settings.health_check_interval)


@app.on_event("startup")
async def _on_startup() -> None:
    await health_monitor.start()


@app.on_event("shutdown")
async def _on_shutdown() -> None:
    await health_monitor.stop()


def _login_html() -> str:
    return """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>iperf3 主控面板</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@radix-ui/themes@3.1.1/dist/css/themes.css" />
  <script src="https://cdn.tailwindcss.com"></script>
  <script>
    tailwind.config = {
      theme: {
        extend: {
          colors: {
            slate: {
              950: '#020617',
            },
          },
        },
      },
    };
  </script>
  <style>
    body { font-family: 'Inter', system-ui, -apple-system, sans-serif; background: radial-gradient(circle at 20% 20%, rgba(56,189,248,0.06), transparent 35%), radial-gradient(circle at 80% 0%, rgba(16,185,129,0.05), transparent 40%), #020617; color: #e2e8f0; margin: 0; padding: 0; }
    .glass-card { border: 1px solid rgba(148, 163, 184, 0.15); background: rgba(15, 23, 42, 0.8); box-shadow: 0 20px 60px rgba(0, 0, 0, 0.35); backdrop-filter: blur(12px); }
    .panel-card { border: 1px solid rgba(148, 163, 184, 0.18); background: rgba(15, 23, 42, 0.7); box-shadow: 0 12px 35px rgba(0, 0, 0, 0.25); backdrop-filter: blur(10px); }
    .gradient-bar { background: linear-gradient(120deg, #22c55e 0%, #0ea5e9 35%, #a855f7 100%); height: 4px; border-radius: 999px; }
    .hidden { display: none; }
  </style>
</head>
<body>
  <div class="radix-themes min-h-screen" data-theme="dark">
    <div class="relative mx-auto max-w-6xl px-6 py-10 lg:px-10">
      <div class="absolute inset-0 -z-10 overflow-hidden rounded-3xl bg-gradient-to-br from-slate-900/90 via-slate-950 to-slate-950 shadow-[0_30px_120px_rgba(0,0,0,0.55)]"></div>
      <div class="relative z-10 space-y-6">
        <div class="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <p class="text-sm uppercase tracking-[0.25em] text-sky-300/80">iperf3 控制中心</p>
            <h1 class="text-3xl font-bold text-white sm:text-4xl">主控面板</h1>
            <p class="mt-2 text-slate-400 text-sm leading-relaxed max-w-3xl">通过浏览器即可管理节点、发起测试并查看最新结果。</p>
          </div>
          <div class="flex items-center gap-3">
            <span class="inline-flex items-center gap-2 rounded-full bg-emerald-500/10 px-3 py-2 text-xs font-semibold text-emerald-300 ring-1 ring-emerald-500/30">
              <span class="h-2 w-2 rounded-full bg-emerald-400 animate-pulse"></span>
              实时控制
            </span>
          </div>
        </div>

        <div class="glass-card rounded-3xl p-6 ring-1 ring-slate-800/60">
          <div class="gradient-bar mb-6"></div>
          <div id="login-card" class="space-y-6">
            <div class="flex items-center justify-between gap-4">
              <div>
                <h2 class="text-2xl font-semibold text-white">解锁控制台</h2>
                <p class="text-sm text-slate-400">输入共享密码以进入运维面板。</p>
              </div>
              <div class="hidden sm:inline-flex items-center gap-2 rounded-full bg-slate-800/70 px-3 py-2 text-xs font-medium text-slate-200 ring-1 ring-slate-700">
                <span class="h-2 w-2 rounded-full bg-amber-400 animate-ping"></span>
                会话未解锁
              </div>
            </div>
            <div id="login-alert" class="hidden rounded-xl border border-rose-500/40 bg-rose-500/10 px-4 py-3 text-sm text-rose-100"></div>
            <div class="grid gap-4 md:grid-cols-3">
              <div class="md:col-span-2 space-y-3">
                <label class="text-sm font-medium text-slate-200" for="password">控制台密码</label>
                <input id="password" type="password" placeholder="请输入控制台密码"
                  class="w-full rounded-xl border border-slate-800 bg-slate-900/70 px-4 py-3 text-sm text-slate-100 placeholder:text-slate-500 focus:border-sky-500 focus:outline-none focus:ring-2 focus:ring-sky-500/60" />
                <p class="text-xs text-slate-500">默认密码为 <code class="font-semibold text-sky-300">iperf-pass</code>，可通过环境变量覆盖。</p>
              </div>
              <div class="flex items-end">
                <button id="login-btn" class="w-full rounded-xl bg-gradient-to-r from-sky-500 to-emerald-400 px-4 py-3 text-sm font-semibold text-slate-950 shadow-lg shadow-emerald-500/20 transition hover:scale-[1.01] hover:shadow-xl">解锁</button>
              </div>
            </div>
          </div>

          <div id="app-card" class="hidden space-y-8">
            <div class="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
              <div>
                <p class="text-sm uppercase tracking-[0.25em] text-sky-300/80">控制面板</p>
                <h2 class="text-2xl font-semibold text-white">iperf3 主控面板</h2>
                <p class="text-sm text-slate-400" id="auth-hint"></p>
              </div>
              <div class="flex flex-wrap items-center gap-3">
                <button data-refresh-nodes class="rounded-lg border border-slate-700 bg-slate-800/60 px-4 py-2 text-sm font-semibold text-slate-100 shadow-sm transition hover:border-sky-500 hover:text-sky-200">刷新节点</button>
                <a href="/web/schedules" class="rounded-lg border border-emerald-500/40 bg-emerald-500/15 px-4 py-2 text-sm font-semibold text-emerald-100 shadow-sm transition hover:bg-emerald-500/25">定时任务</a>
                <button id="logout-btn" class="rounded-lg border border-rose-500/40 bg-rose-500/15 px-4 py-2 text-sm font-semibold text-rose-100 shadow-sm transition hover:bg-rose-500/25">退出登录</button>
              </div>
            </div>

            <div class="panel-card rounded-2xl p-5 space-y-3">
              <div class="flex flex-wrap items-center justify-between gap-3">
                <div>
                  <h3 class="text-lg font-semibold text-white">代理配置文件</h3>
                  <p class="text-sm text-slate-400">导入或导出 agent_configs.json。</p>
                </div>
                <div class="flex flex-wrap items-center gap-2">
                  <input id="config-file-input" type="file" accept="application/json" class="hidden" />
                  <button id="export-configs" class="rounded-lg border border-slate-700 bg-slate-800/60 px-4 py-2 text-sm font-semibold text-slate-100 shadow-sm transition hover:border-sky-500 hover:text-sky-200">导出</button>
                  <button id="import-configs" class="rounded-lg border border-sky-500/40 bg-sky-500/15 px-4 py-2 text-sm font-semibold text-sky-100 shadow-sm transition hover:bg-sky-500/25">导入</button>
                </div>
              </div>
              <div id="config-alert" class="hidden rounded-xl border border-slate-700 bg-slate-800/60 px-4 py-3 text-sm text-slate-100"></div>
              <p class="text-xs text-slate-500">可在不同实例之间迁移配置，便于备份。</p>
            </div>

            <div class="grid gap-4 lg:grid-cols-2">
              <div class="panel-card rounded-2xl p-5 space-y-4">
                <div class="flex items-center justify-between gap-2">
                  <h3 class="text-lg font-semibold text-white">添加节点</h3>
                  <span class="rounded-full bg-slate-800/70 px-3 py-1 text-xs font-semibold text-slate-300 ring-1 ring-slate-700">Agent 注册表</span>
                </div>
                <div id="add-node-alert" class="hidden rounded-xl border border-rose-500/40 bg-rose-500/10 px-4 py-3 text-sm text-rose-100"></div>
                <div class="grid gap-3 sm:grid-cols-2">
                  <div class="space-y-2">
                    <label class="text-sm font-medium text-slate-200">名称</label>
                    <input id="node-name" placeholder="node-a" class="rounded-xl border border-slate-800 bg-slate-900/60 px-3 py-2 text-sm text-slate-100 placeholder:text-slate-500 focus:border-sky-500 focus:outline-none focus:ring-2 focus:ring-sky-500/60" />
                  </div>
                  <div class="space-y-2">
                    <label class="text-sm font-medium text-slate-200">IP 地址</label>
                    <input id="node-ip" placeholder="10.0.0.11" class="rounded-xl border border-slate-800 bg-slate-900/60 px-3 py-2 text-sm text-slate-100 placeholder:text-slate-500 focus:border-sky-500 focus:outline-none focus:ring-2 focus:ring-sky-500/60" />
                  </div>
                  <div class="space-y-2">
                    <label class="text-sm font-medium text-slate-200">Agent 端口</label>
                    <input id="node-port" type="number" value="8000" class="rounded-xl border border-slate-800 bg-slate-900/60 px-3 py-2 text-sm text-slate-100 placeholder:text-slate-500 focus:border-sky-500 focus:outline-none focus:ring-2 focus:ring-sky-500/60" />
                  </div>
                  <div class="space-y-2">
                    <label class="text-sm font-medium text-slate-200">iperf 端口</label>
                    <input id="node-iperf-port" type="number" value="5201" class="rounded-xl border border-slate-800 bg-slate-900/60 px-3 py-2 text-sm text-slate-100 placeholder:text-slate-500 focus:border-sky-500 focus:outline-none focus:ring-2 focus:ring-sky-500/60" />
                  </div>
                </div>
                <div class="space-y-2">
                  <label class="text-sm font-medium text-slate-200">描述（可选）</label>
                  <textarea id="node-desc" rows="2" class="w-full rounded-xl border border-slate-800 bg-slate-900/60 px-3 py-2 text-sm text-slate-100 placeholder:text-slate-500 focus:border-sky-500 focus:outline-none focus:ring-2 focus:ring-sky-500/60"></textarea>
                </div>
                <button id="save-node" class="w-full rounded-xl bg-gradient-to-r from-emerald-500 to-sky-500 px-4 py-3 text-sm font-semibold text-slate-950 shadow-lg shadow-emerald-500/20 transition hover:scale-[1.01] hover:shadow-xl">保存节点</button>
              </div>

              <div class="panel-card rounded-2xl p-5 space-y-4">
                <div class="flex items-center justify-between gap-2">
                  <h3 class="text-lg font-semibold text-white">发起测试</h3>
                  <span class="rounded-full bg-slate-800/70 px-3 py-1 text-xs font-semibold text-slate-300 ring-1 ring-slate-700">快速启动</span>
                </div>
                <div id="test-alert" class="hidden rounded-xl border border-rose-500/40 bg-rose-500/10 px-4 py-3 text-sm text-rose-100"></div>
                <div class="grid gap-3 sm:grid-cols-2">
                  <div class="space-y-2">
                    <label class="text-sm font-medium text-slate-200">源节点</label>
                    <select id="src-select" class="rounded-xl border border-slate-800 bg-slate-900/60 px-3 py-2 text-sm text-slate-100 focus:border-sky-500 focus:outline-none focus:ring-2 focus:ring-sky-500/60"></select>
                  </div>
                  <div class="space-y-2">
                    <label class="text-sm font-medium text-slate-200">目标节点</label>
                    <select id="dst-select" class="rounded-xl border border-slate-800 bg-slate-900/60 px-3 py-2 text-sm text-slate-100 focus:border-sky-500 focus:outline-none focus:ring-2 focus:ring-sky-500/60"></select>
                  </div>
                  <div class="space-y-2">
                    <label class="text-sm font-medium text-slate-200">协议</label>
                    <select id="protocol" class="rounded-xl border border-slate-800 bg-slate-900/60 px-3 py-2 text-sm text-slate-100 focus:border-sky-500 focus:outline-none focus:ring-2 focus:ring-sky-500/60"><option value="tcp">TCP</option><option value="udp">UDP</option></select>
                  </div>
                  <div class="space-y-2">
                    <label class="text-sm font-medium text-slate-200">时长（秒）</label>
                    <input id="duration" type="number" value="10" class="rounded-xl border border-slate-800 bg-slate-900/60 px-3 py-2 text-sm text-slate-100 placeholder:text-slate-500 focus:border-sky-500 focus:outline-none focus:ring-2 focus:ring-sky-500/60" />
                  </div>
                  <div class="space-y-2">
                    <label class="text-sm font-medium text-slate-200">并行数</label>
                    <input id="parallel" type="number" value="1" class="rounded-xl border border-slate-800 bg-slate-900/60 px-3 py-2 text-sm text-slate-100 placeholder:text-slate-500 focus:border-sky-500 focus:outline-none focus:ring-2 focus:ring-sky-500/60" />
                  </div>
                  <div class="space-y-2">
                    <label class="text-sm font-medium text-slate-200">端口</label>
                    <input id="test-port" type="number" value="5201" class="rounded-xl border border-slate-800 bg-slate-900/60 px-3 py-2 text-sm text-slate-100 placeholder:text-slate-500 focus:border-sky-500 focus:outline-none focus:ring-2 focus:ring-sky-500/60" />
                  </div>
                </div>
                <button id="run-test" class="w-full rounded-xl bg-gradient-to-r from-sky-500 to-indigo-500 px-4 py-3 text-sm font-semibold text-white shadow-lg shadow-sky-500/20 transition hover:scale-[1.01] hover:shadow-xl">开始测试</button>
              </div>
            </div>

            <div class="grid gap-4">
              <div class="panel-card rounded-2xl p-5 space-y-4">
                <div class="flex flex-wrap items-center justify-between gap-3">
                  <div>
                    <h3 class="text-lg font-semibold text-white">节点列表</h3>
                    <p class="text-sm text-slate-400">实时状态与检测到的 iperf 端口。</p>
                  </div>
                  <button data-refresh-nodes class="rounded-lg border border-slate-700 bg-slate-800/60 px-4 py-2 text-sm font-semibold text-slate-100 shadow-sm transition hover:border-sky-500 hover:text-sky-200">刷新</button>
                </div>
                <div id="nodes-list" class="text-sm text-slate-400 space-y-3">暂无节点。</div>
              </div>
            </div>

            <div class="grid gap-4">
              <div class="panel-card rounded-2xl p-5 space-y-4">
                <div class="flex flex-wrap items-center justify-between gap-3">
                  <div>
                    <h3 class="text-lg font-semibold text-white">最近测试</h3>
                    <p class="text-sm text-slate-400">按时间倒序展示，可展开查看原始输出。</p>
                  </div>
                  <div class="flex flex-wrap items-center gap-2">
                    <button id="refresh-tests" class="rounded-lg border border-slate-700 bg-slate-800/60 px-4 py-2 text-sm font-semibold text-slate-100 shadow-sm transition hover:border-sky-500 hover:text-sky-200">刷新</button>
                    <button id="delete-all-tests" class="rounded-lg border border-rose-500/40 bg-rose-500/15 px-4 py-2 text-sm font-semibold text-rose-100 shadow-sm transition hover:bg-rose-500/25">清空记录</button>
                  </div>
                </div>
                <div id="tests-list" class="text-sm text-slate-400 space-y-3">暂无测试记录。</div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>

    <script>
    const loginCard = document.getElementById('login-card');
    const appCard = document.getElementById('app-card');
    const loginAlert = document.getElementById('login-alert');
    const authHint = document.getElementById('auth-hint');
    const configAlert = document.getElementById('config-alert');
    const importConfigsBtn = document.getElementById('import-configs');
    const exportConfigsBtn = document.getElementById('export-configs');
    const configFileInput = document.getElementById('config-file-input');

    const nodeName = document.getElementById('node-name');
    const nodeIp = document.getElementById('node-ip');
    const nodePort = document.getElementById('node-port');
    const nodeIperf = document.getElementById('node-iperf-port');
    const nodeDesc = document.getElementById('node-desc');
    const nodesList = document.getElementById('nodes-list');
    const testsList = document.getElementById('tests-list');
    const saveNodeBtn = document.getElementById('save-node');
    const srcSelect = document.getElementById('src-select');
    const dstSelect = document.getElementById('dst-select');
    const addNodeAlert = document.getElementById('add-node-alert');
    const testAlert = document.getElementById('test-alert');
    const deleteAllTestsBtn = document.getElementById('delete-all-tests');
    const testPortInput = document.getElementById('test-port');
    let nodeCache = [];
    let editingNodeId = null;
    const styles = {
      rowCard: 'rounded-xl border border-slate-800/70 bg-slate-900/60 p-4 shadow-sm shadow-black/30 space-y-3',
      inline: 'flex flex-wrap items-center gap-3',
      badgeOnline: 'inline-flex items-center gap-2 rounded-full bg-emerald-500/10 px-3 py-1 text-xs font-semibold text-emerald-300 ring-1 ring-emerald-500/40',
      badgeOffline: 'inline-flex items-center gap-2 rounded-full bg-rose-500/10 px-3 py-1 text-xs font-semibold text-rose-200 ring-1 ring-rose-500/40',
      pillInfo: 'inline-flex items-center justify-center gap-2 rounded-lg bg-sky-500/15 px-3 py-2 text-xs font-semibold text-sky-100 ring-1 ring-sky-500/40 transition hover:bg-sky-500/25',
      pillDanger: 'inline-flex items-center justify-center gap-2 rounded-lg bg-rose-500/15 px-3 py-2 text-xs font-semibold text-rose-100 ring-1 ring-rose-500/40 transition hover:bg-rose-500/25',
      pillWarn: 'inline-flex items-center justify-center gap-2 rounded-lg bg-amber-500/15 px-3 py-2 text-xs font-semibold text-amber-100 ring-1 ring-amber-500/40 transition hover:bg-amber-500/25',
      pillMuted: 'inline-flex items-center justify-center gap-2 rounded-lg bg-slate-800/70 px-3 py-2 text-xs font-semibold text-slate-200 ring-1 ring-slate-700',
      textMuted: 'text-slate-400 text-sm',
      textMutedSm: 'text-slate-500 text-xs',
      table: 'w-full border-collapse overflow-hidden rounded-xl border border-slate-800/60 bg-slate-900/50 text-sm text-slate-100',
      tableHeader: 'bg-slate-900/70 text-slate-300',
      tableCell: 'border-b border-slate-800 px-3 py-2',
      codeBlock: 'overflow-auto rounded-lg border border-slate-800 bg-slate-950/80 p-3 text-xs text-slate-200 shadow-inner shadow-black/30',
    };

    function show(el) { el.classList.remove('hidden'); }
    function hide(el) { el.classList.add('hidden'); }
    function setAlert(el, message) { el.textContent = message; show(el); }
    function clearAlert(el) { el.textContent = ''; hide(el); }

    async function exportAgentConfigs() {
      clearAlert(configAlert);
      const res = await fetch('/agent-configs/export');
      if (!res.ok) {
        setAlert(configAlert, '导出配置失败。');
        return;
      }

      const blob = await res.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = 'agent_configs.json';
      document.body.appendChild(link);
      link.click();
      document.body.removeChild(link);
      URL.revokeObjectURL(url);
    }

    async function importAgentConfigs(file) {
      clearAlert(configAlert);
      if (!file) return;

      let payload;
      try {
        payload = JSON.parse(await file.text());
      } catch (err) {
        setAlert(configAlert, 'JSON 文件无效。');
        return;
      }

      const res = await fetch('/agent-configs/import', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });

      if (!res.ok) {
        const msg = await res.text();
        setAlert(configAlert, msg || '导入配置失败。');
        return;
      }

      const imported = await res.json();
      setAlert(configAlert, `已导入 ${imported.length} 条代理配置。`);
    }

    function resetNodeForm() {
      nodeName.value = '';
      nodeIp.value = '';
      nodePort.value = 8000;
      nodeIperf.value = 5201;
      nodeDesc.value = '';
      editingNodeId = null;
      saveNodeBtn.textContent = '保存节点';
    }

    async function removeNode(nodeId) {
      clearAlert(addNodeAlert);
      const confirmDelete = confirm('确定删除该节点并清理相关测试记录吗？');
      if (!confirmDelete) return;

      const res = await fetch(`/nodes/${nodeId}`, { method: 'DELETE' });
      if (!res.ok) {
        setAlert(addNodeAlert, '删除节点失败。');
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
        authHint.textContent = '已通过认证，可管理节点与测速任务。';
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
      if (!res.ok) {
        setAlert(loginAlert, '密码错误或未配置。');
        return;
      }
      await checkAuth();
    }

    async function logout() {
      await fetch('/auth/logout', { method: 'POST' });
      await checkAuth();
    }

    function syncTestPort() {
      const dst = nodeCache.find((n) => n.id === Number(dstSelect.value));
      if (dst) {
        const detected = dst.detected_iperf_port || dst.iperf_port;
        testPortInput.value = detected || 5201;
      }
    }

    async function refreshNodes() {
      const res = await fetch('/nodes/status');
      const nodes = await res.json();
      nodeCache = nodes;
      nodesList.innerHTML = '';
      srcSelect.innerHTML = '';
      dstSelect.innerHTML = '';

      if (!nodes.length) {
        nodesList.textContent = '暂无节点。';
        return;
      }

      nodes.forEach((node) => {
        const statusBadge = node.status === 'online'
          ? `<span class="${styles.badgeOnline}"><span class=\"h-2 w-2 rounded-full bg-emerald-400\"></span> 在线</span>`
          : `<span class="${styles.badgeOffline}"><span class=\"h-2 w-2 rounded-full bg-rose-400\"></span> 离线</span>`;

        const ports = node.detected_iperf_port ? `${node.detected_iperf_port}` : `${node.iperf_port}`;

        const item = document.createElement('div');
        item.className = styles.rowCard;
        item.innerHTML = `
          <div class="flex flex-col gap-2 md:flex-row md:items-center md:justify-between">
            <div>
              <div class="flex flex-wrap items-center gap-2">${statusBadge}<span class="text-base font-semibold text-white">${node.name}</span></div>
              <p class="${styles.textMuted}">${node.ip}:${node.agent_port} · iperf ${ports}${node.description ? ' · ' + node.description : ''}</p>
            </div>
            <div class="flex flex-wrap gap-2">
              <button class="${styles.pillInfo}" onclick="editNode(${node.id})">编辑</button>
              <button class="${styles.pillDanger}" onclick="removeNode(${node.id})">删除</button>
            </div>
          </div>
        `;
        nodesList.appendChild(item);

        const optionA = document.createElement('option');
        optionA.value = node.id;
        optionA.textContent = `${node.name} (${node.ip} | iperf ${ports})`;
        srcSelect.appendChild(optionA);

        const optionB = optionA.cloneNode(true);
        dstSelect.appendChild(optionB);
      });

      syncTestPort();
    }

    function editNode(nodeId) {
      const node = nodeCache.find((n) => n.id === nodeId);
      if (!node) return;
      nodeName.value = node.name;
      nodeIp.value = node.ip;
      nodePort.value = node.agent_port;
      nodeIperf.value = node.iperf_port;
      nodeDesc.value = node.description || '';
      editingNodeId = nodeId;
      saveNodeBtn.textContent = '保存修改';
    }

    async function saveNodeInline(nodeId, payload) {
      const res = await fetch(`/nodes/${nodeId}`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      if (!res.ok) {
        alert('保存失败，请检查字段。');
        return;
      }
      await refreshNodes();
    }

    async function refreshTests() {
      const res = await fetch('/tests');
      const tests = await res.json();
      if (!tests.length) {
        testsList.textContent = '暂无测试记录。';
        return;
      }
      testsList.innerHTML = '';

      const table = document.createElement('table');
      table.className = styles.table;
      const headerRow = document.createElement('tr');
      headerRow.className = styles.tableHeader;
      ['序号', '节点信息', '类型', 'TLS RTT (ms)', 'HTTP 延迟 (ms)', '平均速度 (Mbps)', '峰值速度 (Mbps)', '每秒连接', 'UDP 类型', '操作'].forEach((label) => {
        const th = document.createElement('th');
        th.textContent = label;
        th.className = styles.tableCell + ' font-semibold';
        headerRow.appendChild(th);
      });
      table.appendChild(headerRow);

      const detailsContainer = document.createElement('div');
      detailsContainer.className = 'flex flex-col gap-3 mt-3';
      const detailBlocks = new Map();

      const toggleDetail = (testId, btn) => {
        const block = detailBlocks.get(testId);
        if (!block) return;
        const isHidden = block.classList.contains('hidden');
        if (isHidden) {
          block.classList.remove('hidden');
          btn.textContent = '收起';
        } else {
          block.classList.add('hidden');
          btn.textContent = '详情';
        }
      };

      tests.slice().reverse().forEach((test) => {
        const metrics = summarizeTestMetrics(test.raw_result || {});
        const rateSummary = summarizeRateTable(test.raw_result || {});
        const latencyValue = metrics.latencyMs !== undefined && metrics.latencyMs !== null ? metrics.latencyMs : null;
        const jitterValue = metrics.jitterMs !== undefined && metrics.jitterMs !== null ? metrics.jitterMs : null;
        const pathLabel = `${formatNodeLabel(test.src_node_id)} → ${formatNodeLabel(test.dst_node_id)}`;

        const row = document.createElement('tr');
        const cells = [
          `#${test.id}`,
          pathLabel,
          test.protocol.toUpperCase(),
          latencyValue !== null ? formatMetric(latencyValue) : 'N/A',
          jitterValue !== null ? formatMetric(jitterValue) : 'N/A',
          rateSummary.receiverRateMbps,
          rateSummary.senderRateMbps,
          test.params.parallel ?? 'N/A',
          test.protocol.toLowerCase() === 'udp' ? 'UDP' : 'TCP',
        ];
        cells.forEach((value) => {
          const td = document.createElement('td');
          td.textContent = value;
          td.className = styles.tableCell;
          row.appendChild(td);
        });

        const actionTd = document.createElement('td');
        actionTd.className = styles.tableCell;
        const detailsBtn = document.createElement('button');
        detailsBtn.textContent = '详情';
        detailsBtn.className = styles.pillInfo;
        detailsBtn.onclick = () => toggleDetail(test.id, detailsBtn);
        const deleteBtn = document.createElement('button');
        deleteBtn.textContent = '删除';
        deleteBtn.className = styles.pillDanger + ' ml-2';
        deleteBtn.onclick = () => deleteTestResult(test.id);
        actionTd.appendChild(detailsBtn);
        actionTd.appendChild(deleteBtn);
        row.appendChild(actionTd);
        table.appendChild(row);

        const block = buildTestDetailsBlock(test, metrics, latencyValue, pathLabel);
        detailBlocks.set(test.id, block);
        detailsContainer.appendChild(block);
      });

      testsList.appendChild(table);
      testsList.appendChild(detailsContainer);
    }

    async function deleteTestResult(testId) {
      clearAlert(testAlert);
      const res = await fetch(`/tests/${testId}`, { method: 'DELETE' });
      if (!res.ok) {
        setAlert(testAlert, '删除记录失败。');
        return;
      }
      await refreshTests();
    }

    async function clearAllTests() {
      clearAlert(testAlert);
      const res = await fetch('/tests', { method: 'DELETE' });
      if (!res.ok) {
        setAlert(testAlert, '清空失败。');
        return;
      }
      await refreshTests();
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
        const msg = editingNodeId ? '更新节点失败，请检查字段。' : '保存节点失败，请检查字段。';
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
        const message = details ? `启动测试失败：${details}` : '启动测试失败，请确认节点存在且参数有效。';
        setAlert(testAlert, message);
        return;
      }

      await refreshTests();
      clearAlert(testAlert);
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
        latencyMs = latencyMs / 1000;
      }

      return { bitsPerSecond, jitterMs, lostPercent, latencyMs };
    }

    function summarizeRateTable(raw) {
      const result = raw && raw.iperf_result ? raw.iperf_result : raw;
      const end = (result && result.end) || {};
      const sumSent = end.sum_sent || end.sum || {};
      const sumReceived = end.sum_received || end.sum || {};

      return {
        senderRateMbps: sumSent.bits_per_second ? formatMetric(sumSent.bits_per_second / 1e6, 2) : 'N/A',
        receiverRateMbps: sumReceived.bits_per_second ? formatMetric(sumReceived.bits_per_second / 1e6, 2) : 'N/A',
        senderCongestion: end.sender_tcp_congestion || 'N/A',
        receiverCongestion: end.receiver_tcp_congestion || 'N/A',
        status: raw && raw.status ? raw.status : 'unknown',
      };
    }

    function formatMetric(value, decimals = 2) {
      if (value === undefined || value === null || Number.isNaN(value)) return 'N/A';
      return Number(value).toFixed(decimals);
    }

    function formatNodeLabel(nodeId) {
      const node = nodeCache.find((n) => n.id === Number(nodeId));
      if (node && node.name) return node.name;
      return `节点 ${nodeId}`;
    }

    function renderRawResult(raw) {
      const wrap = document.createElement('div');
      wrap.className = 'overflow-auto rounded-xl border border-slate-800/70 bg-slate-950/60 p-3';

      if (!raw) {
        wrap.textContent = '无原始结果。';
        return wrap;
      }

      const result = raw.iperf_result || raw;
      const end = result.end || {};
      const sumSent = end.sum_sent || {};
      const sumReceived = end.sum_received || {};

      const summaryTable = document.createElement('table');
      summaryTable.className = styles.table + ' mb-3';

      const addSummaryRow = (label, value) => {
        const row = document.createElement('tr');
        const l = document.createElement('th');
        l.textContent = label;
        l.className = styles.tableCell + ' font-semibold text-slate-200';
        const v = document.createElement('td');
        v.textContent = value;
        v.className = styles.tableCell + ' text-slate-100';
        row.appendChild(l);
        row.appendChild(v);
        summaryTable.appendChild(row);
      };

      addSummaryRow('状态', raw.status || 'unknown');
      addSummaryRow('发送速率 (Mbps)', sumSent.bits_per_second ? formatMetric(sumSent.bits_per_second / 1e6) : 'N/A');
      addSummaryRow('接收速率 (Mbps)', sumReceived.bits_per_second ? formatMetric(sumReceived.bits_per_second / 1e6) : 'N/A');
      addSummaryRow('发送拥塞控制', end.sender_tcp_congestion || 'N/A');
      addSummaryRow('接收拥塞控制', end.receiver_tcp_congestion || 'N/A');
      wrap.appendChild(summaryTable);

      const intervals = result.intervals || [];
      if (!intervals.length) {
        const fallback = document.createElement('pre');
        fallback.className = styles.codeBlock;
        fallback.textContent = JSON.stringify(result, null, 2);
        wrap.appendChild(fallback);
        return wrap;
      }

      const intervalTable = document.createElement('table');
      intervalTable.className = styles.table;
      const headerRow = document.createElement('tr');
      headerRow.className = styles.tableHeader;
      ['时间区间 (s)', '速率 (Mbps)', '重传', 'RTT (ms)', 'CWND', '窗口'].forEach((label) => {
        const th = document.createElement('th');
        th.textContent = label;
        th.className = styles.tableCell + ' font-semibold';
        headerRow.appendChild(th);
      });
      intervalTable.appendChild(headerRow);

      intervals.forEach((interval) => {
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
          td.className = styles.tableCell;
          row.appendChild(td);
        });
        intervalTable.appendChild(row);
      });

      wrap.appendChild(intervalTable);
      return wrap;
    }

    function buildTestDetailsBlock(test, metrics, latencyValue, pathLabel) {
      const block = document.createElement('div');
      block.className = 'hidden rounded-xl border border-slate-800/60 bg-slate-900/60 p-3 shadow-inner shadow-black/20';
      block.dataset.testId = test.id;

      const header = document.createElement('div');
      header.className = 'flex flex-col gap-3 md:flex-row md:items-center md:justify-between';

      const summary = document.createElement('div');
      summary.innerHTML = `<strong>#${test.id} ${pathLabel}</strong> · ${test.protocol.toUpperCase()} · 端口 ${test.params.port} · 时长 ${test.params.duration}s<br/>` +
        `<span class="${styles.textMutedSm}">速率: ${metrics.bitsPerSecond ? formatMetric(metrics.bitsPerSecond / 1e6, 2) + ' Mbps' : 'N/A'} | 时延: ${latencyValue !== null ? formatMetric(latencyValue) + ' ms' : 'N/A'} | 丢包: ${metrics.lostPercent !== undefined && metrics.lostPercent !== null ? formatMetric(metrics.lostPercent) + '%' : 'N/A'}</span>`;
      header.appendChild(summary);

      const actions = document.createElement('div');
      actions.className = styles.inline;

      const deleteBtn = document.createElement('button');
      deleteBtn.textContent = '删除';
      deleteBtn.className = styles.pillDanger;
      deleteBtn.onclick = () => deleteTestResult(test.id);
      actions.appendChild(deleteBtn);
      header.appendChild(actions);

      block.appendChild(header);

      const rawTable = renderRawResult(test.raw_result);
      rawTable.classList.add('mt-3');
      block.appendChild(rawTable);

      return block;
    }

    document.getElementById('login-btn').addEventListener('click', login);
    document.getElementById('logout-btn').addEventListener('click', logout);
    document.getElementById('run-test').addEventListener('click', runTest);
    saveNodeBtn.addEventListener('click', saveNode);

    importConfigsBtn.addEventListener('click', () => configFileInput.click());
    exportConfigsBtn.addEventListener('click', exportAgentConfigs);
    configFileInput.addEventListener('change', (e) => importAgentConfigs(e.target.files[0]));
    document.getElementById('refresh-tests').addEventListener('click', refreshTests);
    deleteAllTestsBtn.addEventListener('click', clearAllTests);

    document.querySelectorAll('[data-refresh-nodes]').forEach((btn) => btn.addEventListener('click', refreshNodes));
    dstSelect.addEventListener('change', syncTestPort);
    document.getElementById('password').addEventListener('keyup', (e) => { if (e.key === 'Enter') login(); });

    checkAuth();
  </script>

</body>
</html>

    """


def _schedule_html() -> str:
    return """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>定时测试计划</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@radix-ui/themes@3.1.1/dist/css/themes.css" />
  <script src="https://cdn.tailwindcss.com"></script>
  <script>
    tailwind.config = {
      theme: {
        extend: {
          colors: {
            slate: {
              950: '#020617',
            },
          },
        },
      },
    };
  </script>
  <style>
    body { font-family: 'Inter', system-ui, -apple-system, sans-serif; background: radial-gradient(circle at 20% 20%, rgba(56,189,248,0.06), transparent 35%), radial-gradient(circle at 80% 0%, rgba(16,185,129,0.05), transparent 40%), #020617; color: #e2e8f0; margin: 0; padding: 0; }
    .glass-card { border: 1px solid rgba(148, 163, 184, 0.15); background: rgba(15, 23, 42, 0.8); box-shadow: 0 20px 60px rgba(0, 0, 0, 0.35); backdrop-filter: blur(12px); }
    .panel-card { border: 1px solid rgba(148, 163, 184, 0.18); background: rgba(15, 23, 42, 0.7); box-shadow: 0 12px 35px rgba(0, 0, 0, 0.25); backdrop-filter: blur(10px); }
    .gradient-bar { background: linear-gradient(120deg, #22c55e 0%, #0ea5e9 35%, #a855f7 100%); height: 4px; border-radius: 999px; }
    .hidden { display: none; }
  </style>
</head>
<body>
  <div class="radix-themes min-h-screen" data-theme="dark">
    <div class="relative mx-auto max-w-5xl px-6 py-10 lg:px-10">
      <div class="absolute inset-0 -z-10 overflow-hidden rounded-3xl bg-gradient-to-br from-slate-900/90 via-slate-950 to-slate-950 shadow-[0_30px_120px_rgba(0,0,0,0.55)]"></div>
      <div class="relative z-10 space-y-6">
        <div class="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
          <div>
            <p class="text-sm uppercase tracking-[0.25em] text-sky-300/80">iperf3 控制中心</p>
            <h1 class="text-3xl font-bold text-white">定时测试计划</h1>
            <p class="text-sm text-slate-400">集中管理计划任务，循环执行链路测试。</p>
          </div>
          <div class="flex gap-2">
            <a href="/web" class="rounded-lg border border-slate-700 bg-slate-800/60 px-4 py-2 text-sm font-semibold text-slate-100 shadow-sm transition hover:border-sky-500 hover:text-sky-200">返回控制台</a>
            <button id="logout-btn" class="rounded-lg border border-rose-500/40 bg-rose-500/15 px-4 py-2 text-sm font-semibold text-rose-100 shadow-sm transition hover:bg-rose-500/25">退出登录</button>
          </div>
        </div>

        <div class="panel-card rounded-2xl p-5 space-y-4">
          <div class="flex flex-wrap items-center justify-between gap-3">
            <div>
              <h3 class="text-lg font-semibold text-white">新增计划</h3>
              <p class="text-sm text-slate-400">保存后即按设定间隔周期执行。</p>
            </div>
            <div class="flex gap-2">
              <button id="refresh-schedules" class="rounded-lg border border-slate-700 bg-slate-800/60 px-4 py-2 text-sm font-semibold text-slate-100 shadow-sm transition hover:border-sky-500 hover:text-sky-200">刷新列表</button>
            </div>
          </div>
          <div id="schedule-alert" class="hidden rounded-xl border border-rose-500/40 bg-rose-500/10 px-4 py-3 text-sm text-rose-100"></div>
          <div class="grid gap-3 md:grid-cols-2">
            <div class="space-y-2">
              <label class="text-sm font-medium text-slate-200">计划名称</label>
              <input id="schedule-name" placeholder="夜间基线" class="rounded-xl border border-slate-800 bg-slate-900/60 px-3 py-2 text-sm text-slate-100 placeholder:text-slate-500 focus:border-sky-500 focus:outline-none focus:ring-2 focus:ring-sky-500/60" />
            </div>
            <div class="space-y-2">
              <label class="text-sm font-medium text-slate-200">协议</label>
              <select id="schedule-protocol" class="rounded-xl border border-slate-800 bg-slate-900/60 px-3 py-2 text-sm text-slate-100 focus:border-sky-500 focus:outline-none focus:ring-2 focus:ring-sky-500/60"><option value="tcp">TCP</option><option value="udp">UDP</option></select>
            </div>
            <div class="space-y-2">
              <label class="text-sm font-medium text-slate-200">源节点</label>
              <select id="schedule-src" class="rounded-xl border border-slate-800 bg-slate-900/60 px-3 py-2 text-sm text-slate-100 focus:border-sky-500 focus:outline-none focus:ring-2 focus:ring-sky-500/60"></select>
            </div>
            <div class="space-y-2">
              <label class="text-sm font-medium text-slate-200">目标节点</label>
              <select id="schedule-dst" class="rounded-xl border border-slate-800 bg-slate-900/60 px-3 py-2 text-sm text-slate-100 focus:border-sky-500 focus:outline-none focus:ring-2 focus:ring-sky-500/60"></select>
            </div>
            <div class="space-y-2">
              <label class="text-sm font-medium text-slate-200">时长（秒）</label>
              <input id="schedule-duration" type="number" value="10" class="rounded-xl border border-slate-800 bg-slate-900/60 px-3 py-2 text-sm text-slate-100 placeholder:text-slate-500 focus:border-sky-500 focus:outline-none focus:ring-2 focus:ring-sky-500/60" />
            </div>
            <div class="space-y-2">
              <label class="text-sm font-medium text-slate-200">并行数</label>
              <input id="schedule-parallel" type="number" value="1" class="rounded-xl border border-slate-800 bg-slate-900/60 px-3 py-2 text-sm text-slate-100 placeholder:text-slate-500 focus:border-sky-500 focus:outline-none focus:ring-2 focus:ring-sky-500/60" />
            </div>
            <div class="space-y-2">
              <label class="text-sm font-medium text-slate-200">端口</label>
              <input id="schedule-port" type="number" value="5201" class="rounded-xl border border-slate-800 bg-slate-900/60 px-3 py-2 text-sm text-slate-100 placeholder:text-slate-500 focus:border-sky-500 focus:outline-none focus:ring-2 focus:ring-sky-500/60" />
            </div>
            <div class="space-y-2">
              <label class="text-sm font-medium text-slate-200">间隔（分钟）</label>
              <input id="schedule-interval" type="number" value="60" class="rounded-xl border border-slate-800 bg-slate-900/60 px-3 py-2 text-sm text-slate-100 placeholder:text-slate-500 focus:border-sky-500 focus:outline-none focus:ring-2 focus:ring-sky-500/60" />
            </div>
            <div class="space-y-2 md:col-span-2">
              <label class="text-sm font-medium text-slate-200">备注（可选）</label>
              <input id="schedule-notes" placeholder="例如：日报带宽基线" class="rounded-xl border border-slate-800 bg-slate-900/60 px-3 py-2 text-sm text-slate-100 placeholder:text-slate-500 focus:border-sky-500 focus:outline-none focus:ring-2 focus:ring-sky-500/60" />
            </div>
          </div>
          <button id="save-schedule" class="w-full rounded-xl bg-gradient-to-r from-emerald-500 to-sky-500 px-4 py-3 text-sm font-semibold text-slate-950 shadow-lg shadow-emerald-500/20 transition hover:scale-[1.01] hover:shadow-xl">保存计划</button>
        </div>

        <div class="panel-card rounded-2xl p-5 space-y-4">
          <div class="flex flex-wrap items-center justify-between gap-3">
            <div>
              <h3 class="text-lg font-semibold text-white">计划列表</h3>
              <p class="text-sm text-slate-400">展示已保存的定时测试。</p>
            </div>
          </div>
          <div id="schedules-list" class="text-sm text-slate-400 space-y-3">暂无计划。</div>
        </div>
      </div>
    </div>
  </div>

    <script>
    const scheduleAlert = document.getElementById('schedule-alert');
    const schedulesList = document.getElementById('schedules-list');
    const scheduleSrcSelect = document.getElementById('schedule-src');
    const scheduleDstSelect = document.getElementById('schedule-dst');
    const scheduleProtocol = document.getElementById('schedule-protocol');
    const scheduleDuration = document.getElementById('schedule-duration');
    const scheduleParallel = document.getElementById('schedule-parallel');
    const schedulePort = document.getElementById('schedule-port');
    const scheduleInterval = document.getElementById('schedule-interval');
    const scheduleNotes = document.getElementById('schedule-notes');
    const scheduleName = document.getElementById('schedule-name');
    const logoutBtn = document.getElementById('logout-btn');
    const saveScheduleBtn = document.getElementById('save-schedule');
    const refreshSchedulesBtn = document.getElementById('refresh-schedules');
    let nodeCache = [];

    const styles = {
      rowCard: 'rounded-xl border border-slate-800/70 bg-slate-900/60 p-4 shadow-sm shadow-black/30 space-y-3',
      inline: 'flex flex-wrap items-center gap-3',
      pillInfo: 'inline-flex items-center justify-center gap-2 rounded-lg bg-sky-500/15 px-3 py-2 text-xs font-semibold text-sky-100 ring-1 ring-sky-500/40 transition hover:bg-sky-500/25',
      pillDanger: 'inline-flex items-center justify-center gap-2 rounded-lg bg-rose-500/15 px-3 py-2 text-xs font-semibold text-rose-100 ring-1 ring-rose-500/40 transition hover:bg-rose-500/25',
      textMuted: 'text-slate-400 text-sm',
      textMutedSm: 'text-slate-500 text-xs',
    };

    function setAlert(el, message) { el.textContent = message; el.classList.remove('hidden'); }
    function clearAlert(el) { el.textContent = ''; el.classList.add('hidden'); }

    async function logout() {
      await fetch('/auth/logout', { method: 'POST' });
      window.location.href = '/web';
    }

    async function checkAuth() {
      const res = await fetch('/auth/status');
      const data = await res.json();
      if (!data.authenticated) {
        window.location.href = '/web';
      }
    }

    async function refreshNodes() {
      const res = await fetch('/nodes/status');
      const nodes = await res.json();
      nodeCache = nodes;
      scheduleSrcSelect.innerHTML = '';
      scheduleDstSelect.innerHTML = '';

      nodes.forEach((node) => {
        const optionA = document.createElement('option');
        optionA.value = node.id;
        optionA.textContent = `${node.name} (${node.ip} | iperf ${node.detected_iperf_port || node.iperf_port})`;
        scheduleSrcSelect.appendChild(optionA);
        scheduleDstSelect.appendChild(optionA.cloneNode(true));
      });
    }

    async function refreshSchedules() {
      const res = await fetch('/schedules');
      const schedules = await res.json();
      if (!schedules.length) {
        schedulesList.textContent = '暂无计划。';
        return;
      }
      schedulesList.innerHTML = '';
      schedules.forEach((schedule) => {
        const row = document.createElement('div');
        row.className = styles.rowCard;
        const intervalMinutes = Math.round(schedule.interval_seconds / 60);
        row.innerHTML = `
          <div class="flex flex-col gap-2 md:flex-row md:items-center md:justify-between">
            <div>
              <p class="font-semibold text-white">${schedule.name}</p>
              <p class="${styles.textMuted}">${schedule.protocol.toUpperCase()} · ${schedule.src_node_id} → ${schedule.dst_node_id} · 每 ${intervalMinutes} 分钟 · 端口 ${schedule.port} · 时长 ${schedule.duration}s · 并行 ${schedule.parallel}${schedule.notes ? ' · ' + schedule.notes : ''}</p>
            </div>
            <div class="${styles.inline}">
              <button class="${styles.pillDanger}" onclick="deleteSchedule(${schedule.id})">删除</button>
            </div>
          </div>
        `;
        schedulesList.appendChild(row);
      });
    }

    async function saveSchedule() {
      clearAlert(scheduleAlert);
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
        const msg = await res.text();
        setAlert(scheduleAlert, msg || '保存计划失败。');
        return;
      }

      scheduleName.value = '';
      scheduleNotes.value = '';
      await refreshSchedules();
    }

    async function deleteSchedule(scheduleId) {
      const res = await fetch(`/schedules/${scheduleId}`, { method: 'DELETE' });
      if (!res.ok) {
        setAlert(scheduleAlert, '删除计划失败。');
        return;
      }
      await refreshSchedules();
    }

    logoutBtn.addEventListener('click', logout);
    saveScheduleBtn.addEventListener('click', saveSchedule);
    refreshSchedulesBtn.addEventListener('click', refreshSchedules);

    checkAuth();
    refreshNodes();
    refreshSchedules();
  </script>

</body>
</html>

    """


@app.get("/web", response_class=HTMLResponse)
def dashboard() -> HTMLResponse:
    """Serve主控面板页面，认证在前端处理。"""

    return HTMLResponse(content=_login_html())


@app.get("/web/schedules", response_class=HTMLResponse)
def schedules_page() -> HTMLResponse:
    """单独的定时测试管理页面。"""

    return HTMLResponse(content=_schedule_html())


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
    _sync_agent_config(obj)
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

    previous_name = node.name
    updates = payload.model_dump(exclude_unset=True)
    for key, value in updates.items():
        setattr(node, key, value)

    db.commit()
    db.refresh(node)
    _persist_state(db)
    _sync_agent_config(node, previous_name=previous_name)
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
    try:
        agent_store.delete(node.name)
    except KeyError:
        pass
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


@app.get("/agent-configs/export")
def export_agent_configs(db: Session = Depends(get_db)) -> Response:
    """Download the current agent inventory as a JSON file."""

    configs = agent_store.list_configs()
    if not configs:
        nodes = db.scalars(select(Node)).all()
        configs = [
            AgentConfigCreate(**_normalized_agent_payload(_agent_config_from_node(node).model_dump()))
            for node in nodes
        ]

    payload = json.dumps(
        [config.model_dump() for config in configs],
        ensure_ascii=False,
        indent=2,
    )

    return Response(
        content=payload,
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=agent_configs.json"},
    )


@app.post("/agent-configs/import", response_model=List[AgentConfigRead])
def import_agent_configs(configs: List[AgentConfigCreate], db: Session = Depends(get_db)) -> List[AgentConfigRead]:
    seen: set[str] = set()
    normalized: List[AgentConfigCreate] = []
    for config in configs:
        if config.name in seen:
            raise HTTPException(status_code=400, detail="duplicate agent config names not allowed")
        seen.add(config.name)
        normalized.append(
            AgentConfigCreate(**_normalized_agent_payload(config.model_dump()))
        )

    try:
        imported = agent_store.replace_all(normalized)
    except ValueError as exc:  # pragma: no cover - defensive
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    existing_nodes = {node.name: node for node in db.scalars(select(Node)).all()}
    for config in normalized:
        node = existing_nodes.get(config.name)
        if node:
            node.ip = config.host
            node.agent_port = config.agent_port
            node.iperf_port = config.iperf_port
            node.description = config.description
        else:
            db.add(
                Node(
                    name=config.name,
                    ip=config.host,
                    agent_port=config.agent_port,
                    iperf_port=config.iperf_port,
                    description=config.description,
                )
            )

    db.commit()
    _persist_state(db)

    return imported


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
