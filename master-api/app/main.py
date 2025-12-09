import asyncio
import json
import logging
import socket
import time
import ipaddress
from datetime import datetime, timezone
from typing import Dict, List

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

import httpx
from fastapi import BackgroundTasks, Body, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse
from sqlalchemy import or_, select, text
from sqlalchemy.orm import Session

from .auth import auth_manager
from .config import settings
from .constants import DEFAULT_IPERF_PORT
from .database import SessionLocal, engine, get_db
from .agent_store import AgentConfigStore
from .models import Base, Node, TestResult, TestSchedule, ScheduleResult
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
    DualSuiteTestCreate,
    BackboneLatency,
    StreamingServiceStatus,
    StreamingTestResult,
    PasswordChangeRequest,
)
from .state_store import StateStore

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# ============================================================================
# Scheduler Setup
# ============================================================================

scheduler = AsyncIOScheduler()
scheduler.start()


def _log_dashboard_password() -> None:
    manager = auth_manager()
    logger.warning("Dashboard password initialized: %s", manager.current_password())


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


def _init_database_with_retry(max_attempts: int = 5, delay_seconds: float = 2.0) -> None:
    """Initialize database schema with simple retry to handle cold starts."""

    attempt = 0
    while True:
        attempt += 1
        try:
            Base.metadata.create_all(bind=engine)
            _ensure_iperf_port_column()
            _ensure_test_result_columns()
            return
        except Exception:  # pragma: no cover - best-effort bootstrap
            logger.exception(
                "Database initialization failed (attempt %s/%s)", attempt, max_attempts
            )
            if attempt >= max_attempts:
                raise
            time.sleep(delay_seconds)


_init_database_with_retry()

state_store = StateStore(settings.state_file, settings.state_recent_tests)
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


def _agent_config_from_node(node: Node) -> AgentConfigCreate:
    return AgentConfigCreate(
        name=node.name,
        host=node.ip,
        agent_port=node.agent_port,
        iperf_port=node.iperf_port,
        description=node.description,
    )


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
                region=item.get("region"),
                tier=item.get("tier"),
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
dashboard_auth = auth_manager()


FAVICON_SVG = """<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64' fill='none'>
<rect width='64' height='64' rx='14' fill='url(#g)'/>
<path d='M16 42c9-2 12-4 16-10 4 6 7 8 16 10-6 4-10 6-16 14-6-8-10-10-16-14Z' fill='#e2e8f0' fill-opacity='.9'/>
<circle cx='32' cy='22' r='8' stroke='#e2e8f0' stroke-width='4' stroke-linecap='round'/>
<defs><linearGradient id='g' x1='8' y1='8' x2='56' y2='56' gradientUnits='userSpaceOnUse'><stop stop-color='#0ea5e9'/><stop offset='1' stop-color='#22c55e'/></linearGradient></defs>
</svg>"""


