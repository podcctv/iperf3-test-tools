import asyncio
import hashlib
import asyncio
import hmac
import json
import logging
import socket
import time
import ipaddress
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, List

import httpx
from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy import or_, select, text
from sqlalchemy.orm import Session

from .config import settings
from .constants import DEFAULT_IPERF_PORT
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
    BackboneLatency,
    StreamingServiceStatus,
    StreamingTestResult,
)
from .state_store import StateStore

Base.metadata.create_all(bind=engine)


def _ensure_iperf_port_column() -> None:
    if engine.dialect.name != "sqlite":
        return

    with engine.connect() as connection:
        columns = connection.exec_driver_sql("PRAGMA table_info(nodes)").fetchall()
        if not any(col[1] == "iperf_port" for col in columns):
            connection.exec_driver_sql(
                f"ALTER TABLE nodes ADD COLUMN iperf_port INTEGER DEFAULT {DEFAULT_IPERF_PORT}"
            )
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
_geo_cache: dict[str, tuple[str | None, float]] = {}
GEO_CACHE_TTL_SECONDS = 60 * 60 * 6


async def _resolve_geo_ip(ip_or_host: str) -> tuple[str | None, str]:
    try:
        ipaddress.ip_address(ip_or_host)
        return ip_or_host, ip_or_host
    except ValueError:
        pass

    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(ip_or_host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror:
        return None, ip_or_host

    for info in infos:
        sockaddr = info[4]
        if sockaddr:
            resolved_ip = sockaddr[0]
            try:
                ipaddress.ip_address(resolved_ip)
                return resolved_ip, ip_or_host
            except ValueError:
                continue

    return None, ip_or_host

ZHEJIANG_TARGETS = [
    {
        "key": "zj_cu",
        "name": "浙江联通",
        "host": "zj-cu-v4.ip.zstaticcdn.com",
        "port": 443,
    },
    {
        "key": "zj_cm",
        "name": "浙江移动",
        "host": "zj-cm-v4.ip.zstaticcdn.com",
        "port": 443,
    },
    {
        "key": "zj_ct",
        "name": "浙江电信",
        "host": "zj-ct-v4.ip.zstaticcdn.com",
        "port": 443,
    },
]


def _bootstrap_state() -> None:
    db = SessionLocal()
    try:
        state_store.restore(db)
    finally:
        db.close()


_bootstrap_state()

app = FastAPI(title="iperf3 master api")
agent_store = AgentConfigStore(settings.agent_config_file)


class BackboneLatencyMonitor:
    def __init__(self, targets: List[dict], interval_seconds: int = 60) -> None:
        self.targets = targets
        self.interval_seconds = interval_seconds
        self._cache: Dict[str, BackboneLatency] = {}
        self._task: asyncio.Task | None = None
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
                logger.exception("Failed to refresh backbone latency")
            await asyncio.sleep(self.interval_seconds)

    async def _measure_target(self, target: dict) -> BackboneLatency:
        samples: list[float] = []
        detail: str | None = None
        for _ in range(2):
            start = time.perf_counter()
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(target["host"], int(target["port"])),
                    timeout=5,
                )
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
                samples.append((time.perf_counter() - start) * 1000)
            except Exception as exc:  # pragma: no cover - network dependent
                detail = str(exc)

        latency_ms = sum(samples) / len(samples) if samples else None
        checked_at = int(datetime.now(timezone.utc).timestamp())
        return BackboneLatency(
            key=target["key"],
            name=target["name"],
            host=target["host"],
            port=int(target["port"]),
            latency_ms=round(latency_ms, 2) if latency_ms is not None else None,
            status="ok" if latency_ms is not None else "error",
            detail=None if latency_ms is not None else detail,
            checked_at=checked_at,
        )

    async def refresh(self) -> List[BackboneLatency]:
        async with self._lock:
            results = await asyncio.gather(
                *[self._measure_target(target) for target in self.targets]
            )
            self._cache = {result.key: result for result in results}
            return results

    async def get_statuses(self) -> List[BackboneLatency]:
        if not self._cache:
            return await self.refresh()
        ordered = [self._cache[target["key"]] for target in self.targets if target["key"] in self._cache]
        return ordered


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

    if latency_ms is not None and latency_ms > 1000:
        latency_ms = latency_ms / 1000

    return {
        "bits_per_second": bits_per_second,
        "jitter_ms": jitter_ms,
        "lost_percent": lost_percent,
        "latency_ms": latency_ms,
    }


async def _probe_streaming_unlock(node: Node) -> StreamingTestResult:
    offline_status = StreamingServiceStatus(
        service="节点离线", unlocked=False, detail="agent 未在线或不可达"
    )
    node_status = await health_monitor.check_node(node)
    if node_status.status != "online":
        return StreamingTestResult(
            node_id=node.id,
            node_name=node.name,
            services=[offline_status],
            elapsed_ms=0,
        )

    agent_url = f"http://{node.ip}:{node.agent_port}/streaming_probe"
    try:
        async with httpx.AsyncClient(timeout=settings.request_timeout + 10) as client:
            response = await client.get(agent_url)
    except httpx.RequestError as exc:
        return StreamingTestResult(
            node_id=node.id,
            node_name=node.name,
            services=[
                StreamingServiceStatus(
                    service="连通性检查", unlocked=False, detail=str(exc)
                )
            ],
            elapsed_ms=0,
        )

    if response.status_code != 200:
        return StreamingTestResult(
            node_id=node.id,
            node_name=node.name,
            services=[
                StreamingServiceStatus(
                    service="连通性检查",
                    unlocked=False,
                    status_code=response.status_code,
                    detail=response.text[:200],
                )
            ],
            elapsed_ms=0,
        )

    try:
        payload = response.json()
    except Exception:  # pragma: no cover - defensive parsing
        return StreamingTestResult(
            node_id=node.id,
            node_name=node.name,
            services=[
                StreamingServiceStatus(
                    service="数据解析", unlocked=False, detail="返回数据无法解析"
                )
            ],
            elapsed_ms=0,
        )

    services: list[StreamingServiceStatus] = []
    for item in payload.get("results", []) or []:
        key = item.get("key") or item.get("service")
        services.append(
            StreamingServiceStatus(
                key=key,
                service=item.get("service") or (key or "未知服务"),
                unlocked=bool(item.get("unlocked")),
                status_code=item.get("status_code"),
                detail=item.get("detail"),
            )
        )

    if not services:
        services.append(
            StreamingServiceStatus(
                service="未返回数据", unlocked=False, detail="未收到任何探测结果"
            )
        )

    return StreamingTestResult(
        node_id=node.id,
        node_name=node.name,
        services=services,
        elapsed_ms=payload.get("elapsed_ms"),
    )


def _dashboard_token(password: str) -> str:
    secret = settings.dashboard_secret.encode()
    return hmac.new(secret, password.encode(), hashlib.sha256).hexdigest()


def _load_dashboard_password() -> str:
    path = settings.dashboard_password_file
    if path.exists():
        try:
            content = path.read_text(encoding="utf-8").strip()
            if content:
                return content
        except OSError:
            logger.exception("Failed to read stored dashboard password from %s", path)
    return settings.dashboard_password


def _save_dashboard_password(password: str) -> None:
    path = settings.dashboard_password_file
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(password, encoding="utf-8")
    except OSError:
        logger.exception("Failed to persist dashboard password to %s", path)


_dashboard_password = _load_dashboard_password()


def _current_dashboard_password() -> str:
    return _dashboard_password


def _is_authenticated(request: Request) -> bool:
    stored = request.cookies.get(settings.dashboard_cookie_name)
    return stored == _dashboard_token(_current_dashboard_password())


def _set_auth_cookie(response: Response, password: str) -> None:
    response.set_cookie(
        settings.dashboard_cookie_name,
        _dashboard_token(password),
        httponly=True,
        samesite="lax",
        max_age=60 * 60 * 24,
    )


class NodeHealthMonitor:
    def __init__(self, interval_seconds: int = 30) -> None:
        self.interval_seconds = interval_seconds
        self._task: asyncio.Task | None = None
        self._cache: Dict[int, NodeWithStatus] = {}
        self._lock = asyncio.Lock()

    def invalidate(self, node_id: int | None = None) -> None:
        """Clear cached health state so updates reflect immediately."""

        if node_id is None:
            self._cache = {}
        else:
            self._cache.pop(node_id, None)

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
backbone_monitor = BackboneLatencyMonitor(ZHEJIANG_TARGETS, interval_seconds=60)