@app.get("/favicon.ico")
def favicon() -> Response:
    return Response(
        content=FAVICON_SVG,
        media_type="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )


def _is_authenticated(request: Request) -> bool:
    return dashboard_auth.is_authenticated(request)


def _set_auth_cookie(response: Response, password: str) -> None:
    dashboard_auth.set_auth_cookie(response, password)





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
                statuses = await self.refresh()
                await self._sync_ports(statuses)
            except Exception:
                logger.exception("Failed to refresh node health")
            await asyncio.sleep(self.interval_seconds)

    async def _sync_ports(self, statuses: List[NodeWithStatus]) -> None:
        """Sync detected iperf ports to database if different."""
        updates = {}
        for s in statuses:
             if s.detected_iperf_port and s.detected_iperf_port != s.iperf_port:
                 updates[s.id] = s.detected_iperf_port
        
        if updates:
            db = SessionLocal()
            try:
                for nid, port in updates.items():
                    node = db.get(Node, nid)
                    if node:
                        node.iperf_port = port
                        logger.info(f"Auto-updating iperf port for node {node.name}: {port}")
                db.commit()
            except Exception as e:
                logger.error(f"Failed to sync ports: {e}")
            finally:
                db.close()

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
    _load_schedules_on_startup()
    _log_dashboard_password()


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
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@shoelace-style/shoelace@2.15.1/cdn/themes/dark.css" />
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@radix-ui/themes@3.1.1/dist/css/themes.css" />
  <script type="module" src="https://cdn.jsdelivr.net/npm/@shoelace-style/shoelace@2.15.1/cdn/shoelace.js"></script>
  <script src="https://cdn.tailwindcss.com"></script>
  <style>
    :root {
      --primary: #3b82f6;
      --primary-hover: #2563eb;
      --bg-dark: #0f172a;
      --card-bg: rgba(30, 41, 59, 0.7);
      --glass-border: rgba(255, 255, 255, 0.08);
      --text-main: #f8fafc;
      --text-muted: #94a3b8;
    }
    body {
      font-family: 'Inter', system-ui, -apple-system, sans-serif;
      background-color: var(--bg-dark);
      background-image: 
        radial-gradient(at 0% 0%, rgba(56, 189, 248, 0.15) 0px, transparent 50%),
        radial-gradient(at 100% 0%, rgba(139, 92, 246, 0.15) 0px, transparent 50%),
        radial-gradient(at 100% 100%, rgba(16, 185, 129, 0.15) 0px, transparent 50%),
        radial-gradient(at 0% 100%, rgba(244, 63, 94, 0.15) 0px, transparent 50%);
      background-attachment: fixed;
      color: var(--text-main);
      margin: 0;
      padding: 0;
      min-height: 100vh;
      display: flex;
      flex-direction: column;
    }
    .page-frame {
      flex: 1;
      display: flex;
      flex-direction: column;
      padding: 2rem 1rem;
    }
    .page-content {
      width: 100%;
      max-width: 1200px;
      margin: 0 auto;
      flex: 1;
      display: flex;
      flex-direction: column;
      gap: 2rem;
    }
    .glass-panel {
      background: var(--card-bg);
      backdrop-filter: blur(12px);
      border: 1px solid var(--glass-border);
      border-radius: 1.5rem;
      box-shadow: 0 25px 50px -12px rgba(0, 0, 0, 0.5);
    }
    
    /* Login Specifics */
    .login-container {
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      flex: 1;
      min-height: 60vh;
    }
    .login-card {
      width: 100%;
      max-width: 420px;
      padding: 2.5rem;
      position: relative;
      overflow: hidden;
    }
    .login-card::before {
      content: '';
      position: absolute;
      top: 0; left: 0; right: 0; height: 4px;
      background: linear-gradient(90deg, #3b82f6, #8b5cf6, #ec4899);
    }
    .login-title {
      font-size: 1.875rem;
      font-weight: 700;
      margin-bottom: 0.5rem;
      text-align: center;
      background: linear-gradient(to right, #60a5fa, #c084fc);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
    }
    .login-subtitle {
      text-align: center;
      color: var(--text-muted);
      font-size: 0.95rem;
      margin-bottom: 2rem;
    }
    
    .form-group { margin-bottom: 1.5rem; }
    .form-label {
      display: block;
      color: #e2e8f0;
      font-size: 0.875rem;
      font-weight: 500;
      margin-bottom: 0.5rem;
    }
    .form-input {
      width: 100%;
      background: rgba(15, 23, 42, 0.6);
      border: 1px solid rgba(148, 163, 184, 0.2);
      border-radius: 0.75rem;
      padding: 0.75rem 1rem;
      color: #fff;
      font-size: 0.95rem;
      transition: all 0.2s;
    }
    .form-input:focus {
      outline: none;
      border-color: #60a5fa;
      box-shadow: 0 0 0 2px rgba(96, 165, 250, 0.2);
      background: rgba(15, 23, 42, 0.8);
    }
    
    .btn-primary {
      width: 100%;
      background: linear-gradient(135deg, #3b82f6, #2563eb);
      color: white;
      font-weight: 600;
      padding: 0.75rem;
      border-radius: 0.75rem;
      border: none;
      cursor: pointer;
      font-size: 1rem;
      transition: transform 0.1s, box-shadow 0.2s;
      box-shadow: 0 4px 12px rgba(37, 99, 235, 0.3);
    }
    .btn-primary:hover {
      transform: translateY(-1px);
      box-shadow: 0 6px 16px rgba(37, 99, 235, 0.4);
    }
    .btn-primary:active { transform: translateY(0); }
    
    .status-badge {
      display: inline-flex;
      align-items: center;
      gap: 0.5rem;
      padding: 0.25rem 0.75rem;
      border-radius: 999px;
      font-size: 0.75rem;
      font-weight: 500;
      background: rgba(255, 255, 255, 0.05);
      border: 1px solid rgba(255, 255, 255, 0.1);
      color: var(--text-muted);
      margin: 0 auto;
    }
    .status-dot {
      width: 8px; height: 8px;
      border-radius: 50%;
      background-color: #fbbf24;
      box-shadow: 0 0 8px rgba(251, 191, 36, 0.5);
    }
    
    /* Dashboard Specifics */
    .panel-card {
      background: rgba(30, 41, 59, 0.4);
      border: 1px solid rgba(148, 163, 184, 0.1);
      backdrop-filter: blur(8px);
    }
    .alert {
      padding: 0.75rem 1rem;
      border-radius: 0.75rem;
      font-size: 0.9rem;
      margin-bottom: 1rem;
      border: 1px solid transparent;
    }
    .alert-error {
      background: rgba(239, 68, 68, 0.15);
      border-color: rgba(239, 68, 68, 0.3);
      color: #fca5a5;
    }
    .alert-success {
      background: rgba(34, 197, 94, 0.15);
      border-color: rgba(34, 197, 94, 0.3);
      color: #86efac;
    }
    .hidden { display: none !important; }

    /* Animations */
    @keyframes shake {
      0%, 100% { transform: translateX(0); }
      25% { transform: translateX(-8px); }
      75% { transform: translateX(8px); }
    }
    .animate-shake {
      animation: shake 0.4s cubic-bezier(.36,.07,.19,.97) both;
      border-color: rgba(239, 68, 68, 0.5);
      box-shadow: 0 0 0 4px rgba(239, 68, 68, 0.1);
    }
    
    @keyframes success-pulse {
      0% { transform: scale(1); box-shadow: 0 0 0 0 rgba(34, 197, 94, 0.7); border-color: rgba(34, 197, 94, 0.4); }
      50% { transform: scale(1.02); box-shadow: 0 0 0 10px rgba(34, 197, 94, 0); border-color: rgba(34, 197, 94, 0.8); }
      100% { transform: scale(1); box-shadow: 0 0 0 0 rgba(34, 197, 94, 0); border-color: rgba(34, 197, 94, 0.4); }
    }
    .animate-success {
      animation: success-pulse 0.6s ease-out;
      border-color: rgba(34, 197, 94, 0.6);
    }
  </style>
</head>
<body>
  <div class="radix-themes min-h-screen" data-theme="dark">
    <div class="page-frame">
      <div class="page-content">

        <div class="card-stack">
          <div class="login-container hidden" id="login-card">
            <div class="glass-panel login-card">
              <h1 class="login-title">iperf web login</h1>
              
              <div id="login-alert" class="alert alert-error hidden"></div>

              <form id="login-form">
                <div class="form-group">
                  <label class="form-label" for="password-input">Password</label>
                  <input id="password-input" class="form-input" type="password" placeholder="Enter dashboard password" autocomplete="current-password" required />
                </div>
                <button id="login-btn" type="button" class="btn-primary">
                  Login
                </button>
              </form>
            </div>
          </div>

          <div id="app-card" class="hidden space-y-8 app-card">
            <div class="flex flex-col gap-3 lg:flex-row lg:items-center lg:justify-between">
              <div>
                <p class="text-sm uppercase tracking-[0.25em] text-sky-300/80">控制面板</p>
                <h2 class="text-2xl font-semibold text-white">iperf3 主控面板</h2>
                <p class="text-sm text-slate-400" id="auth-hint"></p>
              </div>
              <div class="flex flex-wrap items-center gap-3">
                <button data-refresh-nodes onclick="refreshNodes()" class="rounded-lg border border-slate-700 bg-slate-800/60 px-4 py-2 text-sm font-semibold text-slate-100 shadow-sm transition hover:border-sky-500 hover:text-sky-200">刷新节点</button>
                <a href="/web/schedules" class="rounded-lg border border-emerald-500/40 bg-emerald-500/15 px-4 py-2 text-sm font-semibold text-emerald-100 shadow-sm transition hover:bg-emerald-500/25">定时任务</a>
                <button id="open-settings" onclick="toggleSettingsModal(true)" class="rounded-lg border border-indigo-500/40 bg-indigo-500/15 px-4 py-2 text-sm font-semibold text-indigo-100 shadow-sm transition hover:bg-indigo-500/25 inline-flex items-center gap-2">
                  <span class="text-base">⚙️</span>
                  <span>设置</span>
                </button>
                <button id="logout-btn" class="rounded-lg border border-rose-500/40 bg-rose-500/15 px-4 py-2 text-sm font-semibold text-rose-100 shadow-sm transition hover:bg-rose-500/25">退出登录</button>
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
                    <button data-refresh-nodes onclick="refreshNodes()" class="rounded-lg border border-slate-700 bg-slate-800/60 px-4 py-2 text-sm font-semibold text-slate-100 shadow-sm transition hover:border-sky-500 hover:text-sky-200">刷新</button>
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
              
              <!-- IP Whitelist Management -->
              <div class="glass-card rounded-2xl p-6">
                <div class="flex flex-wrap items-center justify-between gap-3 mb-4">
                  <div>
                    <h3 class="text-lg font-semibold text-white">🔒 IP 白名单管理</h3>
                    <p class="text-sm text-slate-400">防止未授权 IP 滥用 iperf3 服务</p>
                  </div>
                  <button id="sync-whitelist-btn" onclick="syncWhitelist()" class="rounded-lg border border-sky-500/40 bg-sky-500/15 px-4 py-2 text-sm font-semibold text-sky-100 shadow-sm transition hover:bg-sky-500/25">
                    同步白名单到所有 Agent
                  </button>
                </div>
                
                <div id="whitelist-status" class="space-y-3">
                  <div class="flex items-center gap-2 text-sm">
                    <span class="text-slate-400">状态:</span>
                    <span id="whitelist-status-text" class="text-emerald-400">● 自动同步已启用</span>
                  </div>
                  <div class="flex items-center gap-2 text-sm">
                    <span class="text-slate-400">说明:</span>
                    <span class="text-slate-300">添加/删除节点时自动同步白名单，无需手动操作</span>
                  </div>
                  <div id="whitelist-sync-result" class="hidden rounded-lg border border-slate-700 bg-slate-900/50 p-3 text-xs">
                    <!-- Sync results will be displayed here -->
                  </div>
                </div>
              </div>

              <div class="panel-card rounded-2xl p-5 space-y-4">
                <div class="flex flex-wrap items-center justify-between gap-3">
                  <div>
                    <p class="text-xs uppercase tracking-[0.2em] text-sky-300/70">IPERF3 测试</p>
                    <h3 class="text-lg font-semibold text-white">测试计划</h3>
                  </div>
                  <div class="inline-flex items-center gap-2 rounded-full border border-slate-700/70 bg-slate-900/70 p-1 shadow-inner shadow-black/20">
                    <button id="single-test-tab" class="rounded-full bg-gradient-to-r from-sky-500/80 to-indigo-500/80 px-4 py-1.5 text-xs font-semibold text-slate-50 shadow-lg shadow-sky-500/15 ring-1 ring-sky-400/40 transition hover:brightness-110">单程测试</button>
                    <button id="suite-test-tab" class="rounded-full px-4 py-1.5 text-xs font-semibold text-slate-300 transition hover:text-white">双向 TCP/UDP 测试</button>
                  </div>
                </div>
                <p id="test-panel-intro" class="text-sm text-slate-400">快速规划 iperf3 单程或双向链路测试，支持限速、并行与反向 (-R)。</p>
                <div id="test-alert" class="hidden rounded-xl border border-rose-500/40 bg-rose-500/10 px-4 py-3 text-sm text-rose-100"></div>

                <div id="single-test-panel" class="space-y-4">
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
                    <div class="space-y-2 hidden">
                      <label class="text-sm font-medium text-slate-200">端口</label>
                      <input id="test-port" type="number" value="62001" class="rounded-xl border border-slate-800 bg-slate-900/60 px-3 py-2 text-sm text-slate-100 placeholder:text-slate-500 focus:border-sky-500 focus:outline-none focus:ring-2 focus:ring-sky-500/60" />
                    </div>
                    <div class="space-y-2">
                      <label class="text-sm font-medium text-slate-200">忽略前（秒）</label>
                      <input id="omit" type="number" value="0" class="rounded-xl border border-slate-800 bg-slate-900/60 px-3 py-2 text-sm text-slate-100 placeholder:text-slate-500 focus:border-sky-500 focus:outline-none focus:ring-2 focus:ring-sky-500/60" />
                    </div>
                  </div>
                  <div id="tcp-options" class="grid gap-3 sm:grid-cols-2">
                    <div class="space-y-2">
                      <label class="text-sm font-medium text-slate-200">TCP 限速带宽 (-b，可选)</label>
                      <input id="tcp-bandwidth" type="text" placeholder="例如 0（不限）或 500M" class="rounded-xl border border-slate-800 bg-slate-900/60 px-3 py-2 text-sm text-slate-100 placeholder:text-slate-500 focus:border-sky-500 focus:outline-none focus:ring-2 focus:ring-sky-500/60" />
                    </div>
                  </div>
                  <div id="udp-options" class="hidden grid gap-3 sm:grid-cols-3">
                    <div class="space-y-2">
                      <label class="text-sm font-medium text-slate-200">UDP 带宽 (-b)</label>
                      <input id="udp-bandwidth" type="text" value="100M" class="rounded-xl border border-slate-800 bg-slate-900/60 px-3 py-2 text-sm text-slate-100 placeholder:text-slate-500 focus:border-sky-500 focus:outline-none focus:ring-2 focus:ring-sky-500/60" />
                    </div>
                    <div class="space-y-2">
                      <label class="text-sm font-medium text-slate-200">UDP 包长 (-l)</label>
                      <input id="udp-len" type="number" value="1400" class="rounded-xl border border-slate-800 bg-slate-900/60 px-3 py-2 text-sm text-slate-100 placeholder:text-slate-500 focus:border-sky-500 focus:outline-none focus:ring-2 focus:ring-sky-500/60" />
                    </div>
                    <div class="space-y-2">
                      <label class="text-sm font-medium text-slate-200">UDP 备注</label>
                      <p class="rounded-xl border border-slate-800 bg-slate-900/40 px-3 py-2 text-xs text-slate-400">默认 100M/1400B，可根据链路容量调整。</p>
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
                </div>

                <div id="suite-test-panel" class="hidden space-y-4">
                  <p class="text-sm text-slate-400">一键完成 TCP/UDP 去回四项测试，适合基线验证与跨运营商链路对比。</p>
                  <div class="grid gap-3 sm:grid-cols-2">
                    <div class="space-y-2">
                      <label class="text-sm font-medium text-slate-200">源节点</label>
                      <select id="suite-src-select" class="rounded-xl border border-slate-800 bg-slate-900/60 px-3 py-2 text-sm text-slate-100 focus:border-sky-500 focus:outline-none focus:ring-2 focus:ring-sky-500/60"></select>
                    </div>
                    <div class="space-y-2">
                      <label class="text-sm font-medium text-slate-200">目标节点</label>
                      <select id="suite-dst-select" class="rounded-xl border border-slate-800 bg-slate-900/60 px-3 py-2 text-sm text-slate-100 focus:border-sky-500 focus:outline-none focus:ring-2 focus:ring-sky-500/60"></select>
                    </div>
                    <div class="space-y-2">
                      <label class="text-sm font-medium text-slate-200">时长（秒）</label>
                      <input id="suite-duration" type="number" value="10" class="rounded-xl border border-slate-800 bg-slate-900/60 px-3 py-2 text-sm text-slate-100 placeholder:text-slate-500 focus:border-sky-500 focus:outline-none focus:ring-2 focus:ring-sky-500/60" />
                    </div>
                    <div class="space-y-2">
                      <label class="text-sm font-medium text-slate-200">并行数 (P)</label>
                      <input id="suite-parallel" type="number" value="1" class="rounded-xl border border-slate-800 bg-slate-900/60 px-3 py-2 text-sm text-slate-100 placeholder:text-slate-500 focus:border-sky-500 focus:outline-none focus:ring-2 focus:ring-sky-500/60" />
                    </div>
                    <div class="space-y-2 hidden">
                      <label class="text-sm font-medium text-slate-200">端口</label>
                      <input id="suite-port" type="number" value="62001" class="rounded-xl border border-slate-800 bg-slate-900/60 px-3 py-2 text-sm text-slate-100 placeholder:text-slate-500 focus:border-sky-500 focus:outline-none focus:ring-2 focus:ring-sky-500/60" />
                    </div>
                    <div class="space-y-2">
                      <label class="text-sm font-medium text-slate-200">忽略前（秒）</label>
                      <input id="suite-omit" type="number" value="0" class="rounded-xl border border-slate-800 bg-slate-900/60 px-3 py-2 text-sm text-slate-100 placeholder:text-slate-500 focus:border-sky-500 focus:outline-none focus:ring-2 focus:ring-sky-500/60" />
                    </div>
                  </div>
                  <div class="grid gap-3 sm:grid-cols-3">
                    <div class="space-y-2">
                      <label class="text-sm font-medium text-slate-200">TCP 限速 (-b，可选)</label>
                      <input id="suite-tcp-bandwidth" type="text" placeholder="例如 0 或 500M" class="rounded-xl border border-slate-800 bg-slate-900/60 px-3 py-2 text-sm text-slate-100 placeholder:text-slate-500 focus:border-sky-500 focus:outline-none focus:ring-2 focus:ring-sky-500/60" />
                    </div>
                    <div class="space-y-2">
                      <label class="text-sm font-medium text-slate-200">UDP 带宽 (-b)</label>
                      <input id="suite-udp-bandwidth" type="text" value="100M" class="rounded-xl border border-slate-800 bg-slate-900/60 px-3 py-2 text-sm text-slate-100 placeholder:text-slate-500 focus:border-sky-500 focus:outline-none focus:ring-2 focus:ring-sky-500/60" />
                    </div>
                    <div class="space-y-2">
                      <label class="text-sm font-medium text-slate-200">UDP 包长 (-l)</label>
                      <input id="suite-udp-len" type="number" value="1400" class="rounded-xl border border-slate-800 bg-slate-900/60 px-3 py-2 text-sm text-slate-100 placeholder:text-slate-500 focus:border-sky-500 focus:outline-none focus:ring-2 focus:ring-sky-500/60" />
                    </div>
                  </div>
                  <button id="run-suite-test" class="w-full rounded-xl bg-gradient-to-r from-emerald-500 to-cyan-500 px-4 py-3 text-sm font-semibold text-white shadow-lg shadow-emerald-500/20 transition hover:scale-[1.01] hover:shadow-xl">启动双向测试</button>
                </div>

                <div id="test-progress" class="hidden space-y-2">
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

  <!-- Settings Modal -->
  <div id="settings-modal" class="fixed inset-0 z-40 hidden items-center justify-center bg-slate-950/80 px-4 py-6 backdrop-blur">
    <div class="relative w-full max-w-2xl rounded-3xl border border-slate-800 bg-slate-900/80 p-6 shadow-2xl shadow-black/40">
      <button id="close-settings" onclick="toggleSettingsModal(false)" class="absolute right-4 top-4 rounded-full border border-slate-700/80 bg-slate-800/80 p-2 text-slate-300 transition hover:bg-slate-700/80">✕</button>
      
      <div class="mb-6 flex items-center justify-between gap-2">
        <div>
          <p class="text-xs uppercase tracking-[0.2em] text-indigo-300/80">系统管理</p>
          <h3 class="text-2xl font-semibold text-white">设置</h3>
        </div>
        <span class="rounded-full bg-indigo-500/10 px-3 py-1 text-xs font-semibold text-indigo-200 ring-1 ring-indigo-500/40">Settings</span>
      </div>

      <!-- Tab Navigation -->
      <div class="mb-6 inline-flex items-center gap-2 rounded-full border border-slate-700/70 bg-slate-900/70 p-1 shadow-inner shadow-black/20">
        <button id="password-tab" onclick="setActiveSettingsTab('password')" class="rounded-full bg-gradient-to-r from-indigo-500/80 to-purple-500/80 px-4 py-2 text-sm font-semibold text-slate-50 shadow-lg shadow-indigo-500/15 ring-1 ring-indigo-400/40 transition hover:brightness-110">
          🔐 密码管理
        </button>
        <button id="config-tab" onclick="setActiveSettingsTab('config')" class="rounded-full px-4 py-2 text-sm font-semibold text-slate-300 transition hover:text-white">
          📦 配置管理
        </button>
      </div>

      <!-- Password Management Panel -->
      <div id="password-panel" class="space-y-4">
        <div class="rounded-xl border border-slate-800/60 bg-slate-950/40 p-4">
          <h4 class="mb-3 text-lg font-semibold text-white">修改密码</h4>
          <p class="mb-4 text-sm text-slate-400">更新您的访问密码以保护系统安全。</p>
          
          <div id="change-password-alert" class="alert hidden mb-4"></div>
          
          <div class="grid gap-4 md:grid-cols-3">
            <div class="space-y-2">
              <label class="text-xs font-semibold text-slate-300" for="current-password">当前密码</label>
              <input id="current-password" type="password" class="form-input" placeholder="Current Password" />
            </div>
            <div class="space-y-2">
              <label class="text-xs font-semibold text-slate-300" for="new-password">新密码</label>
              <input id="new-password" type="password" class="form-input" placeholder="最少 6 位" />
            </div>
            <div class="space-y-2">
              <label class="text-xs font-semibold text-slate-300" for="confirm-password">确认新密码</label>
              <input id="confirm-password" type="password" class="form-input" placeholder="再次输入" />
            </div>
          </div>
          
          <div class="mt-4 flex justify-end">
            <button id="change-password-btn" onclick="changePassword()" class="rounded-xl bg-gradient-to-r from-indigo-500 to-purple-500 px-6 py-2.5 text-sm font-semibold text-white shadow-lg shadow-indigo-500/20 transition hover:scale-[1.02] hover:shadow-xl">
              更新密码
            </button>
          </div>
        </div>
      </div>

      <!-- Config Management Panel -->
      <div id="config-panel" class="hidden space-y-4">
        <div class="rounded-xl border border-slate-800/60 bg-slate-950/40 p-4">
          <h4 class="mb-3 text-lg font-semibold text-white">代理配置文件</h4>
          <p class="mb-4 text-sm text-slate-400">导入或导出 agent_configs.json，便于在不同实例之间迁移配置。</p>
          
          <div id="config-alert" class="hidden mb-4 rounded-xl border border-slate-700 bg-slate-800/60 px-4 py-3 text-sm text-slate-100"></div>
          
          <input id="config-file-input" type="file" accept="application/json" class="hidden" />
          
          <div class="flex flex-wrap items-center gap-3">
            <button id="export-configs" class="rounded-xl border border-slate-700 bg-slate-800/60 px-5 py-2.5 text-sm font-semibold text-slate-100 shadow-sm transition hover:border-sky-500 hover:text-sky-200 inline-flex items-center gap-2">
              <span>📤</span>
              <span>导出配置</span>
            </button>
            <button id="import-configs" class="rounded-xl border border-sky-500/40 bg-sky-500/15 px-5 py-2.5 text-sm font-semibold text-sky-100 shadow-sm transition hover:bg-sky-500/25 inline-flex items-center gap-2">
              <span>📥</span>
              <span>导入配置</span>
            </button>
          </div>
          
          <p class="mt-4 text-xs text-slate-500">💡 提示: 配置文件包含所有节点信息，可用于备份或迁移到其他服务器。</p>
        </div>
      </div>
    </div>
  </div>

    <script>
    console.log('Script loading...');
    const apiFetch = (url, options = {}) => fetch(url, { credentials: 'include', ...options });
    
    // Declare all elements in global scope (will be initialized in DOMContentLoaded)
    let loginForm, loginCard, appCard, loginAlert, loginButton, passwordInput;
    let loginStatus, loginStatusDot, loginStatusLabel, loginHint, authHint;
    let originalLoginLabel, configAlert, importConfigsBtn, exportConfigsBtn, configFileInput;
    let changePasswordAlert, currentPasswordInput, newPasswordInput, confirmPasswordInput, changePasswordBtn;

    // Initialize all elements when DOM is ready
    document.addEventListener('DOMContentLoaded', () => {
        console.log('DOM fully loaded. Initializing elements...');
        
        // Login elements
        loginForm = document.getElementById('login-form');
        loginCard = document.getElementById('login-card');
        appCard = document.getElementById('app-card');
        loginAlert = document.getElementById('login-alert');
        loginButton = document.getElementById('login-btn');
        passwordInput = document.getElementById('password-input');
        loginStatus = document.getElementById('login-status');
        loginStatusDot = document.getElementById('login-status-dot');
        loginStatusLabel = document.getElementById('login-status-label');
        loginHint = document.getElementById('login-hint');
        authHint = document.getElementById('auth-hint');
        originalLoginLabel = loginButton?.textContent || 'Login';
        
        // Config elements
        configAlert = document.getElementById('config-alert');
        importConfigsBtn = document.getElementById('import-configs');
        exportConfigsBtn = document.getElementById('export-configs');
        configFileInput = document.getElementById('config-file-input');
        
        // Password change elements
        changePasswordAlert = document.getElementById('change-password-alert');
        currentPasswordInput = document.getElementById('current-password');
        newPasswordInput = document.getElementById('new-password');
        confirmPasswordInput = document.getElementById('confirm-password');
        changePasswordBtn = document.getElementById('change-password-btn');
        
        console.log('Elements initialized. Login button:', loginButton);
        console.log('Password input:', passwordInput);
        
        // Attach event listeners
        if (loginButton) {
            loginButton.addEventListener('click', (e) => {
                e.preventDefault();
                console.log('Login button clicked via addEventListener');
                login();
            });
        }
        
        if (passwordInput) {
            passwordInput.addEventListener('keyup', (e) => {
                if (e.key === 'Enter') {
                    e.preventDefault();
                    console.log('Enter key pressed in password field');
                    login();
                }
            });
            passwordInput.focus();
        }
        
        // Run initial checks
        checkAuth();
    });

    // Other element references (not in DOMContentLoaded because they're not used in login flow)
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
    const omitInput = document.getElementById('omit');
    const protocolSelect = document.getElementById('protocol');
    const tcpBandwidthInput = document.getElementById('tcp-bandwidth');
    const udpBandwidthInput = document.getElementById('udp-bandwidth');
    const udpLenInput = document.getElementById('udp-len');
    const tcpOptions = document.getElementById('tcp-options');
    const udpOptions = document.getElementById('udp-options');
    const singleTestPanel = document.getElementById('single-test-panel');
    const suiteTestPanel = document.getElementById('suite-test-panel');
    const singleTestTab = document.getElementById('single-test-tab');
    const suiteTestTab = document.getElementById('suite-test-tab');
    const testPanelIntro = document.getElementById('test-panel-intro');
    const suiteSrcSelect = document.getElementById('suite-src-select');
    const suiteDstSelect = document.getElementById('suite-dst-select');
    const suiteDuration = document.getElementById('suite-duration');
    const suiteParallel = document.getElementById('suite-parallel');
    const suitePort = document.getElementById('suite-port');
    const suiteOmit = document.getElementById('suite-omit');
    const suiteTcpBandwidth = document.getElementById('suite-tcp-bandwidth');
    const suiteUdpBandwidth = document.getElementById('suite-udp-bandwidth');
    const suiteUdpLen = document.getElementById('suite-udp-len');
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
      { key: 'youtube', label: 'YouTube Premium', color: 'text-rose-300', bg: 'border-rose-500/30 bg-rose-500/10', indicator: 'bg-rose-400' },
      { key: 'prime_video', label: 'Prime Video', color: 'text-amber-300', bg: 'border-amber-400/40 bg-amber-500/10', indicator: 'bg-amber-400' },
      { key: 'netflix', label: 'Netflix', color: 'text-red-400', bg: 'border-red-500/40 bg-red-500/10', indicator: 'bg-red-400' },
      { key: 'disney_plus', label: 'Disney+', color: 'text-sky-300', bg: 'border-sky-500/40 bg-sky-500/10', indicator: 'bg-sky-400' },
      { key: 'hbo', label: 'HBO', color: 'text-purple-300', bg: 'border-purple-500/40 bg-purple-500/10', indicator: 'bg-purple-400' },
      { key: 'openai', label: 'OpenAI', color: 'text-emerald-300', bg: 'border-emerald-500/40 bg-emerald-500/10', indicator: 'bg-emerald-400' },
      { key: 'gemini', label: 'Gemini', color: 'text-sky-200', bg: 'border-sky-400/40 bg-sky-400/10', indicator: 'bg-sky-300' },
      // New services
      { key: 'tiktok', label: 'TikTok', color: 'text-pink-300', bg: 'border-pink-500/40 bg-pink-500/10', indicator: 'bg-pink-400' },
      { key: 'twitch', label: 'Twitch', color: 'text-violet-300', bg: 'border-violet-500/40 bg-violet-500/10', indicator: 'bg-violet-400' },
      { key: 'paramount_plus', label: 'Paramount+', color: 'text-blue-300', bg: 'border-blue-500/40 bg-blue-500/10', indicator: 'bg-blue-400' },
      { key: 'spotify', label: 'Spotify', color: 'text-green-300', bg: 'border-green-500/40 bg-green-500/10', indicator: 'bg-green-400' },
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

    function setLoginState(state, message) {
      if (!loginStatus) return;

      const presets = {
        idle: {
          text: '等待解锁',
          dot: 'warning',
          className: 'warning',
          hint: '输入共享密码以进入运维面板。',
        },
        unlocking: {
          text: '正在解锁...',
          dot: 'info',
          className: 'info',
          hint: '正在验证密码，请稍候。',
        },
        unlocked: {
          text: '已解锁',
          dot: 'success',
          className: 'success',
          hint: '已通过认证，可管理节点与测速任务。',
        },
        error: {
          text: '验证失败',
          dot: 'danger',
          className: 'danger',
          hint: '验证未通过，请重新输入。',
        },
      };

      const next = presets[state] || presets.idle;
      loginStatus.className = `status-chip ${next.className}`;
      if (loginStatusDot) {
        loginStatusDot.className = `status-dot ${next.dot}`;
      }
      if (loginStatusLabel) {
        loginStatusLabel.textContent = message || next.text;
      }
      if (loginHint) {
        loginHint.textContent = message || next.hint;
      }
    }

    function setLoginButtonLoading(isLoading) {
      if (!loginButton) return;
      loginButton.disabled = isLoading;
      loginButton.classList.toggle('opacity-70', isLoading);
      loginButton.classList.toggle('cursor-not-allowed', isLoading);
      
      if (isLoading) {
        loginButton.innerHTML = '<span class="inline-flex items-center justify-center gap-2">Logging in...</span>';
      } else {
        loginButton.textContent = 'Login';
      }
    }


    function toggleProtocolOptions() {
      const proto = (protocolSelect?.value || 'tcp').toLowerCase();
      if (proto === 'udp') {
        udpOptions?.classList.remove('hidden');
        tcpOptions?.classList.add('hidden');
      } else {
        tcpOptions?.classList.remove('hidden');
        udpOptions?.classList.add('hidden');
      }
    }

    function setActiveTestTab(mode) {
      const isSuite = mode === 'suite';
      if (singleTestPanel) singleTestPanel.classList.toggle('hidden', isSuite);
      if (suiteTestPanel) suiteTestPanel.classList.toggle('hidden', !isSuite);
      if (testProgress) testProgress.classList.add('hidden');

      if (singleTestTab) {
        singleTestTab.className = isSuite
          ? 'rounded-full px-4 py-1.5 text-xs font-semibold text-slate-300 transition hover:text-white'
          : 'rounded-full bg-gradient-to-r from-sky-500/80 to-indigo-500/80 px-4 py-1.5 text-xs font-semibold text-slate-50 shadow-lg shadow-sky-500/15 ring-1 ring-sky-400/40 transition hover:brightness-110';
      }
      if (suiteTestTab) {
        suiteTestTab.className = isSuite
          ? 'rounded-full bg-gradient-to-r from-emerald-500/80 to-cyan-500/80 px-4 py-1.5 text-xs font-semibold text-slate-50 shadow-lg shadow-emerald-500/15 ring-1 ring-emerald-400/40 transition hover:brightness-110'
          : 'rounded-full px-4 py-1.5 text-xs font-semibold text-slate-300 transition hover:text-white';
      }

      if (testPanelIntro) {
        testPanelIntro.textContent = isSuite
          ? '双向 TCP/UDP 测试一次完成四轮去回，方便基线和互联质量核验。'
          : '快速发起单条 TCP/UDP 链路测试，支持限速、并行与反向 (-R)。';
      }
    }

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

    // Settings Modal Functions
    const settingsModal = document.getElementById('settings-modal');
    const passwordPanel = document.getElementById('password-panel');
    const configPanel = document.getElementById('config-panel');
    const passwordTab = document.getElementById('password-tab');
    const configTab = document.getElementById('config-tab');

    function toggleSettingsModal(isOpen) {
      if (!settingsModal) return;
      if (isOpen) {
        settingsModal.classList.remove('hidden');
        settingsModal.classList.add('flex');
        setActiveSettingsTab('password'); // Default to password tab
      } else {
        settingsModal.classList.add('hidden');
        settingsModal.classList.remove('flex');
      }
    }

    function setActiveSettingsTab(tab) {
      const isPassword = tab === 'password';
      
      // Toggle panels
      if (passwordPanel) passwordPanel.classList.toggle('hidden', !isPassword);
      if (configPanel) configPanel.classList.toggle('hidden', isPassword);
      
      // Update tab styles
      if (passwordTab) {
        passwordTab.className = isPassword
          ? 'rounded-full bg-gradient-to-r from-indigo-500/80 to-purple-500/80 px-4 py-2 text-sm font-semibold text-slate-50 shadow-lg shadow-indigo-500/15 ring-1 ring-indigo-400/40 transition hover:brightness-110'
          : 'rounded-full px-4 py-2 text-sm font-semibold text-slate-300 transition hover:text-white';
      }
      if (configTab) {
        configTab.className = isPassword
          ? 'rounded-full px-4 py-2 text-sm font-semibold text-slate-300 transition hover:text-white'
          : 'rounded-full bg-gradient-to-r from-indigo-500/80 to-purple-500/80 px-4 py-2 text-sm font-semibold text-slate-50 shadow-lg shadow-indigo-500/15 ring-1 ring-indigo-400/40 transition hover:brightness-110';
      }
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
          region: svc.region,
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
        const res = await apiFetch(`/geo?ip=${encodeURIComponent(node.ip)}`);
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
          // For Netflix, tier takes precedence over unlocked flag
          const tier = status?.tier;
          let unlocked = null;
          if (svc.key === 'netflix' && tier) {
            // Netflix tier-based unlock status
            unlocked = tier === 'full' ? true : (tier === 'originals' ? false : null);
          } else {
            // Other services use unlocked flag or tier
            unlocked = status ? (status.unlocked ?? (tier === 'full')) : null;
          }
          
          const detail = status && status.detail ? status.detail.replace(/"/g, "'") : '';
          const region = status?.region;
          const tags = [];

          let badgeColor = unlocked === true ? `${svc.color} ${svc.bg}` : mutedStyle;
          let statusLabel = unlocked === true ? '可解锁' : unlocked === false ? '未解锁' : '未检测';
          let badgeLabel = svc.label;

          if (svc.key === 'netflix' && status) {
            const netflixTier = tier || (unlocked ? 'full' : 'none');
            if (netflixTier === 'full') {
              statusLabel = '全解锁';
              badgeColor = `${svc.color} ${svc.bg}`;
              tags.push('全解锁');
              unlocked = true;  // Ensure unlocked is true for full tier
            } else if (netflixTier === 'originals') {
              statusLabel = '仅解锁自制剧';
              badgeColor = mutedStyle;
              tags.push('自制剧');
              unlocked = false;  // Originals-only is not considered fully unlocked
            } else {
              statusLabel = '未解锁';
              badgeColor = mutedStyle;
              unlocked = false;
            }
          }

          const regionColor = unlocked === true ? svc.color : mutedStyle;
          const regionTag = region ? `<span class=\"rounded-sm px-1 text-[10px] font-bold ${regionColor}\">[${region}]</span>` : '';
          const tagBadges = tags
            .filter(Boolean)
            .map((tag) => `<span class=\"rounded-sm bg-slate-800/60 px-1 text-[10px]\">[${tag}]</span>`)
            .join('');

          const title = `${region ? `[${region}]` : ''}${badgeLabel}：${statusLabel}${detail ? ' · ' + detail : ''}`;
          return `<span class=\"inline-flex items-center gap-1 rounded-full border px-2 py-1 text-[10px] font-semibold ${badgeColor}\" title=\"${title}\">${regionTag}<span>${badgeLabel}</span>${tagBadges}</span>`;
        })
        .join('');
    }

    async function exportAgentConfigs() {
      clearAlert(configAlert);
      const res = await apiFetch('/agent-configs/export');
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

      const res = await apiFetch('/agent-configs/import', {
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

      const res = await apiFetch(`/nodes/${nodeId}`, { method: 'DELETE' });
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

    async function checkAuth(showFeedback = false) {
      try {
        const res = await apiFetch('/auth/status');
        if (!res.ok) {
          let message = '无法验证登录状态。';
          try {
            const data = await res.json();
            if (data?.detail) message = `认证失败：${data.detail}`;
          } catch (_) {
            try {
              const rawText = await res.text();
              if (rawText) message = `认证失败：${rawText}`;
            } catch (_) {}
          }

          setLoginState('error', message);
          if (showFeedback) setAlert(loginAlert, message);
          return false;
        }

        const data = await res.json();
        if (data.authenticated) {
          loginCard.classList.add('hidden');
          appCard.classList.remove('hidden');
          setLoginState('unlocked');
          authHint.textContent = '已通过认证，可管理节点与测速任务。';
          await refreshNodes();
          await refreshTests();
          return true;
        } else {
          appCard.classList.add('hidden');
          loginCard.classList.remove('hidden');
          setLoginState('idle');
          if (showFeedback) setAlert(loginAlert, '登录状态未建立，请重新登录。');
          return false;
        }
      } catch (err) {
        console.error('Auth check failed:', err);
        appCard.classList.add('hidden');
        loginCard.classList.remove('hidden');
        const errorMessage = '无法连接认证服务，请稍后重试。';
        setLoginState('error', errorMessage);
        if (showFeedback) setAlert(loginAlert, errorMessage);
        return false;
      }
    }

    async function login() {
      console.log('Starting login process...');
      clearAlert(loginAlert);
      
      // Reset animations
      const card = document.querySelector('.login-card');
      card.classList.remove('animate-shake', 'animate-success');
      
      const password = (passwordInput?.value || '').trim();
      if (!password) {
        console.warn('Login aborted: empty password');
        setAlert(loginAlert, '请输入密码 (Password Required)');
        passwordInput?.focus();
        card.classList.add('animate-shake');
        setTimeout(() => card.classList.remove('animate-shake'), 400);
        return;
      }

      setLoginButtonLoading(true);
      
      const controller = new AbortController();
      const timeoutId = setTimeout(() => controller.abort(), 8000); // 8s timeout

      try {
        console.log('Sending login request to /auth/login...');
        const res = await apiFetch('/auth/login', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ password }),
          signal: controller.signal
        });
        clearTimeout(timeoutId);

        console.log(`Login response status: ${res.status}`);

        if (res.ok) {
           console.log('Login success. Verifying session...');
           loginAlert.className = 'alert alert-success';
           setAlert(loginAlert, '登录成功 (Success)');
           card.classList.add('animate-success');
           
           // Hide login card immediately to prevent flash
           loginCard.style.opacity = '0.5';
           loginCard.style.pointerEvents = 'none';
           
           // Allow more time for the cookie to be processed/saved by the browser
           setTimeout(async () => {
             const authed = await checkAuth(true);
             console.log(`Session check result: ${authed}`);
             if (!authed) {
                console.error('Login successful but session check failed.');
                loginAlert.className = 'alert alert-error';
                setAlert(loginAlert, '会话建立失败 (Session Failed) - Cookie Blocked?');
                card.classList.remove('animate-success');
                card.classList.add('animate-shake');
                loginCard.style.opacity = '1';
                loginCard.style.pointerEvents = 'auto';
             }
           }, 1200);
           return;
        }

        // Handle HTTP errors
        card.classList.add('animate-shake');
        setTimeout(() => card.classList.remove('animate-shake'), 400);
        
        loginAlert.className = 'alert alert-error';
        let message = '登录失败 (Login Failed)';
        
        if (res.status === 401) {
            console.warn('Login failed: 401 Unauthorized');
            message = '登录失败：密码错误 (Invalid Password)';
        } else if (res.status === 408 || res.status === 504) {
             console.error('Login failed: Timeout');
             message = '登录超时 (Request Timeout)';
        } else {
            try {
                const data = await res.json();
                console.warn('Login failed with details:', data);
                if (data?.detail === 'empty_password') message = '密码不能为空';
                else if (data?.detail === 'invalid_password') message = '登录失败：密码错误 (Invalid Password)';
                else if (data?.detail) message = `登录失败：${data.detail}`;
            } catch (e) {
                console.error('Failed to parse error response:', e);
                message = `登录失败 (HTTP ${res.status})`;
            }
        }
        setAlert(loginAlert, message);

      } catch (err) {
        clearTimeout(timeoutId);
        console.error('Login network exception:', err);
        
        card.classList.add('animate-shake');
        setTimeout(() => card.classList.remove('animate-shake'), 400);
        
        loginAlert.className = 'alert alert-error';
        const errorMsg = err.name === 'AbortError' ? '请求超时 (Timeout)' : '无法连接服务器 (Network Error)';
        setAlert(loginAlert, errorMsg);
      } finally {
        setLoginButtonLoading(false);
      }
    }

    async function logout() {
      await apiFetch('/auth/logout', { method: 'POST' });
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

      const res = await apiFetch('/auth/change', {
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

      changePasswordAlert.className = 'alert alert-success';
      setAlert(changePasswordAlert, '✅ 密码已成功更新!当前会话已使用新密码。');
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

    // Sync whitelist to all agents
    async function syncWhitelist() {{
      const btn = document.getElementById('sync-whitelist-btn');
      const resultDiv = document.getElementById('whitelist-sync-result');
      const statusText = document.getElementById('whitelist-status-text');
      
      btn.disabled = true;
      btn.textContent = '同步中...';
      statusText.textContent = '● 正在同步...';
      statusText.className = 'text-yellow-400';
      resultDiv.classList.add('hidden');
      
      try {{
        const res = await apiFetch('/admin/sync_whitelist', {{ method: 'POST' }});
        const data = await res.json();
        
        if (data.status === 'ok') {{
          statusText.textContent = '● 同步成功';
          statusText.className = 'text-emerald-400';
          
          const results = data.results;
          resultDiv.innerHTML = `
            <div class="space-y-2">
              <div class="font-semibold text-emerald-400">✓ 同步完成</div>
              <div class="text-slate-300">成功: ${{results.success}}/${{results.total_agents}} 个 Agent</div>
              ${{results.failed > 0 ? `<div class="text-rose-400">失败: ${{results.failed}} 个 Agent<div class="mt-1 text-xs text-slate-400">${{results.errors.join('<br>')}}</div></div>` : ''}}
            </div>
          `;
          resultDiv.classList.remove('hidden');
          setTimeout(() => {{ resultDiv.classList.add('hidden'); }}, 10000);
        }} else {{
          throw new Error(data.error || '同步失败');
        }}
      }} catch (err) {{
        statusText.textContent = '● 同步失败';
        statusText.className = 'text-rose-400';
        resultDiv.innerHTML = `<div class="text-rose-400">✗ 同步失败: ${{err.message}}</div>`;
        resultDiv.classList.remove('hidden');
      }} finally {{
        btn.disabled = false;
        btn.textContent = '同步白名单到所有 Agent';
        setTimeout(() => {{
          statusText.textContent = '● 自动同步已启用';
          statusText.className = 'text-emerald-400';
        }}, 5000);
      }}
    }}

    function syncSuitePort() {
      const dst = nodeCache.find((n) => n.id === Number(suiteDstSelect?.value));
      if (dst && suitePort) {
        const detected = dst.detected_iperf_port || dst.iperf_port;
        suitePort.value = detected || DEFAULT_IPERF_PORT;
      }
    }


    async function refreshNodes() {
      if (isRefreshingNodes) return;
      isRefreshingNodes = true;
      try {
        const previousSrc = Number(srcSelect.value) || null;
        const previousDst = Number(dstSelect.value) || null;
        const previousSuiteSrc = Number(suiteSrcSelect?.value) || null;
        const previousSuiteDst = Number(suiteDstSelect?.value) || null;
        const res = await apiFetch('/nodes/status');
        const nodes = await res.json();
        nodeCache = nodes;
        nodesList.innerHTML = '';
        srcSelect.innerHTML = '';
        dstSelect.innerHTML = '';
        if (suiteSrcSelect) suiteSrcSelect.innerHTML = '';
        if (suiteDstSelect) suiteDstSelect.innerHTML = '';

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
          const agentPort = node.detected_agent_port || node.agent_port;
          const agentPortDisplay = maskPort(agentPort, privacyEnabled);
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
            const agentPort = node.detected_agent_port || node.agent_port;
            agentPortSpan.textContent = `:${maskPort(agentPort, nextState)}`;
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

        if (suiteSrcSelect && suiteDstSelect) {
          const suiteOptionA = optionA.cloneNode(true);
          const suiteOptionB = optionA.cloneNode(true);
          suiteSrcSelect.appendChild(suiteOptionA);
          suiteDstSelect.appendChild(suiteOptionB);
        }
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

      if (suiteSrcSelect) {
        if (previousSuiteSrc && nodes.some((n) => n.id === previousSuiteSrc)) {
          suiteSrcSelect.value = String(previousSuiteSrc);
        } else if (firstNodeId) {
          suiteSrcSelect.value = String(firstNodeId);
        }
      }

      if (suiteDstSelect) {
        if (previousSuiteDst && nodes.some((n) => n.id === previousSuiteDst)) {
          suiteDstSelect.value = String(previousSuiteDst);
        } else if (firstNodeId) {
          suiteDstSelect.value = String(firstNodeId);
        }
      }

      syncTestPort();
      syncSuitePort();
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
          const res = await apiFetch(`/nodes/${nodeId}/streaming-test`, { method: 'POST' });
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
                region: svc.region,
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
      const res = await apiFetch(`/nodes/${nodeId}`, {
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
      const res = await apiFetch('/tests');
      const tests = await res.json();
      if (!tests.length) {
        testsList.textContent = '暂无测试记录。';
        return;
      }
      testsList.innerHTML = '';

      const detailBlocks = new Map();
      const enrichedTests = tests.slice().reverse().map((test) => {
        const metrics = summarizeTestMetrics(test.raw_result || {});
        if (metrics?.isSuite) {
          const suiteEntries = normalizeSuiteEntries(test);
          return { test, metrics, suiteEntries };
        }
        const rateSummary = summarizeRateTable(test.raw_result || {});
        const latencyValue = metrics.latencyStats?.avg ?? (metrics.latencyMs ?? null);
        const jitterValue = metrics.jitterStats?.avg ?? (metrics.jitterMs ?? null);
        return { test, metrics, rateSummary, latencyValue, jitterValue };
      });

      const maxRate = Math.max(
        1,
        ...enrichedTests
          .filter((item) => !item.metrics?.isSuite)
          .map(({ rateSummary }) => Math.max(rateSummary.receiverRateValue || 0, rateSummary.senderRateValue || 0))
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

      enrichedTests.forEach(({ test, metrics, rateSummary, latencyValue, jitterValue, suiteEntries }) => {
        const pathLabel = `${formatNodeLabel(test.src_node_id)} → ${formatNodeLabel(test.dst_node_id)}`;

        if (metrics?.isSuite) {
          const card = document.createElement('div');
          card.className = 'group space-y-3 rounded-2xl border border-slate-800/70 bg-slate-900/60 p-4 shadow-sm shadow-black/30 transition hover:border-emerald-400/40 hover:shadow-emerald-500/10';

          const header = document.createElement('div');
          header.className = 'flex flex-wrap items-center justify-between gap-2';
          const title = document.createElement('div');
          title.innerHTML = `<p class="text-xs uppercase tracking-[0.2em] text-emerald-300/70">#${test.id} · TCP/UDP 双向测试</p>` +
            `<p class="text-lg font-semibold text-white">${pathLabel}</p>`;
          header.appendChild(title);

          const hasError = suiteEntries.some((entry) => entry.rateSummary?.status && entry.rateSummary.status !== 'ok');
          const statusPill = document.createElement('span');
          statusPill.className = 'inline-flex items-center gap-2 rounded-full bg-slate-800/70 px-3 py-1 text-xs font-semibold text-slate-200 ring-1 ring-slate-700';
          statusPill.textContent = hasError ? '部分异常' : '完成';
          header.appendChild(statusPill);
          card.appendChild(header);

          const suiteGrid = document.createElement('div');
          suiteGrid.className = 'grid gap-3 md:grid-cols-2';
          suiteEntries.forEach((entry) => {
            const tile = document.createElement('div');
            tile.className = 'space-y-2 rounded-xl border border-slate-800/60 bg-slate-950/40 p-3';
            const heading = document.createElement('div');
            heading.className = 'flex items-center justify-between text-sm text-slate-200';

            const labelGroup = document.createElement('div');
            labelGroup.className = 'flex items-center gap-2';
            const labelText = document.createElement('span');
            labelText.className = 'font-semibold';
            labelText.textContent = entry.label;
            labelGroup.appendChild(labelText);

            const badgeRow = document.createElement('div');
            badgeRow.className = 'flex items-center gap-1';
            const latencyValue = entry.metrics?.latencyStats?.avg ?? entry.metrics?.latencyMs;
            if (latencyValue !== undefined && latencyValue !== null) {
              badgeRow.appendChild(createMiniStat('RTT', formatMetric(latencyValue, 2), 'ms', 'text-sky-200', entry.metrics?.latencyStats));
            }
            const jitterValue = entry.metrics?.jitterStats?.avg ?? entry.metrics?.jitterMs;
            if (jitterValue !== undefined && jitterValue !== null) {
              badgeRow.appendChild(createMiniStat('抖动', formatMetric(jitterValue, 2), 'ms', 'text-amber-200', entry.metrics?.jitterStats));
            }
            const lossValue = entry.metrics?.lossStats?.avg ?? entry.metrics?.lostPercent;
            if (lossValue !== undefined && lossValue !== null) {
              badgeRow.appendChild(createMiniStat('丢包', formatMetric(lossValue, 2), '%', 'text-rose-200', entry.metrics?.lossStats));
            }
            const retransValue = entry.metrics?.retransStats?.avg;
            if (retransValue !== undefined && retransValue !== null) {
              badgeRow.appendChild(createMiniStat('重传', formatMetric(retransValue, 0), '次', 'text-indigo-200', entry.metrics?.retransStats));
            }
            if (badgeRow.childNodes.length) {
              labelGroup.appendChild(badgeRow);
            }

            const protoLabel = document.createElement('span');
            protoLabel.className = 'text-[11px] uppercase text-slate-400';
            protoLabel.textContent = `${entry.protocol.toUpperCase()}${entry.reverse ? ' (-R)' : ''}`;

            heading.appendChild(labelGroup);
            heading.appendChild(protoLabel);
            tile.appendChild(heading);

            const rates = document.createElement('div');
            rates.className = 'grid grid-cols-2 gap-2 text-xs text-slate-400';
            rates.innerHTML = `
              <div class="rounded-lg border border-slate-800/60 bg-slate-900/60 p-2">
                <div class="flex items-center justify-between"><span>接收</span><span class="font-semibold text-emerald-200">${entry.rateSummary.receiverRateMbps}</span></div>
              </div>
              <div class="rounded-lg border border-slate-800/60 bg-slate-900/60 p-2">
                <div class="flex items-center justify-between"><span>发送</span><span class="font-semibold text-amber-200">${entry.rateSummary.senderRateMbps}</span></div>
              </div>`;
            tile.appendChild(rates);
            suiteGrid.appendChild(tile);
          });
          card.appendChild(suiteGrid);

          const actions = document.createElement('div');
          actions.className = 'flex flex-wrap items-center justify-between gap-3';
          const buttons = document.createElement('div');
          buttons.className = 'flex flex-wrap gap-2 translate-y-1 opacity-0 transition duration-200 pointer-events-none group-hover:translate-y-0 group-hover:opacity-100 group-hover:pointer-events-auto';
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
          actions.appendChild(buttons);
          card.appendChild(actions);

          const block = buildSuiteDetailsBlock(test, suiteEntries, pathLabel);
          detailBlocks.set(test.id, block);
          testsList.appendChild(card);
          testsList.appendChild(block);
          return;
        }

        const typeLabel = `${test.protocol.toUpperCase()}${test.params?.reverse ? ' (-R)' : ''}`;

        const card = document.createElement('div');
        card.className = 'group space-y-3 rounded-2xl border border-slate-800/70 bg-slate-900/60 p-4 shadow-sm shadow-black/30 transition hover:border-sky-400/40 hover:shadow-sky-500/10';

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

        const quickStats = document.createElement('div');
        quickStats.className = 'flex flex-wrap items-center gap-2 text-xs';
        if (latencyValue !== undefined && latencyValue !== null) {
          quickStats.appendChild(createMiniStat('RTT', formatMetric(latencyValue, 2), 'ms', 'text-sky-200', metrics.latencyStats));
        }
        if (jitterValue !== undefined && jitterValue !== null) {
          quickStats.appendChild(createMiniStat('抖动', formatMetric(jitterValue, 2), 'ms', 'text-amber-200', metrics.jitterStats));
        }
        const lossValue = metrics.lossStats?.avg ?? metrics.lostPercent;
        if (lossValue !== undefined && lossValue !== null) {
          quickStats.appendChild(createMiniStat('丢包', formatMetric(lossValue, 2), '%', 'text-rose-200', metrics.lossStats));
        }
        const retransValue = metrics.retransStats?.avg;
        if (retransValue !== undefined && retransValue !== null) {
          quickStats.appendChild(createMiniStat('重传', formatMetric(retransValue, 2), '次', 'text-indigo-200', metrics.retransStats));
        }
        if (quickStats.childNodes.length) {
          card.appendChild(quickStats);
        }

        const ratesGrid = document.createElement('div');
        ratesGrid.className = 'grid gap-3 sm:grid-cols-2';
        ratesGrid.appendChild(buildRateRow('接收速率 (Mbps)', rateSummary.receiverRateValue, rateSummary.receiverRateMbps, 'from-emerald-400 to-sky-500'));
        ratesGrid.appendChild(buildRateRow('发送速率 (Mbps)', rateSummary.senderRateValue, rateSummary.senderRateMbps, 'from-amber-400 to-rose-500'));
        card.appendChild(ratesGrid);

        const metaChips = document.createElement('div');
        metaChips.className = 'flex flex-wrap items-center gap-2 text-xs text-slate-400';
        metaChips.appendChild(makeChip(test.protocol.toLowerCase() === 'udp' ? 'UDP 测试' : 'TCP 测试'));
        if (test.params?.reverse) metaChips.appendChild(makeChip('反向 (-R)'));
        card.appendChild(metaChips);

        const actions = document.createElement('div');
        actions.className = 'flex flex-wrap items-center justify-between gap-3';

        const buttons = document.createElement('div');
        buttons.className = 'flex flex-wrap gap-2 translate-y-1 opacity-0 transition duration-200 pointer-events-none group-hover:translate-y-0 group-hover:opacity-100 group-hover:pointer-events-auto';
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
      const res = await apiFetch(`/tests/${testId}`, { method: 'DELETE' });
      if (!res.ok) {
        setAlert(testAlert, '删除记录失败。');
        return;
      }
      await refreshTests();
    }

    async function clearAllTests() {
      clearAlert(testAlert);
      const res = await apiFetch('/tests', { method: 'DELETE' });
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

      const res = await apiFetch(url, {
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
        protocol: protocolSelect.value,
        duration: Number(document.getElementById('duration').value),
        parallel: Number(document.getElementById('parallel').value),
        port: Number(testPortInput.value || (selectedDst ? (selectedDst.detected_iperf_port || selectedDst.iperf_port) : DEFAULT_IPERF_PORT)),
        reverse: reverseToggle?.checked || false,
      };

      const omitValue = Number(omitInput.value || 0);
      if (omitValue > 0) payload.omit = omitValue;

      if (payload.protocol === 'tcp') {
        const tcpBw = tcpBandwidthInput.value.trim();
        if (tcpBw) payload.bandwidth = tcpBw;
      } else {
        const udpBw = udpBandwidthInput.value.trim();
        if (udpBw) payload.bandwidth = udpBw;
        const udpLen = Number(udpLenInput.value || 0);
        if (udpLen > 0) payload.datagram_size = udpLen;
      }

      const finishProgress = startProgressBar(
        testProgress,
        testProgressBar,
        testProgressLabel,
        payload.duration * 1000 + 1500,
        '开始链路测试...'
      );

      const res = await apiFetch('/tests', {
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

    async function runSuiteTest() {
      clearAlert(testAlert);
      const selectedDst = nodeCache.find((n) => n.id === Number(suiteDstSelect.value));

      const payload = {
        src_node_id: Number(suiteSrcSelect.value),
        dst_node_id: Number(suiteDstSelect.value),
        duration: Number(suiteDuration.value || 10),
        parallel: Number(suiteParallel.value || 1),
        port: Number(suitePort.value || (selectedDst ? (selectedDst.detected_iperf_port || selectedDst.iperf_port) : DEFAULT_IPERF_PORT)),
      };

      const omitValue = Number(suiteOmit.value || 0);
      if (omitValue > 0) payload.omit = omitValue;

      const tcpBw = suiteTcpBandwidth.value.trim();
      if (tcpBw) payload.tcp_bandwidth = tcpBw;
      const udpBw = suiteUdpBandwidth.value.trim();
      if (udpBw) payload.udp_bandwidth = udpBw;
      const udpLen = Number(suiteUdpLen.value || 0);
      if (udpLen > 0) payload.udp_datagram_size = udpLen;

      const expectedMs = payload.duration * 4000 + 3000;
      const finishProgress = startProgressBar(
        testProgress,
        testProgressBar,
        testProgressLabel,
        expectedMs,
        '准备执行 4 轮双向测试...'
      );

      const res = await apiFetch('/tests/suite', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });

      if (!res.ok) {
        const details = await res.text();
        const message = details ? `启动双向测试失败：${details}` : '启动双向测试失败，请确认节点存在且参数有效。';
        setAlert(testAlert, message);
        finishProgress('测试失败');
        return;
      }

      await refreshTests();
      finishProgress('双向测试完成');
      clearAlert(testAlert);
    }

    function normalizeLatency(value) {
      const num = Number(value);
      if (!Number.isFinite(num)) return null;
      return num > 1000 ? num / 1000 : num;
    }

    function computeStats(values) {
      const filtered = values.filter((v) => Number.isFinite(v));
      if (!filtered.length) return null;
      const max = Math.max(...filtered);
      const min = Math.min(...filtered);
      const avg = filtered.reduce((sum, val) => sum + val, 0) / filtered.length;
      return { min, max, avg };
    }

    function createMiniStat(label, value, unit = '', accent = 'text-sky-200', stats = null) {
      const wrap = document.createElement('div');
      wrap.className = 'relative inline-block';

      const badge = document.createElement('div');
      badge.className = 'inline-flex items-center gap-1 rounded-lg border border-slate-800/80 bg-slate-900/70 px-2 py-1 text-[11px] font-semibold text-slate-200';
      const unitSpan = unit ? `<span class="text-slate-500">${unit}</span>` : '';
      badge.innerHTML = `<span class="text-slate-400">${label}</span><span class="${accent}">${value}</span>${unitSpan}`;
      wrap.appendChild(badge);

      if (stats) {
        const detail = document.createElement('div');
        detail.className = 'pointer-events-none absolute left-1/2 top-full z-20 mt-2 w-max min-w-[180px] -translate-x-1/2 scale-95 rounded-lg border border-slate-800/80 bg-slate-900/95 px-3 py-2 text-[11px] text-slate-200 opacity-0 shadow-2xl shadow-black/30 transition duration-150';
        const primary = stats.avg ?? stats.mean ?? stats.max ?? stats.min;
        const unitLabel = unit ? ` ${unit}` : '';
        detail.innerHTML = `
          <div class="text-[11px] font-semibold text-slate-300">${label} 均值${unit ? ` (${unit})` : ''}</div>
          <div class="mt-1 text-sm font-bold text-white">${formatMetric(primary)}${unitLabel}</div>
          <div class="mt-1 text-[10px] text-slate-500">max ${formatMetric(stats.max)}${unitLabel} · min ${formatMetric(stats.min)}${unitLabel}</div>
        `;
        wrap.appendChild(detail);

        wrap.onmouseenter = () => {
          detail.classList.remove('opacity-0', 'scale-95');
          detail.classList.add('opacity-100', 'scale-100');
        };
        wrap.onmouseleave = () => {
          detail.classList.add('opacity-0', 'scale-95');
          detail.classList.remove('opacity-100', 'scale-100');
        };
      }

      return wrap;
    }

    function collectMetricStats(raw) {
      const jitterValues = [];
      const lossValues = [];
      const latencyValues = [];
      const retransValues = [];

      const pushNumber = (arr, value, normalizer = (v) => v) => {
        const normalized = normalizer(value);
        if (Number.isFinite(normalized)) arr.push(normalized);
      };

      const consumeResult = (result) => {
        if (!result) return;
        const intervals = Array.isArray(result.intervals) ? result.intervals : [];
        const end = result.end || {};
        const streams = Array.isArray(end.streams) ? end.streams : [];
        const sumReceived = end.sum_received || end.sum || {};
        const sumSent = end.sum_sent || end.sum || {};

        const appendStreamMetrics = (stream) => {
          if (!stream) return;
          const sender = stream.sender || stream.sum_sent || stream;
          const receiver = stream.receiver || stream.sum_received || stream;
          [sender, receiver].forEach((endpoint) => {
            if (!endpoint) return;
            pushNumber(latencyValues, endpoint.rtt, normalizeLatency);
            pushNumber(latencyValues, endpoint.mean_rtt, normalizeLatency);
            pushNumber(latencyValues, endpoint.max_rtt, normalizeLatency);
            pushNumber(latencyValues, endpoint.min_rtt, normalizeLatency);
            pushNumber(jitterValues, endpoint.jitter_ms, Number);
            pushNumber(retransValues, endpoint.retransmits, Number);
            if (endpoint.lost_percent !== undefined) pushNumber(lossValues, endpoint.lost_percent, Number);
            if (endpoint.lost_packets !== undefined && endpoint.packets) {
              pushNumber(lossValues, (endpoint.lost_packets / endpoint.packets) * 100, Number);
            }
          });
        };

        intervals.forEach((interval) => {
          const sum = interval?.sum || {};
          pushNumber(jitterValues, sum.jitter_ms, Number);
          if (sum.lost_percent !== undefined) pushNumber(lossValues, sum.lost_percent, Number);
          if (sum.lost_packets !== undefined && sum.packets) {
            pushNumber(lossValues, (sum.lost_packets / sum.packets) * 100, Number);
          }
          const streamsInInterval = Array.isArray(interval?.streams) ? interval.streams : [];
          streamsInInterval.forEach(appendStreamMetrics);
        });

        appendStreamMetrics(streams[0]);
        pushNumber(jitterValues, sumReceived.jitter_ms, Number);
        pushNumber(jitterValues, sumSent.jitter_ms, Number);
        if (sumReceived.lost_percent !== undefined) pushNumber(lossValues, sumReceived.lost_percent, Number);
        if (sumSent.lost_percent !== undefined) pushNumber(lossValues, sumSent.lost_percent, Number);
        if (sumReceived.lost_packets !== undefined && sumReceived.packets) {
          pushNumber(lossValues, (sumReceived.lost_packets / sumReceived.packets) * 100, Number);
        }
      };

      const baseResult = (raw && raw.iperf_result) || raw || {};
      const extraServerResult = baseResult?.server_output_json;
      [baseResult, extraServerResult].forEach(consumeResult);

      return {
        latency: computeStats(latencyValues),
        jitter: computeStats(jitterValues),
        loss: computeStats(lossValues),
        retrans: computeStats(retransValues),
      };
    }

    function summarizeSingleMetrics(raw) {
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

      const stats = collectMetricStats(raw);

      const bitsPerSecond = pickFirst(
        sumReceived?.bits_per_second,
        receiverStream?.bits_per_second,
        sumSent?.bits_per_second,
        senderStream?.bits_per_second,
      );

      const jitterMs = stats?.jitter?.avg ?? pickFirst(
        sumReceived?.jitter_ms,
        sumSent?.jitter_ms,
        receiverStream?.jitter_ms,
        senderStream?.jitter_ms,
      );

      const lostPercent = stats?.loss?.avg ?? pickFirst(
        sumReceived?.lost_percent,
        lossFromPackets,
        sumSent?.lost_percent,
        receiverStream?.lost_percent,
        senderStream?.lost_percent,
      );

      let latencyMs = stats?.latency?.avg ?? pickFirst(
        senderStream?.mean_rtt,
        senderStream?.rtt,
        receiverStream?.mean_rtt,
        receiverStream?.rtt,
      );
      if (latencyMs !== undefined && latencyMs !== null && latencyMs > 1000) {
        latencyMs = latencyMs / 1000;
      }

      return {
        bitsPerSecond,
        jitterMs,
        lostPercent,
        latencyMs,
        jitterStats: stats?.jitter || null,
        lossStats: stats?.loss || null,
        latencyStats: stats?.latency || null,
        retransStats: stats?.retrans || null,
      };
    }

    function summarizeTestMetrics(raw) {
      if (raw?.mode === 'suite' && Array.isArray(raw.tests)) {
        const entries = raw.tests.map((entry) => {
          const detailed = entry.raw || entry;
          const summary = entry.summary || {};
          const merged = { ...summary, ...detailed };
          if (!merged.server_output_json && detailed.server_output_json) {
            merged.server_output_json = detailed.server_output_json;
          }

          return {
            label: entry.label || '子测试',
            protocol: entry.protocol || 'tcp',
            reverse: !!entry.reverse,
            metrics: summarizeSingleMetrics(merged),
            raw: detailed,
          };
        });
        const valid = entries.map((e) => e.metrics).filter(Boolean);
        const avgBits = valid.length
          ? valid.reduce((sum, item) => sum + (item.bitsPerSecond || 0), 0) / valid.length
          : null;
        return { isSuite: true, entries, bitsPerSecond: avgBits };
      }
      return summarizeSingleMetrics(raw);
    }

    function summarizeSingleRateTable(raw) {
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

    function summarizeRateTable(raw) {
      if (raw?.mode === 'suite' && Array.isArray(raw.tests)) {
        return {
          mode: 'suite',
          tests: raw.tests.map((entry) => ({
            label: entry.label || '子测试',
            protocol: entry.protocol || 'tcp',
            reverse: !!entry.reverse,
            summary: summarizeSingleRateTable(entry.raw || entry),
          })),
        };
      }
      return summarizeSingleRateTable(raw);
    }

    function normalizeSuiteEntries(test) {
      const raw = test.raw_result || {};
      const metrics = summarizeTestMetrics(raw);
      const rateInfo = summarizeRateTable(raw);
      const rateMap = new Map();
      (rateInfo.tests || []).forEach((entry) => {
        rateMap.set(entry.label, entry.summary);
      });

      return (metrics.entries || []).map((entry, idx) => {
        const key = entry.label || `子测试 ${idx + 1}`;
        return {
          label: key,
          protocol: entry.protocol,
          reverse: entry.reverse,
          metrics: entry.metrics,
          rateSummary: rateMap.get(key) || summarizeSingleRateTable(entry.raw || entry),
          raw: entry.raw,
        };
      });
    }

    function formatMetric(value, decimals = 2) {
      if (value === undefined || value === null || Number.isNaN(value)) return 'N/A';
      return Number(value).toFixed(decimals);
    }

    function renderMetricStat(label, stats, unit = '') {
      if (!stats) return null;
      const unitLabel = unit ? ` ${unit}` : '';
      const primary = stats.avg ?? stats.mean ?? stats.max ?? stats.min;
      const wrap = document.createElement('div');
      wrap.className = 'border border-slate-800 bg-slate-950/70 p-3 text-xs text-slate-300 shadow-inner shadow-black/10';
      wrap.innerHTML = `
        <div class="flex items-center justify-between">
          <span class="font-medium">${label}</span>
          <span class="text-sm font-semibold text-slate-50">${formatMetric(primary)}${unitLabel}</span>
        </div>
        <div class="mt-1 text-[10px] text-slate-500">max ${formatMetric(stats.max)}${unitLabel} · min ${formatMetric(stats.min)}${unitLabel}</div>
      `;
      return wrap;
    }

    function buildMetricGrid(metrics) {
      if (!metrics) return null;
      const grid = document.createElement('div');
      grid.className = 'grid gap-2 sm:grid-cols-2 lg:grid-cols-4';

      [
        renderMetricStat('RTT 均值 (ms)', metrics.latencyStats, 'ms'),
        renderMetricStat('抖动均值 (ms)', metrics.jitterStats, 'ms'),
        renderMetricStat('丢包均值 (%)', metrics.lossStats, '%'),
        renderMetricStat('重传次数', metrics.retransStats, '次'),
      ]
        .filter(Boolean)
        .forEach((node) => grid.appendChild(node));

      return grid.childNodes.length ? grid : null;
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

    function buildSuiteDetailsBlock(test, suiteEntries, pathLabel) {
      const block = document.createElement('div');
      block.className = 'hidden rounded-xl border border-slate-800/60 bg-slate-900/60 p-3 shadow-inner shadow-black/20';
      block.dataset.testId = test.id;

      const header = document.createElement('div');
      header.className = 'flex flex-col gap-2 md:flex-row md:items-center md:justify-between';
      const summary = document.createElement('div');
      summary.innerHTML = `<strong>#${test.id} ${pathLabel}</strong> · 双向测试 · 端口 ${test.params.port} · 时长 ${test.params.duration}s`;
      header.appendChild(summary);

      const deleteBtn = document.createElement('button');
      deleteBtn.textContent = '删除记录';
      deleteBtn.className = styles.pillDanger;
      deleteBtn.onclick = () => deleteTestResult(test.id);
      header.appendChild(deleteBtn);
      block.appendChild(header);

      suiteEntries.forEach((entry) => {
        const section = document.createElement('div');
        section.className = 'mt-3 space-y-2 rounded-xl border border-slate-800/60 bg-slate-950/50 p-3';
        section.innerHTML = `<div class="flex items-center justify-between text-sm text-slate-200"><span class="font-semibold">${entry.label}</span><span class="text-xs uppercase text-slate-400">${entry.protocol.toUpperCase()}${entry.reverse ? ' (-R)' : ''}</span></div>`;
        section.appendChild(renderRawResult(entry.raw || {}));
        block.appendChild(section);
      });

      return block;
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

    loginButton?.addEventListener('click', (event) => { event.preventDefault(); login(); });
    loginForm?.addEventListener('submit', (event) => { event.preventDefault(); login(); });
    document.getElementById('logout-btn').addEventListener('click', logout);
    document.getElementById('run-test').addEventListener('click', runTest);
    document.getElementById('run-suite-test').addEventListener('click', runSuiteTest);
    protocolSelect?.addEventListener('change', toggleProtocolOptions);
    singleTestTab?.addEventListener('click', () => setActiveTestTab('single'));
    suiteTestTab?.addEventListener('click', () => setActiveTestTab('suite'));
    suiteDstSelect?.addEventListener('change', syncSuitePort);
    suiteSrcSelect?.addEventListener('change', syncSuitePort);
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
    passwordInput?.addEventListener('keyup', (e) => { if (e.key === 'Enter') login(); });

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

    toggleProtocolOptions();
    setActiveTestTab('single');
    syncSuitePort();
    checkAuth();
    ensureAutoRefresh();
  </script>

</body>
</html>

    """


def _schedules_html() -> str:
    return f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>定时任务 - iperf3 Master</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
  <style>
    body {{ background: linear-gradient(135deg, #0f172a 0%, #1e293b 100%); min-height: 100vh; }}
    .glass-card {{ background: rgba(15, 23, 42, 0.7); backdrop-filter: blur(10px); border: 1px solid rgba(148, 163, 184, 0.1); }}
    .custom-scrollbar::-webkit-scrollbar {{ width: 6px; height: 6px; }}
    .custom-scrollbar::-webkit-scrollbar-track {{ background: rgba(15, 23, 42, 0.3); border-radius: 3px; }}
    .custom-scrollbar::-webkit-scrollbar-thumb {{ background: rgba(148, 163, 184, 0.3); border-radius: 3px; }}
    .custom-scrollbar::-webkit-scrollbar-thumb:hover {{ background: rgba(148, 163, 184, 0.5); }}
  </style>
</head>
<body class="text-slate-100">
  <div class="container mx-auto px-4 py-8 max-w-7xl">
    <!-- Header -->
    <div class="mb-8 flex items-center justify-between">
      <div>
        <h1 class="text-3xl font-bold text-white">定时任务管理</h1>
        <p class="text-slate-400 mt-1">Schedule Management & Monitoring</p>
      </div>
      <div class="flex gap-3">
        <a href="/web" class="px-4 py-2 rounded-lg border border-slate-700 bg-slate-800/60 text-sm font-semibold text-slate-100 hover:border-sky-500 transition">
          ← 返回主页
        </a>
        <button id="create-schedule-btn" class="px-4 py-2 rounded-lg bg-gradient-to-r from-emerald-500 to-sky-500 text-sm font-semibold text-white shadow-lg hover:scale-105 transition">
          + 新建任务
        </button>
        <button id="refresh-btn" class="px-4 py-2 rounded-lg border border-slate-700 bg-slate-800/60 text-sm font-semibold text-slate-100 hover:border-sky-500 transition">
          🔄 刷新
        </button>
      </div>
    </div>

    <!-- Schedules List -->
    <div id="schedules-container" class="space-y-6">
      <div class="text-center text-slate-400 py-12">加载中...</div>
    </div>
  </div>

  <!-- Create/Edit Modal -->
  <div id="schedule-modal" class="fixed inset-0 z-50 hidden items-center justify-center bg-slate-950/80 px-4 backdrop-blur">
    <div class="glass-card relative w-full max-w-2xl rounded-2xl p-6 shadow-2xl">
      <button id="close-modal" class="absolute right-4 top-4 rounded-full border border-slate-700 bg-slate-800 p-2 text-slate-300 hover:bg-slate-700">✕</button>
      
      <h3 id="modal-title" class="text-xl font-bold text-white mb-6">新建定时任务</h3>
      
      <div class="space-y-4">
        <div>
          <label class="text-sm font-medium text-slate-200">任务名称</label>
          <input id="schedule-name" type="text" placeholder="例如: 北京→上海链路监控" class="w-full mt-1 rounded-lg border border-slate-700 bg-slate-900/60 px-3 py-2 text-slate-100 focus:border-sky-500 focus:outline-none">
        </div>
        
        <div class="grid grid-cols-2 gap-4">
          <div>
            <label class="text-sm font-medium text-slate-200">源节点</label>
            <select id="schedule-src" class="w-full mt-1 rounded-lg border border-slate-700 bg-slate-900/60 px-3 py-2 text-slate-100 focus:border-sky-500 focus:outline-none"></select>
          </div>
          <div>
            <label class="text-sm font-medium text-slate-200">目标节点</label>
            <select id="schedule-dst" class="w-full mt-1 rounded-lg border border-slate-700 bg-slate-900/60 px-3 py-2 text-slate-100 focus:border-sky-500 focus:outline-none"></select>
          </div>
        </div>
        
        <div class="grid grid-cols-3 gap-4">
          <div>
            <label class="text-sm font-medium text-slate-200">协议</label>
            <select id="schedule-protocol" class="w-full mt-1 rounded-lg border border-slate-700 bg-slate-900/60 px-3 py-2 text-slate-100 focus:border-sky-500 focus:outline-none">
              <option value="tcp">TCP</option>
              <option value="udp">UDP</option>
            </select>
          </div>
          <div>
            <label class="text-sm font-medium text-slate-200">时长(秒)</label>
            <input id="schedule-duration" type="number" value="10" min="1" class="w-full mt-1 rounded-lg border border-slate-700 bg-slate-900/60 px-3 py-2 text-slate-100 focus:border-sky-500 focus:outline-none">
          </div>
          <div>
            <label class="text-sm font-medium text-slate-200">并行数</label>
            <input id="schedule-parallel" type="number" value="1" min="1" class="w-full mt-1 rounded-lg border border-slate-700 bg-slate-900/60 px-3 py-2 text-slate-100 focus:border-sky-500 focus:outline-none">
          </div>
        </div>
        
        <div>
          <label class="text-sm font-medium text-slate-200">执行间隔(分钟)</label>
          <input id="schedule-interval" type="number" value="30" min="1" class="w-full mt-1 rounded-lg border border-slate-700 bg-slate-900/60 px-3 py-2 text-slate-100 focus:border-sky-500 focus:outline-none">
          <p class="text-xs text-slate-500 mt-1">建议: 30分钟 = 每天48次测试</p>
        </div>
        
        <div>
          <label class="text-sm font-medium text-slate-200">备注(可选)</label>
          <textarea id="schedule-notes" rows="2" placeholder="任务说明..." class="w-full mt-1 rounded-lg border border-slate-700 bg-slate-900/60 px-3 py-2 text-slate-100 focus:border-sky-500 focus:outline-none"></textarea>
        </div>
      </div>
      
      <div class="mt-6 flex justify-end gap-3">
        <button id="cancel-modal" class="px-4 py-2 rounded-lg border border-slate-700 bg-slate-800 text-sm font-semibold text-slate-100 hover:border-slate-500">取消</button>
        <button id="save-schedule" class="px-6 py-2 rounded-lg bg-gradient-to-r from-emerald-500 to-sky-500 text-sm font-semibold text-white shadow-lg hover:scale-105 transition">保存</button>
      </div>
    </div>
  </div>

  <script>
    const apiFetch = (url, options = {{}}) => fetch(url, {{ credentials: 'include', ...options }});
    let nodes = [];
    let schedules = [];
    let editingScheduleId = null;
    let charts = {{}};

    // 加载节点列表
    async function loadNodes() {{
      const res = await apiFetch('/nodes');
      nodes = await res.json();
      updateNodeSelects();
    }}

    function updateNodeSelects() {{
      const srcSelect = document.getElementById('schedule-src');
      const dstSelect = document.getElementById('schedule-dst');
      
      const options = nodes.map(n => `<option value="${{n.id}}">${{n.name}} (${{n.ip}})</option>`).join('');
      srcSelect.innerHTML = options;
      dstSelect.innerHTML = options;
    }}

    // 加载定时任务列表
    async function loadSchedules() {{
      const res = await apiFetch('/schedules');
      schedules = await res.json();
      renderSchedules();
    }}

    // 渲染定时任务列表
    function renderSchedules() {{
      const container = document.getElementById('schedules-container');
      
      if (schedules.length === 0) {{
        container.innerHTML = '<div class="text-center text-slate-400 py-12">暂无定时任务,点击"新建任务"开始</div>';
        return;
      }}
      
      container.innerHTML = schedules.map(schedule => {{
        const srcNode = nodes.find(n => n.id === schedule.src_node_id);
        const dstNode = nodes.find(n => n.id === schedule.dst_node_id);
        const statusBadge = schedule.enabled 
          ? '<span class="inline-flex items-center gap-1 px-2 py-1 rounded-full bg-emerald-500/20 text-emerald-300 text-xs font-semibold"><span class="h-2 w-2 rounded-full bg-emerald-400"></span>运行中</span>'
          : '<span class="inline-flex items-center gap-1 px-2 py-1 rounded-full bg-slate-700 text-slate-400 text-xs font-semibold"><span class="h-2 w-2 rounded-full bg-slate-500"></span>已暂停</span>';
        
        return `
          <div class="glass-card rounded-2xl p-6 space-y-4">
            <!-- Schedule Header -->
            <div class="flex items-start justify-between">
              <div class="flex-1">
                <h3 class="text-lg font-bold text-white">${{schedule.name}}</h3>
                <div class="mt-2 flex items-center gap-4 text-sm text-slate-300">
                  <span>${{srcNode?.name || 'Unknown'}} → ${{dstNode?.name || 'Unknown'}}</span>
                  <span class="text-slate-500">|</span>
                  <span>${{schedule.protocol.toUpperCase()}}</span>
                  <span class="text-slate-500">|</span>
                  <span>${{schedule.duration}}秒</span>
                  <span class="text-slate-500">|</span>
                  <span>每${{Math.floor(schedule.interval_seconds / 60)}}分钟</span>
                </div>
              </div>
              <div class="flex items-center gap-3">
                <div class="hidden md:block text-xs text-right mr-2 space-y-1">
                   <div class="text-slate-400">Next Run</div>
                   <div class="font-mono text-emerald-400" data-countdown="${{schedule.next_run_at || ''}}" data-schedule-id="${{schedule.id}}">Calculating...</div>
                </div>
                ${{statusBadge}}
                <button onclick="toggleSchedule(${{schedule.id}})" class="px-3 py-1 rounded-lg border border-slate-700 bg-slate-800 text-xs font-semibold text-slate-100 hover:border-sky-500 transition">
                  ${{schedule.enabled ? '暂停' : '启用'}}
                </button>
                <button onclick="runSchedule(${{schedule.id}})" class="px-3 py-1 rounded-lg border border-slate-700 bg-slate-800 text-xs font-semibold text-slate-100 hover:border-emerald-500 transition">立即运行</button>
                <button onclick="editSchedule(${{schedule.id}})" class="px-3 py-1 rounded-lg border border-slate-700 bg-slate-800 text-xs font-semibold text-slate-100 hover:border-sky-500 transition">编辑</button>
                <button onclick="deleteSchedule(${{schedule.id}})" class="px-3 py-1 rounded-lg border border-rose-700 bg-rose-900/20 text-xs font-semibold text-rose-300 hover:bg-rose-900/40 transition">删除</button>
              </div>
            </div>
            
            <!-- Chart Container -->
            <div class="glass-card rounded-xl p-4">
              <div class="flex items-center justify-between mb-4">
                <h4 class="text-sm font-semibold text-slate-200">24小时带宽监控</h4>
                <div class="flex items-center gap-2">
                  <button onclick="toggleHistory(${{schedule.id}})" class="px-3 py-1 rounded-lg border border-slate-700 bg-slate-800 text-xs font-semibold text-slate-100 hover:border-sky-500 transition">
                    📊 历史记录
                  </button>
                  <button onclick="changeDate(${{schedule.id}}, -1)" class="px-2 py-1 rounded border border-slate-700 bg-slate-800 text-xs text-slate-300 hover:border-sky-500">◀ 前一天</button>
                  <span id="date-${{schedule.id}}" class="text-xs text-slate-400">今天</span>
                  <button onclick="changeDate(${{schedule.id}}, 1)" class="px-2 py-1 rounded border border-slate-700 bg-slate-800 text-xs text-slate-300 hover:border-sky-500">后一天 ▶</button>
                </div>
              </div>
              <canvas id="chart-${{schedule.id}}" height="80"></canvas>
              <div id="stats-${{schedule.id}}"></div>
            </div>
            
            <!-- History Table (Collapsible) -->
            <div id="history-panel-${{schedule.id}}" class="glass-card rounded-xl p-4 mt-4 hidden">
               <div class="flex items-center justify-between mb-3">
                 <h4 class="text-sm font-semibold text-slate-200">最近执行记录</h4>
                 <button onclick="toggleHistory(${{schedule.id}})" class="text-xs text-slate-400 hover:text-slate-200">✕ 关闭</button>
               </div>
               <div class="overflow-x-auto max-h-60 overflow-y-auto custom-scrollbar">
                 <table class="w-full text-left text-xs">
                   <thead class="text-slate-400 border-b border-slate-700 sticky top-0 bg-slate-900/90 backdrop-blur z-10">
                     <tr>
                       <th class="pb-2">时间</th>
                       <th class="pb-2">上传 (Mbps)</th>
                       <th class="pb-2">下载 (Mbps)</th>
                       <th class="pb-2">延迟 (ms)</th>
                       <th class="pb-2">丢包 (%)</th>
                       <th class="pb-2">状态</th>
                     </tr>
                   </thead>
                   <tbody id="history-${{schedule.id}}" class="text-slate-300 divide-y divide-slate-800">
                     <tr><td colspan="6" class="py-2 text-center text-slate-500">加载中...</td></tr>
                   </tbody>
                 </table>
               </div>
            </div>
          </div>
        `;
      }}).join('');
      
      // 渲染图表和表格
      setTimeout(() => {{
        schedules.forEach(schedule => {{
          loadChartData(schedule.id);
        }});
        updateCountdowns();
        // 自动刷新逻辑 - 只在有新数据时才刷新图表
        if (window.refreshInterval) clearInterval(window.refreshInterval);
        window.refreshInterval = setInterval(async () => {{
             // 检查是否有新数据，只刷新图表数据，不重绘整个页面
             const res = await apiFetch('/schedules');
             const newSchedules = await res.json();
             
             // 更新 next_run_at 时间，但不重绘整个页面
             newSchedules.forEach(ns => {{
                const countdownEl = document.querySelector(`[data-countdown][data-schedule-id="${{ns.id}}"]`);
                if (countdownEl && ns.next_run_at) {{
                  countdownEl.dataset.countdown = ns.next_run_at;
                }}
             }});
             
             // 只在有新数据时才刷新图表（每分钟检查一次）
             const now = new Date();
             if (!window.lastChartRefresh || (now - window.lastChartRefresh) > 60000) {{
               schedules.forEach(s => loadChartData(s.id));
               window.lastChartRefresh = now;
             }}
        }}, 15000); 

        if (window.countdownInterval) clearInterval(window.countdownInterval);
        window.countdownInterval = setInterval(updateCountdowns, 1000);
      }}, 100);
    }}

    // 加载图表数据
    async function loadChartData(scheduleId, date = null) {{
      const dateEl = document.getElementById(`date-${{scheduleId}}`);
      // 如果没有指定date，且当前也没显示日期，则默认今天
      if (!date && (!dateEl || dateEl.textContent === '今天')) {{
         const d = new Date();
         date = `${{d.getFullYear()}}-${{String(d.getMonth()+1).padStart(2,'0')}}-${{String(d.getDate()).padStart(2,'0')}}`;
      }} else if (!date) {{
         // 使用当前显示的日期
         const currentDate = new Date(dateEl.textContent);
         date = `${{currentDate.getFullYear()}}-${{String(currentDate.getMonth()+1).padStart(2,'0')}}-${{String(currentDate.getDate()).padStart(2,'0')}}`;
      }}
      
      const tzOffset = new Date().getTimezoneOffset();
      const res = await apiFetch(`/schedules/${{scheduleId}}/results?date=${{date}}&tz_offset=${{tzOffset}}`);
      const data = await res.json();
      
      renderChart(scheduleId, data.results, date);
      renderHistoryTable(scheduleId, data.results);
    }}
    
    // 渲染历史表格
    function renderHistoryTable(scheduleId, results) {{
      const tbody = document.getElementById(`history-${{scheduleId}}`);
      if (!tbody) return;
      
      if (results.length === 0) {{
        tbody.innerHTML = '<tr><td colspan="6" class="py-2 text-center text-slate-500">暂无数据</td></tr>';
        return;
      }}
      
      // 按时间倒序
      const sorted = [...results].reverse().slice(0, 10); // 显示最近10条
      
      tbody.innerHTML = sorted.map(r => {{
          const time = new Date(r.executed_at).toLocaleTimeString('zh-CN');
          const statusColor = r.status === 'success' ? 'text-emerald-400' : 'text-rose-400';
          const s = r.test_result?.summary || {{}};
          const protocol = r.test_result?.protocol || 'tcp';
          
          // Convert bits_per_second to Mbps
          const speedMbps = s.bits_per_second ? (s.bits_per_second / 1000000).toFixed(2) : '-';
          
          // TCP 不显示丢包，UDP 才有丢包数据
          const lostPercent = protocol === 'udp' 
            ? (s.lost_percent?.toFixed(2) || '-')
            : '<span class="text-slate-600">N/A</span>';
          
          return `
            <tr>
              <td class="py-2">${{time}}</td>
              <td class="py-2 text-sky-400">${{speedMbps}}</td>
              <td class="py-2 text-emerald-400">${{speedMbps}}</td>
              <td class="py-1">${{s.latency_ms?.toFixed(2) || '-'}}</td>
              <td class="py-1">${{lostPercent}}</td>
              <td class="py-1 ${{statusColor}} text-xs" title="${{r.error_message || ''}}">
                ${{r.status}}
                ${{r.status === 'failed' ? '<span class="ml-1 cursor-help">ⓘ</span>' : ''}}
              </td>
            </tr>
          `;
      }}).join('');
    }}



    // 渲染Chart.js图表
    function renderChart(scheduleId, results, date) {{
      const canvas = document.getElementById(`chart-${{scheduleId}}`);
      if (!canvas) return;
      
      // 销毁旧图表
      if (charts[scheduleId]) {{
        charts[scheduleId].destroy();
      }}
      
      // 准备数据
      const labels = results.map(r => {{
        const time = new Date(r.executed_at);
        return time.toLocaleTimeString('zh-CN', {{ hour: '2-digit', minute: '2-digit' }});
      }});
      
      const uploadData = results.map(r => {{
        if (!r.test_result?.summary?.bits_per_second) return 0;
        return (r.test_result.summary.bits_per_second / 1000000).toFixed(2);
      }});
      
      const downloadData = results.map(r => {{
        if (!r.test_result?.summary?.bits_per_second) return 0;
        return (r.test_result.summary.bits_per_second / 1000000).toFixed(2);
      }});
      
      // 计算统计数据
      const uploadValues = uploadData.map(v => parseFloat(v) || 0);
      const downloadValues = downloadData.map(v => parseFloat(v) || 0);
      const uploadMax = Math.max(...uploadValues);
      const uploadAvg = uploadValues.reduce((a,b) => a+b, 0) / uploadValues.length;
      const uploadCurrent = uploadValues[uploadValues.length - 1] || 0;
      const downloadMax = Math.max(...downloadValues);
      const downloadAvg = downloadValues.reduce((a,b) => a+b, 0) / downloadValues.length;
      const downloadCurrent = downloadValues[downloadValues.length - 1] || 0;
      
      // 创建图表
      const ctx = canvas.getContext('2d');
      charts[scheduleId] = new Chart(ctx, {{
        type: 'line',
        data: {{
          labels: labels,
          datasets: [
            {{
              label: '上传 (Mbps)',
              data: uploadData,
              backgroundColor: 'rgba(59, 130, 246, 0.3)',
              borderColor: 'rgba(59, 130, 246, 1)',
              borderWidth: 1.5,
              fill: true,
              tension: 0.1,
              pointRadius: 0,
              pointHoverRadius: 4,
            }},
            {{
              label: '下载 (Mbps)',
              data: downloadData,
              backgroundColor: 'rgba(16, 185, 129, 0.3)',
              borderColor: 'rgba(16, 185, 129, 1)',
              borderWidth: 1.5,
              fill: true,
              tension: 0.1,
              pointRadius: 0,
              pointHoverRadius: 4,
            }}
          ]
        }},
        options: {{
          responsive: true,
          maintainAspectRatio: true,
          interaction: {{
            mode: 'index',
            intersect: false,
          }},
          plugins: {{
            legend: {{
              display: true,
              position: 'top',
              labels: {{ 
                color: '#cbd5e1',
                usePointStyle: true,
                padding: 15,
                font: {{ size: 11 }}
              }}
            }},
            tooltip: {{
              backgroundColor: 'rgba(15, 23, 42, 0.9)',
              titleColor: '#cbd5e1',
              bodyColor: '#94a3b8',
              borderColor: 'rgba(148, 163, 184, 0.2)',
              borderWidth: 1,
              padding: 12,
              displayColors: true,
              callbacks: {{
                afterLabel: function(context) {{
                  const result = results[context.dataIndex];
                  if (!result.test_result?.summary) return '';
                  const s = result.test_result.summary;
                  return [
                    `延迟: ${{s.latency_ms?.toFixed(2) || 'N/A'}} ms`,
                    `丢包: ${{s.lost_percent?.toFixed(2) || 'N/A'}} %`
                  ];
                }}
              }}
            }}
          }},
          scales: {{
            x: {{ 
              grid: {{
                display: true,
                color: 'rgba(148, 163, 184, 0.15)',
                drawBorder: false,
                lineWidth: 0.5,
              }},
              ticks: {{ 
                color: '#94a3b8',
                font: {{ size: 9 }},
                maxRotation: 0,
                autoSkip: true,
                maxTicksLimit: 24,
              }}
            }},
            y: {{ 
              grid: {{
                display: true,
                color: 'rgba(148, 163, 184, 0.15)',
                drawBorder: false,
                lineWidth: 0.5,
              }},
              ticks: {{ 
                color: '#94a3b8',
                font: {{ size: 9 }}
              }},
              beginAtZero: true,
              title: {{ 
                display: true, 
                text: 'Mbps', 
                color: '#cbd5e1',
                font: {{ size: 10, weight: 'bold' }}
              }}
            }}
          }}
        }}
      }});
      
      // 显示统计信息
      const statsEl = document.getElementById(`stats-${{scheduleId}}`);
      if (statsEl) {{
        statsEl.innerHTML = `
          <div class="text-xs text-slate-400 mt-2 flex gap-6">
            <div>
              <span class="text-sky-400">Max 上传:</span> ${{uploadMax.toFixed(2)}}Mb; 
              <span class="text-sky-400">Average 上传:</span> ${{uploadAvg.toFixed(2)}}Mb; 
              <span class="text-sky-400">Current 上传:</span> ${{uploadCurrent.toFixed(2)}}Mb;
            </div>
            <div>
              <span class="text-emerald-400">Max 下载:</span> ${{downloadMax.toFixed(2)}}Mb; 
              <span class="text-emerald-400">Average 下载:</span> ${{downloadAvg.toFixed(2)}}Mb; 
              <span class="text-emerald-400">Current 下载:</span> ${{downloadCurrent.toFixed(2)}}Mb;
            </div>
          </div>
        `;
      }}
      
      // 更新日期显示
      document.getElementById(`date-${{scheduleId}}`).textContent = date;
    }}

    // 切换日期
    function changeDate(scheduleId, offset) {{
      const dateEl = document.getElementById(`date-${{scheduleId}}`);
      const currentDate = new Date(dateEl.textContent === '今天' ? new Date() : dateEl.textContent);
      currentDate.setDate(currentDate.getDate() + offset);
      const newDate = currentDate.toISOString().split('T')[0];
      loadChartData(scheduleId, newDate);
    }}

    // Modal操作
    function openModal(scheduleId = null) {{
      editingScheduleId = scheduleId;
      const modal = document.getElementById('schedule-modal');
      const title = document.getElementById('modal-title');
      
      if (scheduleId) {{
        const schedule = schedules.find(s => s.id === scheduleId);
        title.textContent = '编辑定时任务';
        document.getElementById('schedule-name').value = schedule.name;
        document.getElementById('schedule-src').value = schedule.src_node_id;
        document.getElementById('schedule-dst').value = schedule.dst_node_id;
        document.getElementById('schedule-protocol').value = schedule.protocol;
        document.getElementById('schedule-duration').value = schedule.duration;
        document.getElementById('schedule-parallel').value = schedule.parallel;
        document.getElementById('schedule-interval').value = Math.floor(schedule.interval_seconds / 60);
        document.getElementById('schedule-notes').value = schedule.notes || '';
      }} else {{
        title.textContent = '新建定时任务';
        document.getElementById('schedule-name').value = '';
        document.getElementById('schedule-duration').value = 10;
        document.getElementById('schedule-parallel').value = 1;
        document.getElementById('schedule-interval').value = 30;
        document.getElementById('schedule-notes').value = '';
      }}
      
      modal.classList.remove('hidden');
      modal.classList.add('flex');
    }}

    function closeModal() {{
      document.getElementById('schedule-modal').classList.add('hidden');
      editingScheduleId = null;
    }}

    // 保存定时任务
    async function saveSchedule() {{
      const data = {{
        name: document.getElementById('schedule-name').value,
        src_node_id: parseInt(document.getElementById('schedule-src').value),
        dst_node_id: parseInt(document.getElementById('schedule-dst').value),
        protocol: document.getElementById('schedule-protocol').value,
        duration: parseInt(document.getElementById('schedule-duration').value),
        parallel: parseInt(document.getElementById('schedule-parallel').value),
        port: 62001,
        interval_seconds: parseInt(document.getElementById('schedule-interval').value) * 60,
        enabled: true,
        notes: document.getElementById('schedule-notes').value || null,
      }};
      
      try {{
        if (editingScheduleId) {{
          await apiFetch(`/schedules/${{editingScheduleId}}`, {{
            method: 'PUT',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify(data),
          }});
        }} else {{
          await apiFetch('/schedules', {{
            method: 'POST',
            headers: {{ 'Content-Type': 'application/json' }},
            body: JSON.stringify(data),
          }});
        }}
        
        closeModal();
        await loadSchedules();
      }} catch (err) {{
        alert('保存失败: ' + err.message);
      }}
    }}

    // 切换启用/禁用
    async function toggleSchedule(scheduleId) {{
      await apiFetch(`/schedules/${{scheduleId}}/toggle`, {{ method: 'POST' }});
      await loadSchedules();
    }}

    // 编辑
    function editSchedule(scheduleId) {{
      openModal(scheduleId);
    }}

    // 删除
    async function deleteSchedule(scheduleId) {{
      if (!confirm('确定要删除这个定时任务吗?')) return;
      await apiFetch(`/schedules/${{scheduleId}}`, {{ method: 'DELETE' }});
      await loadSchedules();
    }}

    // 立即执行
    // 立即执行
    async function runSchedule(scheduleId) {{
      if (!confirm('确定要立即执行此任务吗?')) return;
      try {{
        await apiFetch(`/schedules/${{scheduleId}}/execute`, {{ method: 'POST' }});
        alert('任务已触发, 请稍后刷新查看结果');
      }} catch (err) {{
        alert('执行失败: ' + err.message);
      }}
    }}

    // Toggle history panel visibility
    function toggleHistory(scheduleId) {{
      const panel = document.getElementById(`history-panel-${{scheduleId}}`);
      if (panel) {{
        panel.classList.toggle('hidden');
      }}
    }}

    function updateCountdowns() {{
      const now = new Date();
      document.querySelectorAll('[data-countdown]').forEach(el => {{
        const nextRun = el.dataset.countdown;
        if (!nextRun) {{
          el.textContent = '';
          return;
        }}
        const target = new Date(nextRun + (nextRun.endsWith('Z') ? '' : 'Z'));
        const diff = target - now;
        
        if (diff <= 0) {{
          el.textContent = 'Running...';
          return;
        }}
        
        const h = Math.floor(diff / 3600000);
        const m = Math.floor((diff % 3600000) / 60000);
        const s = Math.floor((diff % 60000) / 1000);
        el.textContent = `${{h.toString().padStart(2, '0')}}:${{m.toString().padStart(2, '0')}}:${{s.toString().padStart(2, '0')}}`;
      }});
    }}

    // 事件绑定
    document.getElementById('create-schedule-btn').addEventListener('click', () => openModal());
    document.getElementById('close-modal').addEventListener('click', closeModal);
    document.getElementById('cancel-modal').addEventListener('click', closeModal);
    document.getElementById('save-schedule').addEventListener('click', saveSchedule);
    document.getElementById('refresh-btn').addEventListener('click', loadSchedules);

    // 初始化
    (async () => {{
      await loadNodes();
      await loadSchedules();
    }})();
  </script>
</body>
</html>
'''



@app.get("/web", response_class=HTMLResponse)
def dashboard() -> HTMLResponse:
    """Serve dashboard page."""

    return HTMLResponse(content=_login_html())


@app.get("/web/schedules")
async def schedules_page(request: Request):
    """定时任务管理页面"""
    if not auth_manager().is_authenticated(request):
        return HTMLResponse(content="<script>window.location.href='/web';</script>")
    
    return HTMLResponse(content=_schedules_html())


@app.get("/auth/status")
def auth_status(request: Request) -> dict:
    return {"authenticated": _is_authenticated(request)}


@app.post("/auth/login")
def login(response: Response, payload: dict = Body(...)) -> dict:
    raw_password = payload.get("password")
    if raw_password is None or not str(raw_password).strip():
        raise HTTPException(status_code=400, detail="empty_password")

    if not auth_manager().verify_password(raw_password):
        raise HTTPException(status_code=401, detail="invalid_password")

    _set_auth_cookie(response, str(raw_password))
    return {"status": "ok"}


@app.post("/auth/logout")
def logout(response: Response) -> dict:
    response.delete_cookie(settings.dashboard_cookie_name)
    return {"status": "logged_out"}


@app.post("/auth/change")
def change_password(request: Request, response: Response, payload: PasswordChangeRequest) -> dict:
    current_password_raw = payload.current_password
    new_password_raw = payload.new_password

    if not new_password_raw or not str(new_password_raw).strip():
        raise HTTPException(status_code=400, detail="empty_password")

    new_password = dashboard_auth.normalize_password(new_password_raw)
    
    if len(new_password) < 6:
        raise HTTPException(status_code=400, detail="password_too_short")

    authenticated = _is_authenticated(request)
    if not authenticated and current_password_raw is None:
        raise HTTPException(status_code=401, detail="invalid_password")

    try:
        dashboard_auth.update_password(
            new_password,
            current_password=current_password_raw,
            force=authenticated or payload.force,
        )
    except ValueError as exc:
        if str(exc) == "invalid_password":
            raise HTTPException(status_code=401, detail="invalid_password")
        raise HTTPException(status_code=400, detail=str(exc))

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
                    detected_agent_port=node.agent_port,  # Agent port is the port we connected to
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
        detected_agent_port=None,
    )




@app.post("/admin/sync_whitelist")
async def sync_whitelist_endpoint(db: Session = Depends(get_db)):
    """
    Manually trigger whitelist synchronization to all agents.
    Returns sync results with success/failed counts.
    """
    results = await _sync_whitelist_to_agents(db)
    return {
        "status": "ok",
        "message": f"Whitelist synced to {results['success']}/{results['total_agents']} agents",
        "results": results
    }


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
    
    # Sync whitelist to all agents (async, don't wait)
    import asyncio
    try:
        asyncio.create_task(_sync_whitelist_to_agents(db))
    except:
        pass  # Ignore if event loop not running
    
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
    
    # Sync whitelist to all agents (async, don't wait)
    import asyncio
    try:
        asyncio.create_task(_sync_whitelist_to_agents(db))
    except:
        pass  # Ignore if event loop not running
    
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


async def _ensure_iperf_server_running(dst: Node, requested_port: int) -> bool:
    dst_status = await health_monitor.check_node(dst)
    current_port = dst_status.detected_iperf_port or dst_status.iperf_port
    if not dst_status.server_running:
        await _start_iperf_server(dst, requested_port)
        return True
    if current_port != requested_port:
        await _stop_iperf_server(dst)
        await _start_iperf_server(dst, requested_port)
        return True
    return False


async def _sync_whitelist_to_agents(db: Session) -> dict:
    """
    Synchronize IP whitelist to all agents.
    Whitelist includes all node IPs + Master's own IP.
    """
    # Get all node IPs
    nodes = db.scalars(select(Node)).all()
    whitelist = [node.ip for node in nodes]
    
    # Add Master's own IP (if configured)
    master_ip = os.getenv("MASTER_IP", "")
    if master_ip and master_ip not in whitelist:
        whitelist.append(master_ip)
    
    logger.info(f"Syncing whitelist with {len(whitelist)} IPs to {len(nodes)} agents")
    
    results = {
        "total_agents": len(nodes),
        "success": 0,
        "failed": 0,
        "errors": []
    }
    
    # Send whitelist to each agent
    async with httpx.AsyncClient(timeout=10) as client:
        for node in nodes:
            try:
                url = f"http://{node.ip}:{node.agent_port}/update_whitelist"
                response = await client.post(url, json={"allowed_ips": whitelist})
                
                if response.status_code == 200:
                    results["success"] += 1
                    logger.info(f"Whitelist synced to {node.name} ({node.ip})")
                else:
                    results["failed"] += 1
                    error_msg = f"{node.name}: HTTP {response.status_code}"
                    results["errors"].append(error_msg)
                    logger.warning(f"Failed to sync whitelist to {node.name}: {error_msg}")
            except Exception as e:
                results["failed"] += 1
                error_msg = f"{node.name}: {str(e)}"
                results["errors"].append(error_msg)
                logger.error(f"Failed to sync whitelist to {node.name}: {e}")
    
    return results


async def _call_agent_test(src: Node, payload: dict, duration: int) -> dict:
    agent_url = f"http://{src.ip}:{src.agent_port}/run_test"
    try:
        async with httpx.AsyncClient(timeout=duration + settings.request_timeout) as client:
            response = await client.post(agent_url, json=payload)
    except httpx.RequestError as exc:
        raise HTTPException(status_code=502, detail=f"failed to reach source agent: {exc}")

    if response.status_code != 200:
        detail = response.text
        logger.warning(f"Agent {src.name} ({src.ip}:{src.agent_port}) returned {response.status_code}: {response.text[:500]}")
        try:
            parsed = response.json()
            if isinstance(parsed, dict) and parsed.get("error"):
                detail = parsed.get("error")
            else:
                # If error field is empty, log the full response for debugging
                logger.error(f"Agent returned empty error. Full response: {response.text}")
                detail = f"Agent test failed (check master-api logs for details). Response: {response.text}"
        except Exception:
            pass
        raise HTTPException(status_code=502, detail=f"agent returned {response.status_code}: {detail}")


    try:
        raw_data = response.json()
    except ValueError:
        raise HTTPException(status_code=502, detail="agent response is not valid JSON")

    if not isinstance(raw_data, dict):
        detail = response.text[:200]
        raise HTTPException(
            status_code=502,
            detail=f"agent response JSON has unexpected format: {detail}",
        )

    return raw_data


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
    server_started = await _ensure_iperf_server_running(dst, requested_port)

    payload = {
        "target": dst.ip,
        "port": requested_port,
        "duration": test.duration,
        "protocol": test.protocol,
        "parallel": test.parallel,
        "reverse": test.reverse,
    }
    if test.bandwidth:
        payload["bandwidth"] = test.bandwidth
    if test.datagram_size:
        payload["datagram_size"] = test.datagram_size
    if test.omit is not None:
        payload["omit"] = test.omit

    try:
        raw_data = await _call_agent_test(src, payload, test.duration)
    finally:
        if server_started:
            await _stop_iperf_server(dst)
        health_monitor.invalidate(dst.id)

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


@app.post("/tests/suite", response_model=TestRead)
async def create_dual_suite(test: DualSuiteTestCreate, db: Session = Depends(get_db)):
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
    try:
        server_started = await _ensure_iperf_server_running(dst, requested_port)
        plan = [
            ("TCP 去程", "tcp", False, test.tcp_bandwidth),
            ("TCP 回程", "tcp", True, test.tcp_bandwidth),
            ("UDP 去程", "udp", False, test.udp_bandwidth),
            ("UDP 回程", "udp", True, test.udp_bandwidth),
        ]

        results: list[dict] = []
        for label, protocol, reverse, bandwidth in plan:
            payload = {
                "target": dst.ip,
                "port": requested_port,
                "duration": test.duration,
                "protocol": protocol,
                "parallel": test.parallel,
                "reverse": reverse,
            }
            if bandwidth:
                payload["bandwidth"] = bandwidth
            if protocol == "udp" and test.udp_datagram_size:
                payload["datagram_size"] = test.udp_datagram_size
            if test.omit is not None:
                payload["omit"] = test.omit

            raw_data = await _call_agent_test(src, payload, test.duration)
            results.append(
                {
                    "label": label,
                    "protocol": protocol,
                    "reverse": reverse,
                    "raw": raw_data,
                    "summary": _summarize_metrics(raw_data),
                }
            )

    finally:
        if server_started:
            await _stop_iperf_server(dst)
        health_monitor.invalidate(dst.id)

    summary = {
        "mode": "suite",
        "tests": [
            {
                "label": item["label"],
                "protocol": item["protocol"],
                "reverse": item["reverse"],
                "metrics": item["summary"],
            }
            for item in results
        ],
    }

    obj = TestResult(
        src_node_id=src.id,
        dst_node_id=dst.id,
        protocol="suite",
        params={"mode": "tcp_udp_bidir", **test.model_dump()},
        raw_result={"mode": "suite", "tests": results},
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


def _add_schedule_to_scheduler(schedule: TestSchedule):
    """添加任务到调度器"""
    job_id = f"schedule_{schedule.id}"
    
    # 移除旧任务(如果存在)
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    
    # 添加新任务
    scheduler.add_job(
        func=_execute_schedule_task,
        trigger=IntervalTrigger(seconds=schedule.interval_seconds),
        id=job_id,
        args=[schedule.id],
        replace_existing=True,
    )
    logger.info(f"Added schedule {schedule.id} to scheduler with interval {schedule.interval_seconds}s")


def _remove_schedule_from_scheduler(schedule_id: int):
    """从调度器移除任务"""
    job_id = f"schedule_{schedule_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
        logger.info(f"Removed schedule {schedule_id} from scheduler")


async def _execute_schedule_task(schedule_id: int):
    """执行定时任务"""
    
    db = SessionLocal()
    try:
        schedule = db.get(TestSchedule, schedule_id)
        if not schedule or not schedule.enabled:
            return
        
        logger.info(f"Executing schedule {schedule_id}: {schedule.name}")
        
        # 更新执行时间
        schedule.last_run_at = datetime.now(timezone.utc)
        from datetime import timedelta
        schedule.next_run_at = schedule.last_run_at + timedelta(seconds=schedule.interval_seconds)
        
        # 执行测试
        try:
            src_node = db.get(Node, schedule.src_node_id)
            dst_node = db.get(Node, schedule.dst_node_id)
            
            if not src_node or not dst_node:
                raise Exception("Source or destination node not found")
            
            # Get current detected port from health check
            dst_status = await health_monitor.check_node(dst_node)
            current_port = dst_status.detected_iperf_port or dst_status.iperf_port
            
            # 构造测试参数
            # Always use detected port to handle dynamic port changes (e.g., after agent reinstall)
            test_params = {
                "target": dst_node.ip,
                "port": current_port,  # Always use detected port, ignore schedule.port
                "duration": schedule.duration,
                "protocol": schedule.protocol,
                "parallel": schedule.parallel,
            }
            
            # 调用agent执行测试
            # 注意: 这里 _call_agent_test 是 async 的
            raw_data = await _call_agent_test(src_node, test_params, schedule.duration)
            summary = _summarize_metrics(raw_data)
            
            # 保存测试结果
            test_result = TestResult(
                src_node_id=schedule.src_node_id,
                dst_node_id=schedule.dst_node_id,
                protocol=schedule.protocol,
                params=test_params,
                raw_result=raw_data,
                summary=summary,
                created_at=datetime.now(timezone.utc),
            )
            db.add(test_result)
            db.flush()
            
            # 保存schedule结果
            schedule_result = ScheduleResult(
                schedule_id=schedule_id,
                test_result_id=test_result.id,
                executed_at=datetime.now(timezone.utc),
                status="success",
            )
            db.add(schedule_result)
            db.commit()
            
            logger.info(f"Schedule {schedule_id} executed successfully")
            
        except Exception as e:
            logger.error(f"Schedule {schedule_id} execution failed: {e}")
            
            # 保存失败记录
            schedule_result = ScheduleResult(
                schedule_id=schedule_id,
                test_result_id=None,
                executed_at=datetime.now(timezone.utc),
                status="failed",
                error_message=str(e),
            )
            db.add(schedule_result)
            db.commit()
            
    finally:
        db.close()


def _load_schedules_on_startup():
    """应用启动时加载所有启用的定时任务"""
    db = SessionLocal()
    try:
        schedules = db.scalars(
            select(TestSchedule).where(TestSchedule.enabled == True)
        ).all()
        
        for schedule in schedules:
            # 如果 next_run_at 为空，初始化它
            if not schedule.next_run_at:
                from datetime import timedelta
                if schedule.last_run_at:
                    schedule.next_run_at = schedule.last_run_at + timedelta(seconds=schedule.interval_seconds)
                else:
                    schedule.next_run_at = datetime.now(timezone.utc) + timedelta(seconds=schedule.interval_seconds)
                db.commit()
            
            _add_schedule_to_scheduler(schedule)
            logger.info(f"Loaded schedule {schedule.id}: {schedule.name}, next run at {schedule.next_run_at}")
    finally:
        db.close()


@app.post("/schedules", response_model=TestScheduleRead)
def create_schedule(schedule: TestScheduleCreate, db: Session = Depends(get_db)):
    """创建定时任务"""
    # 验证节点存在
    src_node = db.get(Node, schedule.src_node_id)
    dst_node = db.get(Node, schedule.dst_node_id)
    if not src_node or not dst_node:
        raise HTTPException(status_code=404, detail="Source or destination node not found")
    
    # 创建schedule
    db_schedule = TestSchedule(
        name=schedule.name,
        src_node_id=schedule.src_node_id,
        dst_node_id=schedule.dst_node_id,
        protocol=schedule.protocol,
        duration=schedule.duration,
        parallel=schedule.parallel,
        port=schedule.port,
        interval_seconds=schedule.interval_seconds,
        enabled=schedule.enabled,
        notes=schedule.notes,
    )
    
    # 计算下次执行时间
    if schedule.enabled:
        from datetime import timedelta
        db_schedule.next_run_at = datetime.now(timezone.utc) + timedelta(seconds=schedule.interval_seconds)
    
    db.add(db_schedule)
    db.commit()
    db.refresh(db_schedule)
    
    # 如果启用,添加到调度器
    if schedule.enabled:
        _add_schedule_to_scheduler(db_schedule)
    
    _persist_state(db)
    return db_schedule


@app.get("/schedules", response_model=List[TestScheduleRead])
def list_schedules(db: Session = Depends(get_db)):
    """获取所有定时任务"""
    schedules = db.scalars(select(TestSchedule)).all()
    return schedules


@app.get("/schedules/{schedule_id}", response_model=TestScheduleRead)
def get_schedule(schedule_id: int, db: Session = Depends(get_db)):
    """获取单个定时任务"""
    schedule = db.get(TestSchedule, schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return schedule


@app.put("/schedules/{schedule_id}", response_model=TestScheduleRead)
def update_schedule(
    schedule_id: int,
    schedule_update: TestScheduleUpdate,
    db: Session = Depends(get_db)
):
    """更新定时任务"""
    db_schedule = db.get(TestSchedule, schedule_id)
    if not db_schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    
    # 更新字段
    update_data = schedule_update.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(db_schedule, key, value)
    
    db.commit()
    db.refresh(db_schedule)
    
    # 重新调度
    _remove_schedule_from_scheduler(schedule_id)
    if db_schedule.enabled:
        _add_schedule_to_scheduler(db_schedule)
    
    _persist_state(db)
    return db_schedule


@app.delete("/schedules/{schedule_id}")
def delete_schedule(schedule_id: int, db: Session = Depends(get_db)):
    """删除定时任务"""
    db_schedule = db.get(TestSchedule, schedule_id)
    if not db_schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    
    # 从调度器移除
    _remove_schedule_from_scheduler(schedule_id)
    
    # 删除相关结果
    # 注意: SQLAlchemy session.delete 不会级联删除 schedule_results, 需手动或配置 cascade
    # 这里手动删除
    db.execute(
        text("DELETE FROM schedule_results WHERE schedule_id = :sid"),
        {"sid": schedule_id}
    )
    
    db.delete(db_schedule)
    db.commit()
    _persist_state(db)
    
    return {"status": "deleted", "schedule_id": schedule_id}


@app.post("/schedules/{schedule_id}/toggle")
def toggle_schedule(schedule_id: int, db: Session = Depends(get_db)):
    """启用/禁用定时任务"""
    db_schedule = db.get(TestSchedule, schedule_id)
    if not db_schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    
    db_schedule.enabled = not db_schedule.enabled
    
    if db_schedule.enabled:
        from datetime import timedelta
        db_schedule.next_run_at = datetime.now(timezone.utc) + timedelta(seconds=db_schedule.interval_seconds)
        _add_schedule_to_scheduler(db_schedule)
    else:
        db_schedule.next_run_at = None
        _remove_schedule_from_scheduler(schedule_id)
    
    db.commit()
    db.refresh(db_schedule)
    _persist_state(db)
    
    return {"enabled": db_schedule.enabled, "schedule_id": schedule_id}


@app.post("/schedules/{schedule_id}/execute")
async def execute_schedule(
    schedule_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db)
):
    """手动立即执行定时任务"""
    schedule = db.get(TestSchedule, schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    
    background_tasks.add_task(_execute_schedule_task, schedule_id)
    return {"status": "triggered", "schedule_id": schedule_id}


@app.get("/schedules/{schedule_id}/results")
def get_schedule_results(
    schedule_id: int,
    date: str = Query(None, description="Date in YYYY-MM-DD format"),
    tz_offset: int = Query(0, description="Timezone offset in minutes (JS getTimezoneOffset)"),
    db: Session = Depends(get_db)
):
    """获取定时任务的测试结果"""
    from datetime import datetime as dt, timedelta
    
    db_schedule = db.get(TestSchedule, schedule_id)
    if not db_schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    
    # 解析日期
    if date:
        try:
            target_date = dt.strptime(date, "%Y-%m-%d").date()
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid date format, use YYYY-MM-DD")
    else:
        # 默认为 UTC 当前日期，后续会结合 offset 修正
        target_date = dt.now(timezone.utc).date()
    
    # 计算查询的时间范围 (根据客户端时区)
    # Start (Local) = YYYY-MM-DD 00:00:00
    # UTC = Local + offset (minutes) (JS definition: UTC - Local)
    # Local = UTC - offset
    
    start_local = dt.combine(target_date, dt.min.time()).replace(tzinfo=timezone.utc)
    end_local = dt.combine(target_date, dt.max.time()).replace(tzinfo=timezone.utc)
    
    start_utc = start_local + timedelta(minutes=tz_offset)
    end_utc = end_local + timedelta(minutes=tz_offset)
    
    results = db.scalars(
        select(ScheduleResult)
        .where(ScheduleResult.schedule_id == schedule_id)
        .where(ScheduleResult.executed_at >= start_utc)
        .where(ScheduleResult.executed_at <= end_utc)
        .order_by(ScheduleResult.executed_at)
    ).all()
    
    # 关联test_result数据
    output = []
    for result in results:
        test_data = None
        if result.test_result_id:
            test = db.get(TestResult, result.test_result_id)
            if test:
                test_data = {
                    "id": test.id,
                    "protocol": test.protocol,
                    "summary": test.summary,
                    "created_at": test.created_at.isoformat() if test.created_at else None,
                }
        
        output.append({
            "id": result.id,
            "executed_at": result.executed_at.isoformat(),
            "status": result.status,
            "error_message": result.error_message,
            "test_result": test_data,
        })
    
    return {
        "schedule_id": schedule_id,
        "date": target_date.isoformat(),
        "tz_offset": tz_offset,
        "results": output,
    }


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


@app.get("/debug/failures")
def debug_failures(db: Session = Depends(get_db)):
    """Debug endpoint to list recent failures"""
    results = db.scalars(
        select(ScheduleResult)
        .where(ScheduleResult.status == "failed")
        .order_by(text("executed_at DESC"))
        .limit(10)
    ).all()
    return [{"id": r.id, "schedule_id": r.schedule_id, "time": r.executed_at, "error": r.error_message} for r in results]