@app.on_event("startup")
async def _on_startup() -> None:
    await health_monitor.start()
    await backbone_monitor.start()


@app.on_event("shutdown")
async def _on_shutdown() -> None:
    await health_monitor.stop()
    await backbone_monitor.stop()


@app.get("/geo")
async def geo_lookup(ip: str = Query(..., description="IP 地址")) -> dict:
    code = await lookup_geo_country_code(ip)
    return {"country_code": code}


async def lookup_geo_country_code(ip: str) -> str | None:
    now = time.time()
    cached = _geo_cache.get(ip)
    if cached and now - cached[1] < GEO_CACHE_TTL_SECONDS:
        return cached[0]

    resolved_ip, cache_key = await _resolve_geo_ip(ip)
    if not resolved_ip:
        _geo_cache[cache_key] = (None, now)
        return None

    async def _fetch_from_ipapi(client: httpx.AsyncClient, target_ip: str) -> str | None:
        resp = await client.get(f"https://ipapi.co/{target_ip}/country/")
        if resp.status_code == 200:
            code = resp.text.strip().upper()
            if len(code) == 2:
                return code
        return None

    async def _fetch_from_ip_api(client: httpx.AsyncClient, target_ip: str) -> str | None:
        resp = await client.get(
            f"https://ip-api.com/json/{target_ip}?fields=status,countryCode,message"
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("status") == "success":
                code = data.get("countryCode")
                if isinstance(code, str) and len(code) == 2:
                    return code.upper()
        return None

    try:
        async with httpx.AsyncClient(timeout=5) as client:
            for fetcher in (
                lambda c: _fetch_from_ipapi(c, resolved_ip),
                lambda c: _fetch_from_ip_api(c, resolved_ip),
            ):
                code = await fetcher(client)
                if code:
                    _geo_cache[cache_key] = (code, now)
                    if cache_key != resolved_ip:
                        _geo_cache[resolved_ip] = (code, now)
                    return code
    except Exception:  # pragma: no cover - external dependency
        logger.exception("Failed to lookup geo country code for %s", ip)

    _geo_cache[cache_key] = (None, now)
    return None


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
    body {
      font-family: 'Inter', system-ui, -apple-system, sans-serif;
      background: radial-gradient(circle at 20% 20%, rgba(56, 189, 248, 0.12), transparent 35%),
        radial-gradient(circle at 85% 15%, rgba(139, 92, 246, 0.12), transparent 40%),
        linear-gradient(140deg, rgba(9, 12, 23, 0.98), rgba(5, 10, 24, 0.96));
      background-attachment: fixed;
      color: #e2e8f0;
      margin: 0;
      padding: 0;
    }
    .glass-card {
      border: 1px solid rgba(148, 163, 184, 0.2);
      background: linear-gradient(160deg, rgba(15, 23, 42, 0.92), rgba(22, 33, 61, 0.82));
      box-shadow: 0 26px 80px rgba(0, 0, 0, 0.45), inset 0 1px 0 rgba(255, 255, 255, 0.04);
      backdrop-filter: blur(14px);
    }
    .panel-card {
      border: 1px solid rgba(148, 163, 184, 0.2);
      background: linear-gradient(135deg, rgba(15, 23, 42, 0.92), rgba(12, 20, 38, 0.82));
      box-shadow: 0 20px 60px rgba(0, 0, 0, 0.35), inset 0 1px 0 rgba(255, 255, 255, 0.04);
      backdrop-filter: blur(12px);
    }
    .gradient-bar { background: linear-gradient(120deg, #22c55e 0%, #0ea5e9 35%, #a855f7 100%); height: 4px; border-radius: 999px; }
    .hidden { display: none; }
  </style>
</head>
<body>
  <div class="radix-themes min-h-screen" data-theme="dark">
    <div class="relative flex min-h-screen items-center justify-center px-6 py-12 lg:px-10">
      <div class="absolute inset-0 -z-10 overflow-hidden rounded-[28px] bg-gradient-to-br from-slate-900/90 via-slate-950 to-slate-950 shadow-[0_30px_120px_rgba(0,0,0,0.55)]"></div>
      <div class="relative z-10 w-full max-w-4xl space-y-8">
        <div class="text-center space-y-3">
          <p class="text-sm uppercase tracking-[0.35em] text-sky-300/80">iperf3 控制中心</p>
          <h1 class="text-3xl font-bold text-white sm:text-4xl">主控面板</h1>
          <p class="mx-auto max-w-2xl text-sm leading-relaxed text-slate-400">集中管理节点、发起测试并查看最新结果，稳定的远程运维体验从这里开始。</p>
        </div>

        <div class="glass-card rounded-3xl p-8 ring-1 ring-slate-800/60">
          <div class="gradient-bar mb-6"></div>
          <div id="login-card" class="space-y-6">
            <div class="flex flex-col items-center justify-between gap-4 text-center sm:flex-row sm:text-left">
              <div>
                <h2 class="text-2xl font-semibold text-white">解锁控制台</h2>
                <p class="text-sm text-slate-400">输入共享密码以进入运维面板。</p>
              </div>
              <div class="inline-flex items-center gap-2 rounded-full bg-slate-800/70 px-3 py-2 text-xs font-medium text-slate-200 ring-1 ring-slate-700">
                <span class="h-2 w-2 rounded-full bg-amber-400 animate-ping"></span>
                会话未解锁
              </div>
            </div>
            <div id="login-alert" class="hidden rounded-xl border border-rose-500/40 bg-rose-500/10 px-4 py-3 text-sm text-rose-100"></div>
            <div class="grid gap-4 md:grid-cols-[2fr_1fr] md:items-end">
              <div class="space-y-3">
                <label class="text-sm font-medium text-slate-200" for="password">控制台密码</label>
                <input id="password" type="password" placeholder="请输入控制台密码"
                  class="w-full rounded-xl border border-slate-800 bg-slate-900/70 px-4 py-3 text-sm text-slate-100 placeholder:text-slate-500 focus:border-sky-500 focus:outline-none focus:ring-2 focus:ring-sky-500/60" />
                <p class="text-xs text-slate-500">密码仅由管理员提供，请妥善保管。</p>
              </div>
              <div class="flex justify-center md:justify-end">
                <button id="login-btn" class="w-full rounded-xl bg-gradient-to-r from-sky-500 to-emerald-400 px-5 py-3 text-sm font-semibold text-slate-950 shadow-lg shadow-emerald-500/20 transition hover:scale-[1.01] hover:shadow-xl">解锁</button>
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

            <div class="panel-card rounded-2xl p-5 space-y-4">
              <div class="flex flex-wrap items-center justify-between gap-3">
                <div>
                  <h3 class="text-lg font-semibold text-white">修改控制台密码</h3>
                  <p class="text-sm text-slate-400">更新登录密码并立即应用到当前会话。</p>
                </div>
                <span class="rounded-full bg-slate-800/70 px-3 py-1 text-xs font-semibold text-slate-300 ring-1 ring-slate-700">本地保存</span>
              </div>
              <div id="change-password-alert" class="hidden rounded-xl border border-slate-700 bg-slate-800/60 px-4 py-3 text-sm text-slate-100"></div>
              <div class="grid gap-3 md:grid-cols-3">
                <div class="space-y-2">
                  <label class="text-sm font-medium text-slate-200" for="current-password">当前密码</label>
                  <input id="current-password" type="password" placeholder="请输入当前密码"
                    class="w-full rounded-xl border border-slate-800 bg-slate-900/70 px-4 py-2 text-sm text-slate-100 placeholder:text-slate-500 focus:border-sky-500 focus:outline-none focus:ring-2 focus:ring-sky-500/60" />
                </div>
                <div class="space-y-2">
                  <label class="text-sm font-medium text-slate-200" for="new-password">新密码</label>
                  <input id="new-password" type="password" placeholder="至少 6 位字符"
                    class="w-full rounded-xl border border-slate-800 bg-slate-900/70 px-4 py-2 text-sm text-slate-100 placeholder:text-slate-500 focus:border-emerald-500 focus:outline-none focus:ring-2 focus:ring-emerald-500/60" />
                </div>
                <div class="space-y-2">
                  <label class="text-sm font-medium text-slate-200" for="confirm-password">确认新密码</label>
                  <input id="confirm-password" type="password" placeholder="再次输入新密码"
                    class="w-full rounded-xl border border-slate-800 bg-slate-900/70 px-4 py-2 text-sm text-slate-100 placeholder:text-slate-500 focus:border-emerald-500 focus:outline-none focus:ring-2 focus:ring-emerald-500/60" />
                </div>
              </div>
              <div class="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-end">
                <p class="text-xs text-slate-500 sm:flex-1">新密码将写入本地数据目录并覆盖默认环境变量。</p>
                <button id="change-password-btn" class="w-full sm:w-auto rounded-xl bg-gradient-to-r from-emerald-500 to-sky-500 px-4 py-3 text-sm font-semibold text-slate-950 shadow-lg shadow-emerald-500/20 transition hover:scale-[1.01] hover:shadow-xl">更新密码</button>
              </div>
            </div>

            <div class="space-y-4">
              <div class="panel-card rounded-2xl p-5 space-y-4">
                <div class="flex flex-wrap items-center justify-between gap-3">
                  <div>
                    <h3 class="text-lg font-semibold text-white">节点列表</h3>
                    <p class="text-sm text-slate-400">实时状态与检测到的 iperf 端口。</p>
                  </div>
                  <div class="flex flex-wrap gap-2">
                    <button data-refresh-nodes class="rounded-lg border border-slate-700 bg-slate-800/60 px-4 py-2 text-sm font-semibold text-slate-100 shadow-sm transition hover:border-sky-500 hover:text-sky-200">刷新</button>
                    <button id="open-add-node" class="rounded-lg border border-emerald-500/40 bg-emerald-500/15 px-4 py-2 text-sm font-semibold text-emerald-100 shadow-sm transition hover:bg-emerald-500/25">添加节点</button>
                  </div>
                </div>
                <div id="streaming-progress" class="hidden space-y-2 rounded-xl border border-slate-800 bg-slate-900/50 p-3">
                  <div class="flex items-center justify-between text-xs text-slate-400">
                    <span>流媒体解锁检测</span>
                    <span id="streaming-progress-label" class="font-medium text-slate-200"></span>
                  </div>
                  <div class="h-2 w-full rounded-full bg-slate-800/80">
                    <div id="streaming-progress-bar" class="h-2 w-0 rounded-full bg-gradient-to-r from-emerald-500 to-sky-500 transition-all duration-300"></div>
                  </div>
                </div>
                <div id="nodes-list" class="text-sm text-slate-400 space-y-3">暂无节点。</div>
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
                    <input id="test-port" type="number" value="62001" class="rounded-xl border border-slate-800 bg-slate-900/60 px-3 py-2 text-sm text-slate-100 placeholder:text-slate-500 focus:border-sky-500 focus:outline-none focus:ring-2 focus:ring-sky-500/60" />
                  </div>
                </div>
                <div class="flex items-center justify-between gap-3 rounded-xl border border-slate-800 bg-slate-900/50 px-3 py-2">
                  <label for="reverse" class="flex items-center gap-2 text-sm font-medium text-slate-200">
                    <input id="reverse" type="checkbox" class="h-4 w-4 rounded border-slate-600 bg-slate-900 text-sky-500 focus:ring-sky-500" />
                    反向测试 (-R)
                  </label>
                  <p class="text-xs text-slate-500">在源节点上发起反向流量测试。</p>
                </div>
                <button id="run-test" class="w-full rounded-xl bg-gradient-to-r from-sky-500 to-indigo-500 px-4 py-3 text-sm font-semibold text-white shadow-lg shadow-sky-500/20 transition hover:scale-[1.01] hover:shadow-xl">开始测试</button>
                <div id="test-progress" class="hidden mt-2 space-y-2">
                  <div class="flex items-center justify-between text-xs text-slate-400">
                    <span>链路测试进度</span>
                    <span id="test-progress-label" class="font-medium text-slate-200"></span>
                  </div>
                  <div class="h-2 w-full rounded-full bg-slate-800/80">
                    <div id="test-progress-bar" class="h-2 w-0 rounded-full bg-gradient-to-r from-sky-500 to-indigo-500 transition-all duration-300"></div>
                  </div>
                </div>
              </div>

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

  <div id="add-node-modal" class="fixed inset-0 z-40 hidden items-center justify-center bg-slate-950/80 px-4 py-6 backdrop-blur">
    <div class="relative w-full max-w-xl rounded-3xl border border-slate-800 bg-slate-900/80 p-6 shadow-2xl shadow-black/40">
      <button id="close-add-node" class="absolute right-4 top-4 rounded-full border border-slate-700/80 bg-slate-800/80 p-2 text-slate-300 transition hover:bg-slate-700/80">✕</button>
      <div class="mb-4 flex items-center justify-between gap-2">
        <div>
          <p class="text-xs uppercase tracking-[0.2em] text-sky-300/80">Agent 注册表</p>
          <h3 id="add-node-title" class="text-xl font-semibold text-white">添加节点</h3>
        </div>
        <span class="rounded-full bg-emerald-500/10 px-3 py-1 text-xs font-semibold text-emerald-200 ring-1 ring-emerald-500/40">本地弹窗</span>
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
          <input id="node-iperf-port" type="number" value="62001" class="rounded-xl border border-slate-800 bg-slate-900/60 px-3 py-2 text-sm text-slate-100 placeholder:text-slate-500 focus:border-sky-500 focus:outline-none focus:ring-2 focus:ring-sky-500/60" />
        </div>
      </div>
      <div class="mt-3 space-y-2">
        <label class="text-sm font-medium text-slate-200">描述（可选）</label>
        <textarea id="node-desc" rows="2" class="w-full rounded-xl border border-slate-800 bg-slate-900/60 px-3 py-2 text-sm text-slate-100 placeholder:text-slate-500 focus:border-sky-500 focus:outline-none focus:ring-2 focus:ring-sky-500/60"></textarea>
      </div>
      <div class="mt-4 flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-end">
        <button id="cancel-add-node" class="w-full sm:w-auto rounded-xl border border-slate-700 bg-slate-800/80 px-4 py-2 text-sm font-semibold text-slate-100 transition hover:border-slate-500">取消</button>
        <button id="save-node" class="w-full sm:w-auto rounded-xl bg-gradient-to-r from-emerald-500 to-sky-500 px-4 py-3 text-sm font-semibold text-slate-950 shadow-lg shadow-emerald-500/20 transition hover:scale-[1.01] hover:shadow-xl">保存节点</button>
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
    const changePasswordAlert = document.getElementById('change-password-alert');
    const currentPasswordInput = document.getElementById('current-password');
    const newPasswordInput = document.getElementById('new-password');
    const confirmPasswordInput = document.getElementById('confirm-password');
    const changePasswordBtn = document.getElementById('change-password-btn');

    const nodeName = document.getElementById('node-name');
    const nodeIp = document.getElementById('node-ip');
    const nodePort = document.getElementById('node-port');
    const nodeIperf = document.getElementById('node-iperf-port');
    const nodeDesc = document.getElementById('node-desc');
    const nodesList = document.getElementById('nodes-list');
    const streamingProgress = document.getElementById('streaming-progress');
    const streamingProgressBar = document.getElementById('streaming-progress-bar');
    const streamingProgressLabel = document.getElementById('streaming-progress-label');
    const testsList = document.getElementById('tests-list');
    const saveNodeBtn = document.getElementById('save-node');
    const srcSelect = document.getElementById('src-select');
    const dstSelect = document.getElementById('dst-select');
    const addNodeAlert = document.getElementById('add-node-alert');
    const testAlert = document.getElementById('test-alert');
    const deleteAllTestsBtn = document.getElementById('delete-all-tests');
    const testPortInput = document.getElementById('test-port');
    const testProgress = document.getElementById('test-progress');
    const testProgressBar = document.getElementById('test-progress-bar');
    const testProgressLabel = document.getElementById('test-progress-label');
    const reverseToggle = document.getElementById('reverse');
    const addNodeModal = document.getElementById('add-node-modal');
    const addNodeTitle = document.getElementById('add-node-title');
    const closeAddNodeBtn = document.getElementById('close-add-node');
    const cancelAddNodeBtn = document.getElementById('cancel-add-node');
    const openAddNodeBtn = document.getElementById('open-add-node');
    const DEFAULT_IPERF_PORT = 62001;
    let nodeCache = [];
    let editingNodeId = null;
    const ipPrivacyState = {};
    let nodeRefreshInterval = null;
    let isRefreshingNodes = false;
    const streamingServices = [
      { key: 'youtube', label: 'YouTube Premium', icon: '▶', color: 'text-rose-300', bg: 'border-rose-500/30 bg-rose-500/10' },
      { key: 'prime_video', label: 'Prime Video', icon: '🎬', color: 'text-amber-300', bg: 'border-amber-400/40 bg-amber-500/10' },
      { key: 'netflix', label: 'Netflix', icon: 'N', color: 'text-red-400', bg: 'border-red-500/40 bg-red-500/10' },
      { key: 'disney_plus', label: 'Disney+', icon: '★', color: 'text-sky-300', bg: 'border-sky-500/40 bg-sky-500/10' },
      { key: 'hbo', label: 'HBO', icon: 'H', color: 'text-purple-300', bg: 'border-purple-500/40 bg-purple-500/10' },
      { key: 'openai', label: 'OpenAI', icon: '∞', color: 'text-emerald-300', bg: 'border-emerald-500/40 bg-emerald-500/10' },
      { key: 'gemini', label: 'Gemini', icon: 'G', color: 'text-sky-200', bg: 'border-sky-400/40 bg-sky-400/10' },
    ];
    let streamingStatusCache = {};
    let isStreamingTestRunning = false;
    const styles = {
      rowCard: 'group relative overflow-hidden rounded-2xl border border-slate-800/80 bg-gradient-to-br from-slate-900/80 via-slate-900/60 to-slate-950/80 p-5 shadow-[0_25px_80px_rgba(0,0,0,0.5)] ring-1 ring-white/5 transition hover:border-sky-500/40 hover:shadow-sky-500/10',
      inline: 'flex flex-wrap items-center gap-3',
      badgeOnline: 'inline-flex items-center gap-2 rounded-full bg-gradient-to-r from-emerald-500/20 to-teal-400/15 px-3 py-1 text-xs font-semibold text-emerald-100 ring-1 ring-emerald-400/40 shadow-[0_10px_30px_rgba(16,185,129,0.15)]',
      badgeOffline: 'inline-flex items-center gap-2 rounded-full bg-gradient-to-r from-rose-500/15 to-orange-400/10 px-3 py-1 text-xs font-semibold text-rose-100 ring-1 ring-rose-400/35 shadow-[0_10px_30px_rgba(244,63,94,0.12)]',
      pillInfo: 'inline-flex items-center justify-center gap-2 rounded-lg bg-gradient-to-r from-sky-500/20 via-sky-500/15 to-indigo-500/20 px-3 py-2 text-xs font-semibold text-sky-50 ring-1 ring-sky-400/40 shadow-[0_12px_30px_rgba(14,165,233,0.18)] transition hover:scale-[1.01] hover:ring-sky-300/60',
      pillDanger: 'inline-flex items-center justify-center gap-2 rounded-lg bg-gradient-to-r from-rose-500/20 via-rose-500/15 to-orange-500/20 px-3 py-2 text-xs font-semibold text-rose-50 ring-1 ring-rose-400/40 shadow-[0_12px_30px_rgba(244,63,94,0.18)] transition hover:scale-[1.01] hover:ring-rose-300/60',
      pillWarn: 'inline-flex items-center justify-center gap-2 rounded-lg bg-gradient-to-r from-amber-500/20 via-amber-500/15 to-yellow-500/20 px-3 py-2 text-xs font-semibold text-amber-50 ring-1 ring-amber-400/40 shadow-[0_12px_30px_rgba(251,191,36,0.18)] transition hover:scale-[1.01] hover:ring-amber-300/60',
      pillMuted: 'inline-flex items-center justify-center gap-2 rounded-lg bg-slate-800/70 px-3 py-2 text-xs font-semibold text-slate-200 ring-1 ring-slate-700/70 shadow-inner shadow-black/20',
      iconButton: 'inline-flex items-center justify-center gap-1 rounded-full border border-slate-700/70 bg-slate-800/80 px-2 py-1 text-xs font-semibold text-slate-200 transition hover:border-sky-400 hover:text-sky-100 hover:shadow-[0_10px_30px_rgba(14,165,233,0.15)]',
      textMuted: 'text-slate-300/90 text-sm drop-shadow-sm',
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

    function toggleAddNodeModal(isOpen) {
      if (!addNodeModal) return;
      if (isOpen) {
        addNodeModal.classList.remove('hidden');
        addNodeModal.classList.add('flex');
      } else {
        addNodeModal.classList.add('hidden');
        addNodeModal.classList.remove('flex');
      }
    }

    function openAddNodeModal() {
      toggleAddNodeModal(true);
      addNodeTitle.textContent = editingNodeId ? '编辑节点' : '添加节点';
      nodeName?.focus({ preventScroll: true });
    }

    function closeAddNodeModal() {
      toggleAddNodeModal(false);
    }

    function startProgressBar(container, bar, label, expectedMs, initialText, showCountdown = true) {
      const start = Date.now();
      const target = Math.max(expectedMs || 0, 1200);
      if (initialText) label.textContent = initialText;
      show(container);
      bar.style.width = '6%';
      const timer = setInterval(() => {
        const elapsed = Date.now() - start;
        const pct = Math.min(96, Math.max(10, (elapsed / target) * 100));
        bar.style.width = `${pct}%`;
        const remain = Math.max(target - elapsed, 0);
        if (remain > 0 && showCountdown) {
          label.textContent = `预计 ${Math.ceil(remain / 1000)}s 完成`;
        }
      }, 250);

      return (finalText) => {
        clearInterval(timer);
        bar.style.width = '100%';
        if (finalText) label.textContent = finalText;
        setTimeout(() => hide(container), 1200);
      };
    }

    function normalizeServiceKey(key, label) {
      return (key || label || 'unknown').toLowerCase().replace(/[^a-z0-9+]+/g, '_');
    }

    function cacheStreamingFromNode(node) {
      if (!node?.streaming?.length) return;

      const byService = {};
      node.streaming.forEach((svc) => {
        const normalized = normalizeServiceKey(svc.key, svc.service);
        byService[normalized] = {
          unlocked: svc.unlocked,
          detail: svc.detail,
          tier: svc.tier,
          service: svc.service,
        };
      });
      streamingStatusCache[node.id] = byService;
    }

    const flagCache = {};
    const FLAG_CACHE_TTL = 1000 * 60 * 60 * 6;

    function extractCountryCode(text) {
      const match = (text || '').match(/\b([A-Za-z]{2})\b/);
      return match ? match[1].toUpperCase() : null;
    }

    function countryCodeToFlag(code) {
      if (!code || code.length !== 2) return null;
      const base = 127397;
      const chars = code.toUpperCase().split('');
      return String.fromCodePoint(...chars.map((c) => c.codePointAt(0) + base));
    }

    function isPrivateIp(ip) {
      if (!ip) return false;
      return (
        /^10\./.test(ip) ||
        /^192\.168\./.test(ip) ||
        /^172\.(1[6-9]|2\d|3[0-1])\./.test(ip) ||
        ip === '127.0.0.1'
      );
    }

    function resolveLocalFlag(node) {
      const codeFromDescription = extractCountryCode(node.description);
      const codeFromName = extractCountryCode(node.name);
      const code = codeFromDescription || codeFromName || null;
      const flag = countryCodeToFlag(code) || '🌐';
      return { flag, code };
    }

    function renderFlagHtml(info) {
      const flag = (info?.flag || '🌐').replace(/'/g, '');
      const code = info?.code;
      if (code) {
        const url = `https://flagcdn.com/24x18/${code.toLowerCase()}.png`;
        return `<img src=\"${url}\" alt=\"${code} flag\" class=\"h-4 w-6 rounded-sm border border-white/10 bg-slate-800/50 shadow-sm\" loading=\"lazy\" onerror=\"this.replaceWith(document.createTextNode('${flag}'))\">`;
      }
      return flag;
    }

    function renderFlagSlot(nodeId, info, extraClass = '', title = '') {
      const classAttr = extraClass ? ` ${extraClass}` : '';
      const titleAttr = title ? ` title=\"${title}\"` : '';
      const codeAttr = info?.code ? ` data-flag-code=\"${info.code}\"` : '';
      return `<span class=\"inline-flex items-center${classAttr}\" data-node-flag=\"${nodeId}\"${codeAttr}${titleAttr}>${renderFlagHtml(info)}</span>`;
    }

    function updateFlagElement(el, info) {
      if (!el || !info) return;
      if (info.code) {
        el.dataset.flagCode = info.code;
      }
      el.innerHTML = renderFlagHtml(info);
    }

    async function getNodeFlag(node) {
      const now = Date.now();
      const cacheKey = node.ip || node.id;
      const cached = flagCache[cacheKey];
      if (cached && now - cached.timestamp < FLAG_CACHE_TTL) {
        return cached;
      }

      const fallback = resolveLocalFlag(node);
      if (!node.ip || isPrivateIp(node.ip)) {
        flagCache[cacheKey] = { ...fallback, timestamp: now };
        return flagCache[cacheKey];
      }

      try {
        const res = await fetch(`/geo?ip=${encodeURIComponent(node.ip)}`);
        if (!res.ok) {
          throw new Error('geo lookup failed');
        }
        const data = await res.json();
        const code = (data?.country_code || '').toUpperCase() || fallback.code;
        const flag = countryCodeToFlag(code) || fallback.flag;
        flagCache[cacheKey] = { flag, code, timestamp: now };
        return flagCache[cacheKey];
      } catch (error) {
        console.warn('无法获取 IP 归属地国旗，将使用回退旗帜。', error);
        return fallback;
      }
    }

    function attachFlagUpdater(node, elements) {
      if (!elements) return;
      const targets = elements instanceof NodeList ? Array.from(elements) : [elements];
      if (!targets.length) return;

      const fallback = resolveLocalFlag(node);
      targets.forEach((el) => updateFlagElement(el, fallback));

      getNodeFlag(node).then((info) => {
        if (!info) return;
        targets.forEach((el) => updateFlagElement(el, info));
      });
    }

    function maskIp(ip, shouldMask) {
      if (!shouldMask || !ip) return ip;
      if (ip.includes(':')) {
        const segments = ip.split(':');
        const kept = segments.slice(0, 2).join(':');
        return `${kept}:****:****`;
      }
      const parts = ip.split('.');
      if (parts.length >= 4) {
        return `${parts[0]}.***.***.***`;
      }
      return `${parts[0] || '***'}.***.***.***`;
    }

    function maskPort(port, shouldMask) {
      if (!port) return port;
      return shouldMask ? '****' : `${port}`;
    }

    function renderStreamingBadges(nodeId) {
      const cache = streamingStatusCache[nodeId];
      if (isStreamingTestRunning && (!cache || cache.inProgress)) {
        return '<span class="text-xs text-emerald-300">流媒体测试中...</span>';
      }
      if (!cache) {
        return '<span class="text-xs text-slate-500">未检测</span>';
      }

      if (cache.error) {
        return `<span class=\"text-xs text-amber-300\">${cache.message || '检测异常'}</span>`;
      }

      const mutedStyle = 'text-slate-500 border-slate-800 bg-slate-900/60';
      return streamingServices
        .map((svc) => {
          const status = cache[svc.key];
          const unlocked = status ? status.unlocked : null;
          const tier = status?.tier;
          const detail = status && status.detail ? status.detail.replace(/"/g, "'") : '';

          let badgeColor = unlocked === true ? `${svc.color} ${svc.bg}` : mutedStyle;
          let statusLabel = unlocked === true ? '可解锁' : unlocked === false ? '未解锁' : '未检测';
          let badgeLabel = svc.label;

          if (svc.key === 'netflix' && status) {
            const netflixTier = tier || (unlocked ? 'full' : 'none');
            if (netflixTier === 'full') {
              badgeLabel = 'Netflix（全解锁）';
              statusLabel = '全片库';
              badgeColor = `${svc.color} ${svc.bg}`;
            } else if (netflixTier === 'originals') {
              badgeLabel = 'Netflix（仅自制）';
              statusLabel = '自制片库';
              badgeColor = 'text-amber-200 border-amber-400/40 bg-amber-400/10';
            } else {
              badgeLabel = 'Netflix';
              statusLabel = '未解锁';
              badgeColor = mutedStyle;
            }
          }

          const title = `${badgeLabel}：${statusLabel}${detail ? ' · ' + detail : ''}`;
          return `<span class=\"inline-flex items-center gap-1 rounded-full border px-2 py-1 text-[10px] font-semibold ${badgeColor}\" title=\"${title}\">${svc.icon}<span>${badgeLabel}</span></span>`;
        })
        .join('');
    }

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
      nodeIperf.value = DEFAULT_IPERF_PORT;
      nodeDesc.value = '';
      editingNodeId = null;
      saveNodeBtn.textContent = '保存节点';
      addNodeTitle.textContent = '添加节点';
      hide(addNodeAlert);
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
        closeAddNodeModal();
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

    async function changePassword() {
      clearAlert(changePasswordAlert);

      const payload = {
        current_password: currentPasswordInput.value,
        new_password: newPasswordInput.value,
        confirm_password: confirmPasswordInput.value,
      };

      if (!payload.new_password) {
        setAlert(changePasswordAlert, '请输入新密码。');
        return;
      }

      if (payload.new_password.length < 6) {
        setAlert(changePasswordAlert, '新密码长度需不少于 6 位。');
        return;
      }

      if (payload.new_password !== payload.confirm_password) {
        setAlert(changePasswordAlert, '两次输入的新密码不一致。');
        return;
      }

      const res = await fetch('/auth/change', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });

      if (!res.ok) {
        let feedback = '更新密码失败。';
        try {
          const data = await res.json();
          if (data?.detail === 'invalid_password') feedback = '当前密码不正确或会话已过期。';
          if (data?.detail === 'password_too_short') feedback = '新密码长度不足 6 位。';
          if (data?.detail === 'password_mismatch') feedback = '两次输入的新密码不一致。';
          if (data?.detail === 'empty_password') feedback = '请输入新密码。';
        } catch (err) {
          feedback = feedback + ' ' + (err?.message || '');
        }
        setAlert(changePasswordAlert, feedback.trim());
        return;
      }

      setAlert(changePasswordAlert, '密码已更新，当前会话已使用新密码。');
      currentPasswordInput.value = '';
      newPasswordInput.value = '';
      confirmPasswordInput.value = '';
    }

    function syncTestPort() {
      const dst = nodeCache.find((n) => n.id === Number(dstSelect.value));
      if (dst) {
        const detected = dst.detected_iperf_port || dst.iperf_port;
        testPortInput.value = detected || DEFAULT_IPERF_PORT;
      }
    }

    async function refreshNodes() {
      if (isRefreshingNodes) return;
      isRefreshingNodes = true;
      try {
        const previousSrc = Number(srcSelect.value) || null;
        const previousDst = Number(dstSelect.value) || null;
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
          cacheStreamingFromNode(node);

          const privacyEnabled = !!ipPrivacyState[node.id];
        const flagInfo = resolveLocalFlag(node);
        const locationBadge = renderFlagSlot(node.id, flagInfo, 'text-base drop-shadow-sm', '服务器所在地区');
        const statusBadge = node.status === 'online'
          ? `<span class="${styles.badgeOnline}"><span class=\"h-2 w-2 rounded-full bg-emerald-400\"></span><span>在线</span></span>`
          : `<span class="${styles.badgeOffline}"><span class=\"h-2 w-2 rounded-full bg-rose-400\"></span><span>离线</span></span>`;

          const ports = node.detected_iperf_port ? `${node.detected_iperf_port}` : `${node.iperf_port}`;
          const agentPortDisplay = maskPort(node.agent_port, privacyEnabled);
          const iperfPortDisplay = maskPort(ports, privacyEnabled);
          const streamingBadges = renderStreamingBadges(node.id);
          const backboneBadges = renderBackboneBadges(node.backbone_latency);
          const ipMasked = maskIp(node.ip, privacyEnabled);

        const item = document.createElement('div');
        item.className = styles.rowCard;
        item.innerHTML = `
          <div class="pointer-events-none absolute inset-0 opacity-80">
              <div class="absolute inset-0 bg-gradient-to-br from-emerald-500/8 via-transparent to-sky-500/10"></div>
              <div class="absolute -left-10 top-0 h-32 w-32 rounded-full bg-sky-500/10 blur-3xl"></div>
          </div>
          <div class="relative flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
            <div class="flex-1 space-y-2">
              <div class="flex flex-wrap items-center gap-2">
                ${statusBadge}
                ${locationBadge}
                <span class="text-base font-semibold text-white drop-shadow">${node.name}</span>
                <button type="button" class="${styles.iconButton}" data-privacy-toggle="${node.id}" aria-label="切换 IP 隐藏">
                  <span class="text-base">${ipPrivacyState[node.id] ? '🙈' : '👁️'}</span>
                </button>
              </div>
              ${backboneBadges ? `<div class=\"flex flex-wrap items-center gap-2\">${backboneBadges}</div>` : ''}
              <div class="flex flex-wrap items-center gap-2" data-streaming-badges="${node.id}">${streamingBadges || ''}</div>
              <p class="${styles.textMuted} flex items-center gap-2">
                <span class="font-mono" data-node-ip-display="${node.id}">${ipMasked}</span>
                <span class="text-slate-500" data-node-agent-port="${node.id}">:${agentPortDisplay}</span>
                <span data-node-iperf-display="${node.id}">· iperf ${iperfPortDisplay}${node.description ? ' · ' + node.description : ''}</span>
              </p>
            </div>
            <div class="flex flex-wrap items-center justify-start gap-2 lg:flex-col lg:items-end lg:justify-center lg:min-w-[170px] opacity-100 md:opacity-0 md:pointer-events-none md:transition md:duration-200 md:group-hover:opacity-100 md:group-hover:pointer-events-auto md:focus-within:opacity-100 md:focus-within:pointer-events-auto">
              <button class="${styles.pillInfo}" onclick="runStreamingCheck(${node.id})">流媒体解锁测试</button>
              <button class="${styles.pillInfo}" onclick="editNode(${node.id})">编辑</button>
              <button class="${styles.pillDanger}" onclick="removeNode(${node.id})">删除</button>
            </div>
          </div>
        `;
        nodesList.appendChild(item);

        const toggleBtn = item.querySelector('[data-privacy-toggle]');
        const ipDisplay = item.querySelector(`[data-node-ip-display="${node.id}"]`);
        const flagDisplay = item.querySelectorAll(`[data-node-flag="${node.id}"]`);
        const agentPortSpan = item.querySelector(`[data-node-agent-port="${node.id}"]`);
        const iperfPortSpan = item.querySelector(`[data-node-iperf-display="${node.id}"]`);
        toggleBtn?.addEventListener('click', () => {
          const nextState = !ipPrivacyState[node.id];
          ipPrivacyState[node.id] = nextState;
          if (ipDisplay) {
            ipDisplay.textContent = maskIp(node.ip, nextState);
          }
          if (agentPortSpan) {
            agentPortSpan.textContent = `:${maskPort(node.agent_port, nextState)}`;
          }
          if (iperfPortSpan) {
            iperfPortSpan.textContent = `· iperf ${maskPort(ports, nextState)}${node.description ? ' · ' + node.description : ''}`;
          }
          toggleBtn.innerHTML = `<span class="text-base">${nextState ? '🙈' : '👁️'}</span>`;
          toggleBtn.setAttribute('aria-pressed', String(nextState));
        });

        attachFlagUpdater(node, flagDisplay);

        const optionA = document.createElement('option');
        optionA.value = node.id;
        optionA.textContent = `${node.name} (${maskIp(node.ip, privacyEnabled)} | iperf ${maskPort(ports, privacyEnabled)})`;
        srcSelect.appendChild(optionA);

        const optionB = optionA.cloneNode(true);
        dstSelect.appendChild(optionB);
      });

      const firstNodeId = nodes[0]?.id;
      if (previousSrc && nodes.some((n) => n.id === previousSrc)) {
        srcSelect.value = String(previousSrc);
      } else if (firstNodeId) {
        srcSelect.value = String(firstNodeId);
      }

      if (previousDst && nodes.some((n) => n.id === previousDst)) {
        dstSelect.value = String(previousDst);
      } else if (firstNodeId) {
        dstSelect.value = String(firstNodeId);
      }

      syncTestPort();
      } finally {
        isRefreshingNodes = false;
      }
    }

    async function runStreamingCheck(nodeId) {
      if (isStreamingTestRunning) return;
      const targetNode = nodeCache.find((n) => n.id === nodeId);
      if (!targetNode) {
        setAlert(addNodeAlert, '节点不存在或尚未加载。');
        return;
      }

      isStreamingTestRunning = true;
      streamingProgressLabel.textContent = '流媒体测试中...';
      const expectedMs = Math.max(3500, 2000);
      const stopProgress = startProgressBar(streamingProgress, streamingProgressBar, streamingProgressLabel, expectedMs, '准备发起检测...', false);

      try {
        streamingStatusCache[nodeId] = { inProgress: true };
        updateNodeStreamingBadges(nodeId);
        streamingProgressLabel.textContent = `${targetNode.name} 测试中`;
        try {
          const res = await fetch(`/nodes/${nodeId}/streaming-test`, { method: 'POST' });
          if (!res.ok) {
            streamingStatusCache[nodeId] = streamingStatusCache[nodeId] || {};
            streamingStatusCache[nodeId].error = true;
            streamingStatusCache[nodeId].message = `请求失败 (${res.status})`;
            updateNodeStreamingBadges(nodeId);
          } else {
            const data = await res.json();
            const byService = {};
            (data.services || []).forEach((svc) => {
              const key = normalizeServiceKey(svc.key, svc.service);
              byService[key] = {
                unlocked: !!svc.unlocked,
                detail: svc.detail,
                service: svc.service,
                tier: svc.tier,
              };
            });
            streamingServices.forEach((svc) => {
              if (!byService[svc.key]) {
                byService[svc.key] = { unlocked: false, detail: '未检测' };
              }
            });
            streamingStatusCache[nodeId] = byService;
            updateNodeStreamingBadges(nodeId);
          }
        } catch (err) {
          streamingStatusCache[nodeId] = { error: true, message: err?.message || '请求异常' };
          updateNodeStreamingBadges(nodeId);
        }

        stopProgress('检测完成');
      } finally {
        isStreamingTestRunning = false;
      }
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
      addNodeTitle.textContent = '编辑节点';
      openAddNodeModal();
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

      const detailBlocks = new Map();
      const enrichedTests = tests.slice().reverse().map((test) => {
        const metrics = summarizeTestMetrics(test.raw_result || {});
        const rateSummary = summarizeRateTable(test.raw_result || {});
        const latencyValue = metrics.latencyMs !== undefined && metrics.latencyMs !== null ? metrics.latencyMs : null;
        const jitterValue = metrics.jitterMs !== undefined && metrics.jitterMs !== null ? metrics.jitterMs : null;
        return { test, metrics, rateSummary, latencyValue, jitterValue };
      });

      const maxRate = Math.max(
        1,
        ...enrichedTests.map(({ rateSummary }) => Math.max(rateSummary.receiverRateValue || 0, rateSummary.senderRateValue || 0))
      );

      const makeChip = (label) => {
        const span = document.createElement('span');
        span.className = 'inline-flex items-center gap-1 rounded-full border border-slate-800 bg-slate-900/70 px-2.5 py-1 text-[11px] font-semibold text-slate-200';
        span.textContent = label;
        return span;
      };

      const buildRateRow = (label, value, displayValue, gradient) => {
        const wrap = document.createElement('div');
        wrap.className = 'space-y-1 rounded-xl border border-slate-800/60 bg-slate-950/40 p-3';
        const header = document.createElement('div');
        header.className = 'flex items-center justify-between text-xs text-slate-400';
        header.innerHTML = `<span>${label}</span><span class="font-semibold text-slate-100">${displayValue}</span>`;

        const barWrap = document.createElement('div');
        barWrap.className = 'h-2 w-full overflow-hidden rounded-full bg-slate-800/80';
        const bar = document.createElement('div');
        if (value) {
          bar.className = `h-2 rounded-full bg-gradient-to-r ${gradient}`;
          bar.style.width = `${Math.min(100, (value / maxRate) * 100)}%`;
        } else {
          bar.className = 'h-2 rounded-full bg-slate-700';
          bar.style.width = '14%';
        }
        barWrap.appendChild(bar);
        wrap.appendChild(header);
        wrap.appendChild(barWrap);
        return wrap;
      };

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

      enrichedTests.forEach(({ test, metrics, rateSummary, latencyValue, jitterValue }) => {
        const pathLabel = `${formatNodeLabel(test.src_node_id)} → ${formatNodeLabel(test.dst_node_id)}`;
        const typeLabel = `${test.protocol.toUpperCase()}${test.params?.reverse ? ' (-R)' : ''}`;

        const card = document.createElement('div');
        card.className = 'space-y-3 rounded-2xl border border-slate-800/70 bg-slate-900/60 p-4 shadow-sm shadow-black/30';

        const header = document.createElement('div');
        header.className = 'flex flex-wrap items-center justify-between gap-2';
        const title = document.createElement('div');
        title.innerHTML = `<p class="text-xs uppercase tracking-[0.2em] text-sky-300/70">#${test.id} · ${typeLabel}</p>` +
          `<p class="text-lg font-semibold text-white">${pathLabel}</p>`;
        header.appendChild(title);

        const statusPill = document.createElement('span');
        statusPill.className = 'inline-flex items-center gap-2 rounded-full bg-slate-800/70 px-3 py-1 text-xs font-semibold text-slate-200 ring-1 ring-slate-700';
        statusPill.textContent = rateSummary.status === 'ok' ? '完成' : (rateSummary.status || '未知');
        header.appendChild(statusPill);
        card.appendChild(header);

        const ratesGrid = document.createElement('div');
        ratesGrid.className = 'grid gap-3 sm:grid-cols-2';
        ratesGrid.appendChild(buildRateRow('接收速率 (Mbps)', rateSummary.receiverRateValue, rateSummary.receiverRateMbps, 'from-emerald-400 to-sky-500'));
        ratesGrid.appendChild(buildRateRow('发送速率 (Mbps)', rateSummary.senderRateValue, rateSummary.senderRateMbps, 'from-amber-400 to-rose-500'));
        card.appendChild(ratesGrid);

        const metaChips = document.createElement('div');
        metaChips.className = 'flex flex-wrap items-center gap-2 text-xs text-slate-400';
        metaChips.appendChild(makeChip(test.protocol.toLowerCase() === 'udp' ? 'UDP 测试' : 'TCP 测试'));
        if (test.params?.reverse) metaChips.appendChild(makeChip('反向 (-R)'));
        metaChips.appendChild(makeChip(latencyValue !== null ? `延迟 ${formatMetric(latencyValue)} ms` : '延迟 N/A'));
        metaChips.appendChild(makeChip(jitterValue !== null ? `抖动 ${formatMetric(jitterValue)} ms` : '抖动 N/A'));
        if (metrics.lostPercent !== undefined && metrics.lostPercent !== null) {
          metaChips.appendChild(makeChip(`丢包 ${formatMetric(metrics.lostPercent)}%`));
        }
        card.appendChild(metaChips);

        const actions = document.createElement('div');
        actions.className = 'flex flex-wrap items-center justify-between gap-3';

        const buttons = document.createElement('div');
        buttons.className = 'flex flex-wrap gap-2';
        const detailsBtn = document.createElement('button');
        detailsBtn.textContent = '详情';
        detailsBtn.className = styles.pillInfo;
        detailsBtn.onclick = () => toggleDetail(test.id, detailsBtn);
        const deleteBtn = document.createElement('button');
        deleteBtn.textContent = '删除';
        deleteBtn.className = styles.pillDanger;
        deleteBtn.onclick = () => deleteTestResult(test.id);
        buttons.appendChild(detailsBtn);
        buttons.appendChild(deleteBtn);

        const congestion = document.createElement('span');
        congestion.className = 'rounded-full bg-slate-800/80 px-3 py-1 text-xs font-semibold text-slate-300 ring-1 ring-slate-700';
        congestion.textContent = `拥塞：${rateSummary.senderCongestion} / ${rateSummary.receiverCongestion}`;

        actions.appendChild(buttons);
        actions.appendChild(congestion);
        card.appendChild(actions);

        const block = buildTestDetailsBlock(test, metrics, latencyValue, pathLabel);
        detailBlocks.set(test.id, block);

        testsList.appendChild(card);
        testsList.appendChild(block);
      });
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
        iperf_port: Number(nodeIperf.value || DEFAULT_IPERF_PORT),
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
      closeAddNodeModal();
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
        port: Number(testPortInput.value || (selectedDst ? (selectedDst.detected_iperf_port || selectedDst.iperf_port) : DEFAULT_IPERF_PORT)),
        reverse: reverseToggle?.checked || false,
      };

      const finishProgress = startProgressBar(
        testProgress,
        testProgressBar,
        testProgressLabel,
        payload.duration * 1000 + 1500,
        '开始链路测试...'
      );

      const res = await fetch('/tests', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });

      if (!res.ok) {
        const details = await res.text();
        const message = details ? `启动测试失败：${details}` : '启动测试失败，请确认节点存在且参数有效。';
        setAlert(testAlert, message);
        finishProgress('测试失败');
        return;
      }

      await refreshTests();
      finishProgress('测试完成');
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
      const pickFirst = (...values) => values.find((v) => v !== undefined && v !== null);

      const lossFromPackets = sumReceived && sumReceived.lost_packets !== undefined && sumReceived.packets
        ? (sumReceived.lost_packets / sumReceived.packets) * 100
        : undefined;

      const bitsPerSecond = pickFirst(
        sumReceived?.bits_per_second,
        receiverStream?.bits_per_second,
        sumSent?.bits_per_second,
        senderStream?.bits_per_second,
      );

      const jitterMs = pickFirst(
        sumReceived?.jitter_ms,
        sumSent?.jitter_ms,
        receiverStream?.jitter_ms,
        senderStream?.jitter_ms,
      );

      const lostPercent = pickFirst(
        sumReceived?.lost_percent,
        lossFromPackets,
        sumSent?.lost_percent,
        receiverStream?.lost_percent,
        senderStream?.lost_percent,
      );

      let latencyMs = pickFirst(
        senderStream?.mean_rtt,
        senderStream?.rtt,
        receiverStream?.mean_rtt,
        receiverStream?.rtt,
      );
      if (latencyMs !== undefined && latencyMs !== null && latencyMs > 1000) {
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
        senderRateValue: sumSent.bits_per_second ? sumSent.bits_per_second / 1e6 : null,
        receiverRateValue: sumReceived.bits_per_second ? sumReceived.bits_per_second / 1e6 : null,
        senderCongestion: end.sender_tcp_congestion || 'N/A',
        receiverCongestion: end.receiver_tcp_congestion || 'N/A',
        status: raw && raw.status ? raw.status : 'unknown',
      };
    }

    function formatMetric(value, decimals = 2) {
      if (value === undefined || value === null || Number.isNaN(value)) return 'N/A';
      return Number(value).toFixed(decimals);
    }

    function renderBackboneBadges(entries) {
      if (!entries || !entries.length) return '';

      const labelMap = { zj_cu: 'CU', zj_ct: 'CT', zj_cm: 'CM' };
      return entries
        .map((item) => {
          const label = labelMap[item.key] || (item.name || item.key || '').slice(0, 2).toUpperCase();
          const hasLatency = item.latency_ms !== undefined && item.latency_ms !== null;
          const chipStyle = hasLatency
            ? 'bg-sky-500/10 text-sky-100 border-sky-500/40'
            : 'bg-rose-500/15 text-rose-200 border-rose-500/40';
          const latencyLabel = hasLatency ? `${formatMetric(item.latency_ms, 0)} ms` : '不可达';
          return `<span class="inline-flex items-center gap-1 rounded-full border px-2.5 py-1 text-[11px] font-semibold ${chipStyle}">${label}<span class=\"text-[10px] text-slate-300\">${latencyLabel}</span></span>`;
        })
        .join('');
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
      const directionLabel = test.params?.reverse ? ' (反向)' : '';
      summary.innerHTML = `<strong>#${test.id} ${pathLabel}</strong> · ${test.protocol.toUpperCase()}${directionLabel} · 端口 ${test.params.port} · 时长 ${test.params.duration}s<br/>` +
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
    changePasswordBtn?.addEventListener('click', changePassword);
    saveNodeBtn.addEventListener('click', saveNode);

    if (openAddNodeBtn) {
      openAddNodeBtn.addEventListener('click', () => {
        resetNodeForm();
        openAddNodeModal();
      });
    }

    if (closeAddNodeBtn) {
      closeAddNodeBtn.addEventListener('click', () => {
        closeAddNodeModal();
        resetNodeForm();
      });
    }

    if (cancelAddNodeBtn) {
      cancelAddNodeBtn.addEventListener('click', () => {
        closeAddNodeModal();
        resetNodeForm();
      });
    }

    if (addNodeModal) {
      addNodeModal.addEventListener('click', (event) => {
        if (event.target === addNodeModal) {
          closeAddNodeModal();
          resetNodeForm();
        }
      });
    }

    importConfigsBtn.addEventListener('click', () => configFileInput.click());
    exportConfigsBtn.addEventListener('click', exportAgentConfigs);
    configFileInput.addEventListener('change', (e) => importAgentConfigs(e.target.files[0]));
    document.getElementById('refresh-tests').addEventListener('click', refreshTests);
    deleteAllTestsBtn.addEventListener('click', clearAllTests);

    document.querySelectorAll('[data-refresh-nodes]').forEach((btn) => btn.addEventListener('click', refreshNodes));
    dstSelect.addEventListener('change', syncTestPort);
    document.getElementById('password').addEventListener('keyup', (e) => { if (e.key === 'Enter') login(); });

    function updateNodeStreamingBadges(nodeId) {
      const container = document.querySelector(`[data-streaming-badges="${nodeId}"]`);
      if (container) {
        container.innerHTML = renderStreamingBadges(nodeId);
      }
    }

    function ensureAutoRefresh() {
      if (nodeRefreshInterval) return;
      nodeRefreshInterval = setInterval(() => refreshNodes(), 10000);
    }

    checkAuth();
    ensureAutoRefresh();
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
    body {
      font-family: 'Inter', system-ui, -apple-system, sans-serif;
      background:
        radial-gradient(circle at 12% 20%, rgba(56, 189, 248, 0.08), transparent 32%),
        radial-gradient(circle at 88% 12%, rgba(16, 185, 129, 0.08), transparent 40%),
        linear-gradient(135deg, rgba(15, 23, 42, 0.94), rgba(2, 6, 23, 0.96));
      background-attachment: fixed;
      color: #e2e8f0;
      margin: 0;
      padding: 0;
    }
    .glass-card {
      border: 1px solid rgba(148, 163, 184, 0.2);
      background: linear-gradient(160deg, rgba(15, 23, 42, 0.92), rgba(22, 33, 61, 0.82));
      box-shadow: 0 26px 80px rgba(0, 0, 0, 0.45), inset 0 1px 0 rgba(255, 255, 255, 0.04);
      backdrop-filter: blur(14px);
    }
    .panel-card {
      border: 1px solid rgba(148, 163, 184, 0.2);
      background: linear-gradient(135deg, rgba(15, 23, 42, 0.92), rgba(12, 20, 38, 0.82));
      box-shadow: 0 20px 60px rgba(0, 0, 0, 0.35), inset 0 1px 0 rgba(255, 255, 255, 0.04);
      backdrop-filter: blur(12px);
    }
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
              <input id="schedule-port" type="number" value="62001" class="rounded-xl border border-slate-800 bg-slate-900/60 px-3 py-2 text-sm text-slate-100 placeholder:text-slate-500 focus:border-sky-500 focus:outline-none focus:ring-2 focus:ring-sky-500/60" />
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
      rowCard: 'relative overflow-hidden rounded-2xl border border-slate-800/80 bg-gradient-to-br from-slate-900/80 via-slate-900/60 to-slate-950/80 p-4 shadow-[0_25px_80px_rgba(0,0,0,0.45)] ring-1 ring-white/5 space-y-3',
      inline: 'flex flex-wrap items-center gap-3',
      pillInfo: 'inline-flex items-center justify-center gap-2 rounded-lg bg-gradient-to-r from-sky-500/20 via-sky-500/15 to-indigo-500/20 px-3 py-2 text-xs font-semibold text-sky-50 ring-1 ring-sky-400/40 shadow-[0_12px_30px_rgba(14,165,233,0.18)] transition hover:scale-[1.01] hover:ring-sky-300/60',
      pillDanger: 'inline-flex items-center justify-center gap-2 rounded-lg bg-gradient-to-r from-rose-500/20 via-rose-500/15 to-orange-500/20 px-3 py-2 text-xs font-semibold text-rose-50 ring-1 ring-rose-400/40 shadow-[0_12px_30px_rgba(244,63,94,0.18)] transition hover:scale-[1.01] hover:ring-rose-300/60',
      textMuted: 'text-slate-300/90 text-sm drop-shadow-sm',
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
        port: Number(schedulePort.value || DEFAULT_IPERF_PORT),
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
    if password != _current_dashboard_password():
        raise HTTPException(status_code=401, detail="invalid_password")

    _set_auth_cookie(response, password)
    return {"status": "ok"}


@app.post("/auth/logout")
def logout(response: Response) -> dict:
    response.delete_cookie(settings.dashboard_cookie_name)
    return {"status": "logged_out"}


@app.post("/auth/change")
def change_password(request: Request, response: Response, payload: dict = Body(...)) -> dict:
    global _dashboard_password

    current_password = str(payload.get("current_password", ""))
    new_password = str(payload.get("new_password", ""))
    confirm_password = str(payload.get("confirm_password", new_password))

    if not new_password:
        raise HTTPException(status_code=400, detail="empty_password")

    if len(new_password) < 6:
        raise HTTPException(status_code=400, detail="password_too_short")

    if new_password != confirm_password:
        raise HTTPException(status_code=400, detail="password_mismatch")

    if not _is_authenticated(request) and current_password != _current_dashboard_password():
        raise HTTPException(status_code=401, detail="invalid_password")

    _dashboard_password = new_password
    _save_dashboard_password(new_password)
    _set_auth_cookie(response, new_password)

    return {"status": "updated"}


async def _start_iperf_server(node: Node, port: int) -> None:
    agent_url = f"http://{node.ip}:{node.agent_port}/start_server"
    try:
        async with httpx.AsyncClient(timeout=settings.request_timeout) as client:
            response = await client.post(agent_url, json={"port": port})
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"failed to reach destination agent: {exc}")

    if response.status_code != 200:
        detail = response.text or "failed to start iperf server"
        raise HTTPException(status_code=502, detail=detail)


async def _stop_iperf_server(node: Node) -> None:
    agent_url = f"http://{node.ip}:{node.agent_port}/stop_server"
    try:
        async with httpx.AsyncClient(timeout=settings.request_timeout) as client:
            response = await client.post(agent_url)
            if response.status_code != 200:
                logger.warning("Failed to stop iperf server for %s: %s", node.name, response.text)
    except httpx.RequestError:
        logger.exception("Failed to reach destination agent when stopping server for %s", node.name)


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
                latency_payload = data.get("backbone_latency") or []
                streaming_payload = data.get("streaming") or []
                streaming_checked_at = data.get("streaming_checked_at")
                backbone_latency = [
                    BackboneLatency(**item) for item in latency_payload if isinstance(item, dict)
                ]
                streaming_statuses = [
                    StreamingServiceStatus(**item)
                    for item in streaming_payload
                    if isinstance(item, dict)
                ]
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
                    backbone_latency=backbone_latency or None,
                    streaming=streaming_statuses or None,
                    streaming_checked_at=streaming_checked_at,
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
    health_monitor.invalidate(obj.id)
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
    health_monitor.invalidate(node.id)
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
    health_monitor.invalidate(node_id)
    return {"status": "deleted"}


@app.get("/nodes/status", response_model=List[NodeWithStatus])
async def nodes_with_status(db: Session = Depends(get_db)):
    return await health_monitor.get_statuses()


@app.get("/latency/zhejiang", response_model=List[BackboneLatency])
async def zhejiang_latency() -> List[BackboneLatency]:
    return await backbone_monitor.get_statuses()


@app.post("/nodes/{node_id}/streaming-test", response_model=StreamingTestResult)
async def streaming_test(node_id: int, db: Session = Depends(get_db)):
    node = db.get(Node, node_id)
    if not node:
        raise HTTPException(status_code=404, detail="node not found")

    return await _probe_streaming_unlock(node)


@app.post("/tests", response_model=TestRead)
async def create_test(test: TestCreate, db: Session = Depends(get_db)):
    src = db.get(Node, test.src_node_id)
    dst = db.get(Node, test.dst_node_id)
    if not src or not dst:
        raise HTTPException(status_code=404, detail="node not found")

    requested_port = test.port
    src_status = await health_monitor.check_node(src)
    if src_status.status != "online":
        raise HTTPException(status_code=503, detail="source node is offline or unreachable")

    dst_status = await health_monitor.check_node(dst)
    if dst_status.status != "online":
        raise HTTPException(status_code=503, detail="destination node is offline or unreachable")

    server_started = False
    current_port = dst_status.detected_iperf_port or dst_status.iperf_port
    if not dst_status.server_running:
        await _start_iperf_server(dst, requested_port)
        server_started = True
    elif current_port != requested_port:
        await _stop_iperf_server(dst)
        await _start_iperf_server(dst, requested_port)
        server_started = True

    agent_url = f"http://{src.ip}:{src.agent_port}/run_test"
    payload = {
        "target": dst.ip,
        "port": requested_port,
        "duration": test.duration,
        "protocol": test.protocol,
        "parallel": test.parallel,
        "reverse": test.reverse,
    }

    try:
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
    finally:
        if server_started:
            await _stop_iperf_server(dst)
        health_monitor.invalidate(dst.id)

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
