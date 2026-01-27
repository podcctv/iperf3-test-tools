import asyncio
import json
import logging
import os
import socket
import time
import ipaddress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Dict, List, Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger
from croniter import croniter

import httpx
from fastapi import BackgroundTasks, Body, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.gzip import GZipMiddleware
from sqlalchemy import or_, select, text
from sqlalchemy.orm import Session, joinedload

from .auth import auth_manager
from .config import settings
from .constants import DEFAULT_IPERF_PORT
from .database import SessionLocal, engine, get_db
from .agent_store import AgentConfigStore
from .models import Base, Node, TestResult, TestSchedule, ScheduleResult, PendingTask, AsnCache, AlertConfig, AlertHistory
from . import alert_service
from .asn_cache import sync_peeringdb, get_asn_info, get_asn_count
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
from .routers import dashboard

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Expected agent version - update when releasing new agent versions
EXPECTED_AGENT_VERSION = "1.4.0"

# Whitelist hash tracking for smart sync
_whitelist_hash_file = Path(os.getenv("DATA_DIR", "/app/data")) / "whitelist_hash.txt"
_last_whitelist_hash: str | None = None

def _compute_whitelist_hash(ips: list[str]) -> str:
    """Compute MD5 hash of sorted IP list for change detection."""
    import hashlib
    sorted_ips = sorted(set(ips))
    content = ",".join(sorted_ips)
    return hashlib.md5(content.encode()).hexdigest()

def _load_whitelist_hash() -> str | None:
    """Load stored whitelist hash from disk."""
    global _last_whitelist_hash
    try:
        if _whitelist_hash_file.exists():
            _last_whitelist_hash = _whitelist_hash_file.read_text().strip()
        return _last_whitelist_hash
    except Exception:
        return None

def _save_whitelist_hash(hash_value: str) -> None:
    """Save whitelist hash to disk for persistence."""
    global _last_whitelist_hash
    try:
        _whitelist_hash_file.parent.mkdir(parents=True, exist_ok=True)
        _whitelist_hash_file.write_text(hash_value)
        _last_whitelist_hash = hash_value
    except Exception as e:
        logger.error(f"Failed to save whitelist hash: {e}")

# ============================================================================
# Scheduler Setup
# ============================================================================

scheduler = AsyncIOScheduler()

# 添加调度器错误事件监听
def scheduler_error_listener(event):
    if event.exception:
        logger.error(f"APScheduler job {event.job_id} failed: {event.exception}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exception(type(event.exception), event.exception, event.exception.__traceback__)}")
    else:
        logger.info(f"APScheduler job {event.job_id} executed successfully")

from apscheduler.events import EVENT_JOB_EXECUTED, EVENT_JOB_ERROR
scheduler.add_listener(scheduler_error_listener, EVENT_JOB_EXECUTED | EVENT_JOB_ERROR)

# 注意：scheduler.start() 已移到 FastAPI lifespan 事件中


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


def _ensure_whitelist_sync_columns() -> None:
    dialect = engine.dialect.name
    with engine.connect() as connection:
        if dialect == "sqlite":
            columns = connection.exec_driver_sql("PRAGMA table_info(nodes)").fetchall()
            column_names = {col[1] for col in columns}
            
            if "whitelist_sync_status" not in column_names:
                connection.exec_driver_sql(
                    "ALTER TABLE nodes ADD COLUMN whitelist_sync_status VARCHAR DEFAULT 'unknown'"
                )
            if "whitelist_sync_message" not in column_names:
                connection.exec_driver_sql(
                    "ALTER TABLE nodes ADD COLUMN whitelist_sync_message VARCHAR"
                )
            if "whitelist_sync_at" not in column_names:
                connection.exec_driver_sql(
                    "ALTER TABLE nodes ADD COLUMN whitelist_sync_at DATETIME"
                )
            if "is_internal" not in column_names:
                connection.exec_driver_sql(
                    "ALTER TABLE nodes ADD COLUMN is_internal BOOLEAN DEFAULT 0"
                )
            # Auto-update status columns
            if "update_status" not in column_names:
                connection.exec_driver_sql(
                    "ALTER TABLE nodes ADD COLUMN update_status VARCHAR DEFAULT 'none'"
                )
            if "update_message" not in column_names:
                connection.exec_driver_sql(
                    "ALTER TABLE nodes ADD COLUMN update_message VARCHAR"
                )
            if "update_at" not in column_names:
                connection.exec_driver_sql(
                    "ALTER TABLE nodes ADD COLUMN update_at DATETIME"
                )
        elif dialect == "postgresql":
            result = connection.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name='nodes'"
                )
            )
            column_names = {row[0] for row in result}
            
            if "whitelist_sync_status" not in column_names:
                connection.execute(text("ALTER TABLE nodes ADD COLUMN whitelist_sync_status VARCHAR DEFAULT 'unknown'"))
            if "whitelist_sync_message" not in column_names:
                connection.execute(text("ALTER TABLE nodes ADD COLUMN whitelist_sync_message VARCHAR"))
            if "whitelist_sync_at" not in column_names:
                connection.execute(text("ALTER TABLE nodes ADD COLUMN whitelist_sync_at TIMESTAMPTZ"))
            if "is_internal" not in column_names:
                connection.execute(text("ALTER TABLE nodes ADD COLUMN is_internal BOOLEAN DEFAULT FALSE"))
            # Auto-update status columns
            if "update_status" not in column_names:
                connection.execute(text("ALTER TABLE nodes ADD COLUMN update_status VARCHAR DEFAULT 'none'"))
            if "update_message" not in column_names:
                connection.execute(text("ALTER TABLE nodes ADD COLUMN update_message VARCHAR"))
            if "update_at" not in column_names:
                connection.execute(text("ALTER TABLE nodes ADD COLUMN update_at TIMESTAMPTZ"))
        
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


def _ensure_schedule_columns() -> None:
    dialect = engine.dialect.name
    with engine.connect() as connection:
        if dialect == "sqlite":
            columns = connection.exec_driver_sql("PRAGMA table_info(test_schedules)").fetchall()
            column_names = {col[1] for col in columns}
            if "direction" not in column_names:
                connection.exec_driver_sql("ALTER TABLE test_schedules ADD COLUMN direction VARCHAR DEFAULT 'upload'")
        elif dialect == "postgresql":
            result = connection.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name='test_schedules'"
                )
            )
            column_names = {row[0] for row in result}
            if "direction" not in column_names:
                connection.execute(text("ALTER TABLE test_schedules ADD COLUMN direction VARCHAR DEFAULT 'upload'"))
        connection.commit()


def _ensure_reverse_mode_columns() -> None:
    """Add columns needed for reverse mode (NAT) agent support."""
    dialect = engine.dialect.name
    with engine.connect() as connection:
        if dialect == "sqlite":
            columns = connection.exec_driver_sql("PRAGMA table_info(nodes)").fetchall()
            column_names = {col[1] for col in columns}
            
            if "agent_mode" not in column_names:
                connection.exec_driver_sql(
                    "ALTER TABLE nodes ADD COLUMN agent_mode VARCHAR DEFAULT 'normal'"
                )
            if "agent_version" not in column_names:
                connection.exec_driver_sql(
                    "ALTER TABLE nodes ADD COLUMN agent_version VARCHAR"
                )
            if "last_heartbeat" not in column_names:
                connection.exec_driver_sql(
                    "ALTER TABLE nodes ADD COLUMN last_heartbeat DATETIME"
                )
        elif dialect == "postgresql":
            result = connection.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name='nodes'"
                )
            )
            column_names = {row[0] for row in result}
            
            if "agent_mode" not in column_names:
                connection.execute(text("ALTER TABLE nodes ADD COLUMN agent_mode VARCHAR DEFAULT 'normal'"))
            if "agent_version" not in column_names:
                connection.execute(text("ALTER TABLE nodes ADD COLUMN agent_version VARCHAR"))
            if "last_heartbeat" not in column_names:
                connection.execute(text("ALTER TABLE nodes ADD COLUMN last_heartbeat TIMESTAMPTZ"))
        
        connection.commit()


def _ensure_trace_source_type_column() -> None:
    """Add source_type column to trace_results table for distinguishing trace sources."""
    dialect = engine.dialect.name
    with engine.connect() as connection:
        if dialect == "sqlite":
            columns = connection.exec_driver_sql("PRAGMA table_info(trace_results)").fetchall()
            column_names = {col[1] for col in columns}
            
            if "source_type" not in column_names:
                connection.exec_driver_sql(
                    "ALTER TABLE trace_results ADD COLUMN source_type VARCHAR DEFAULT 'scheduled'"
                )
        elif dialect == "postgresql":
            result = connection.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name='trace_results'"
                )
            )
            column_names = {row[0] for row in result}
            
            if "source_type" not in column_names:
                connection.execute(text("ALTER TABLE trace_results ADD COLUMN source_type VARCHAR DEFAULT 'scheduled'"))
        
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
            _ensure_schedule_columns()
            _ensure_whitelist_sync_columns()
            _ensure_reverse_mode_columns()
            _ensure_trace_source_type_column()
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
        _ensure_test_result_columns()
        _ensure_iperf_port_column()
        _ensure_whitelist_sync_columns()
        state_store.restore(db)
    finally:
        db.close()


_bootstrap_state()

# FastAPI lifespan 上下文管理器
from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app):
    # Startup: 启动调度器和加载定时任务
    print(">>> LIFESPAN STARTUP <<<", flush=True)
    logger.info("Starting APScheduler...")
    scheduler.start()
    print(">>> SCHEDULER STARTED <<<", flush=True)
    logger.info("APScheduler started successfully")
    
    # Start health monitor (needed for ping history storage)
    await health_monitor.start()
    print(">>> HEALTH MONITOR STARTED <<<", flush=True)
    logger.info("Health monitor started")
    
    # Load existing schedules
    try:
        _load_schedules_on_startup()
        print(">>> SCHEDULES LOADED <<<", flush=True)
        logger.info("Schedules loaded")
    except Exception as e:
        print(f">>> FAILED TO LOAD SCHEDULES: {e} <<<", flush=True)
        logger.error(f"Failed to load schedules: {e}")
    
    # Load trace schedules
    try:
        _load_trace_schedules_on_startup()
        print(">>> TRACE SCHEDULES LOADED <<<", flush=True)
        logger.info("Trace schedules loaded")
    except Exception as e:
        print(f">>> FAILED TO LOAD TRACE SCHEDULES: {e} <<<", flush=True)
        logger.error(f"Failed to load trace schedules: {e}")
    
    # Add daily ASN sync job (PeeringDB Tier classification)
    def _run_asn_sync():
        """Sync ASN data from PeeringDB."""
        try:
            db = SessionLocal()
            stats = sync_peeringdb(db)
            logger.info(f"[ASN-SYNC] Daily sync complete: {stats}")
            db.close()
        except Exception as e:
            logger.error(f"[ASN-SYNC] Daily sync failed: {e}")
    
    scheduler.add_job(
        _run_asn_sync,
        'interval',
        hours=24,
        id='asn_daily_sync',
        replace_existing=True,
        next_run_time=datetime.now(timezone.utc) + timedelta(minutes=5)  # First run 5 min after startup
    )
    logger.info("[ASN-SYNC] Daily PeeringDB sync scheduled (every 24h)")
    
    # Add periodic whitelist sync job (every 1 hour)
    async def _run_whitelist_sync():
        """Sync whitelist to all agents periodically."""
        try:
            db = SessionLocal()
            results = await _sync_whitelist_to_agents(db)
            logger.info(f"[WHITELIST-SYNC] Periodic sync complete: {results['success']}/{results['total_agents']} agents synced")
            db.close()
        except Exception as e:
            logger.error(f"[WHITELIST-SYNC] Periodic sync failed: {e}")
    
    scheduler.add_job(
        _run_whitelist_sync,
        'interval',
        hours=1,
        id='whitelist_hourly_sync',
        replace_existing=True,
        next_run_time=datetime.now(timezone.utc) + timedelta(minutes=2)  # First run 2 min after startup
    )
    logger.info("[WHITELIST-SYNC] Hourly whitelist sync scheduled")
    
    yield
    
    # Shutdown: 关闭调度器
    scheduler.shutdown()
    logger.info("APScheduler shutdown")

app = FastAPI(title="iperf3 master api", lifespan=lifespan)
agent_store = AgentConfigStore(settings.agent_config_file)

# Performance optimizations
app.add_middleware(GZipMiddleware, minimum_size=1000)  # Enable Gzip compression

# Mount static files directory if it exists
import os
static_dir = Path(__file__).parent.parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

app.include_router(dashboard.router, prefix="/web", tags=["dashboard"])



# ============================================================================
# Health Check Endpoint
# ============================================================================

@app.get("/health")
async def health_check():
    """
    Health check endpoint for container orchestration and monitoring.
    Returns 200 OK if the service is healthy.
    """
    from datetime import datetime, timezone
    import time
    
    health_status = {
        "status": "healthy",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "version": EXPECTED_AGENT_VERSION,
        "checks": {}
    }
    
    # Check database connectivity
    try:
        db = next(get_db())
        db.execute(text("SELECT 1"))
        health_status["checks"]["database"] = "ok"
    except Exception as e:
        health_status["status"] = "degraded"
        health_status["checks"]["database"] = f"error: {str(e)[:100]}"
    
    # Check Redis connectivity and measure latency
    try:
        from .redis_client import get_redis_client
        client = get_redis_client()
        if client:
            start = time.perf_counter()
            client.ping()
            latency_ms = (time.perf_counter() - start) * 1000
            health_status["checks"]["redis"] = "ok"
            health_status["checks"]["redis_latency_ms"] = round(latency_ms, 2)
        else:
            health_status["checks"]["redis"] = "disconnected"
    except Exception as e:
        health_status["status"] = "degraded"
        health_status["checks"]["redis"] = f"error: {str(e)[:100]}"
    
    # Check scheduler status
    health_status["checks"]["scheduler"] = "running" if scheduler.running else "stopped"
    
    return health_status


@app.get("/health/live")
async def liveness_probe():
    """Kubernetes liveness probe - returns 200 if app is running."""
    return {"status": "alive"}


@app.get("/health/ready")
async def readiness_probe():
    """Kubernetes readiness probe - returns 200 if app can handle traffic."""
    try:
        db = next(get_db())
        db.execute(text("SELECT 1"))
        return {"status": "ready"}
    except Exception:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=503, content={"status": "not_ready"})


# ============================================================================
# Database Backup Export API
# ============================================================================

@app.get("/api/backup/export")
async def export_backup(request: Request, db: Session = Depends(get_db)):
    """Export database backup as JSON (admin only)."""
    if not auth_manager().is_authenticated(request):
        raise HTTPException(status_code=401, detail="unauthorized")
    
    from datetime import datetime, timezone
    from .models import Node, TestResult, TestSchedule, TraceSchedule, TraceResult
    
    # Export nodes
    nodes_data = []
    for node in db.scalars(select(Node)).all():
        nodes_data.append({
            "id": node.id,
            "name": node.name,
            "ip": node.ip,
            "agent_port": node.agent_port,
            "iperf_port": node.iperf_port,
            "description": node.description,
            "is_internal": node.is_internal,
            "agent_mode": node.agent_mode
        })
    
    # Export test schedules
    schedules_data = []
    for sched in db.scalars(select(TestSchedule)).all():
        schedules_data.append({
            "id": sched.id,
            "name": sched.name,
            "src_node_id": sched.src_node_id,
            "dst_node_id": sched.dst_node_id,
            "protocol": sched.protocol,
            "params": sched.params,
            "interval": sched.interval,
            "enabled": sched.enabled
        })
    
    # Export trace schedules
    trace_schedules_data = []
    for ts in db.scalars(select(TraceSchedule)).all():
        trace_schedules_data.append({
            "id": ts.id,
            "name": ts.name,
            "src_node_id": ts.src_node_id,
            "target": ts.target,
            "interval_minutes": ts.interval_minutes,
            "enabled": ts.enabled
        })
    
    # Get counts (don't export full test results for size)
    test_result_count = db.scalar(select(func.count(TestResult.id))) or 0
    trace_result_count = db.scalar(select(func.count(TraceResult.id))) or 0
    
    backup_data = {
        "backup_timestamp": datetime.now(timezone.utc).isoformat(),
        "version": EXPECTED_AGENT_VERSION,
        "nodes": nodes_data,
        "test_schedules": schedules_data,
        "trace_schedules": trace_schedules_data,
        "stats": {
            "test_result_count": test_result_count,
            "trace_result_count": trace_result_count
        }
    }
    
    from fastapi.responses import JSONResponse
    return JSONResponse(
        content=backup_data,
        headers={
            "Content-Disposition": f"attachment; filename=iperf3_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        }
    )


@app.get("/api/stats")
async def get_system_stats(db: Session = Depends(get_db)):
    """Get system statistics for monitoring dashboards."""
    from datetime import datetime, timezone, timedelta
    from sqlalchemy import func
    from .models import Node, TestResult, TestSchedule, TraceResult, AuditLog
    
    now = datetime.now(timezone.utc)
    one_day_ago = now - timedelta(days=1)
    one_hour_ago = now - timedelta(hours=1)
    
    # Node stats
    total_nodes = db.scalar(select(func.count(Node.id))) or 0
    online_nodes = db.scalar(
        select(func.count(Node.id)).where(
            Node.last_heartbeat > one_hour_ago
        )
    ) or 0
    
    # Test stats
    total_tests = db.scalar(select(func.count(TestResult.id))) or 0
    tests_today = db.scalar(
        select(func.count(TestResult.id)).where(
            TestResult.created_at > one_day_ago
        )
    ) or 0
    
    # Schedule stats
    total_schedules = db.scalar(select(func.count(TestSchedule.id))) or 0
    enabled_schedules = db.scalar(
        select(func.count(TestSchedule.id)).where(TestSchedule.enabled == True)
    ) or 0
    
    # Trace stats
    total_traces = db.scalar(select(func.count(TraceResult.id))) or 0
    
    # Recent login activity
    login_attempts_24h = db.scalar(
        select(func.count(AuditLog.id)).where(
            AuditLog.action.in_(["login_success", "login_failed"]),
            AuditLog.timestamp > one_day_ago
        )
    ) or 0
    
    return {
        "timestamp": now.isoformat(),
        "version": EXPECTED_AGENT_VERSION,
        "nodes": {
            "total": total_nodes,
            "online": online_nodes,
            "offline": total_nodes - online_nodes
        },
        "tests": {
            "total": total_tests,
            "today": tests_today
        },
        "schedules": {
            "total": total_schedules,
            "enabled": enabled_schedules
        },
        "traces": {
            "total": total_traces
        },
        "security": {
            "login_attempts_24h": login_attempts_24h
        },
        "scheduler": {
            "running": scheduler.running,
            "jobs": len(scheduler.get_jobs())
        }
    }


@app.post("/api/cleanup")
async def cleanup_old_data(
    request: Request,
    days: int = 30,
    include_tests: bool = True,
    include_traces: bool = True,
    include_audit_logs: bool = False,
    db: Session = Depends(get_db)
):
    """
    Clean up old data to manage database size (admin only).
    Deletes test results, trace results, and optionally audit logs older than specified days.
    """
    if not auth_manager().is_authenticated(request):
        raise HTTPException(status_code=401, detail="unauthorized")
    
    if days < 7:
        raise HTTPException(status_code=400, detail="Minimum retention is 7 days")
    
    from datetime import datetime, timezone, timedelta
    from sqlalchemy import delete, func
    from .models import TestResult, TraceResult, AuditLog
    
    cutoff_date = datetime.now(timezone.utc) - timedelta(days=days)
    deleted_counts = {}
    
    # Delete old test results
    if include_tests:
        result = db.execute(
            delete(TestResult).where(TestResult.created_at < cutoff_date)
        )
        deleted_counts["test_results"] = result.rowcount
    
    # Delete old trace results
    if include_traces:
        result = db.execute(
            delete(TraceResult).where(TraceResult.executed_at < cutoff_date)
        )
        deleted_counts["trace_results"] = result.rowcount
    
    # Delete old audit logs (optional, disabled by default)
    if include_audit_logs:
        result = db.execute(
            delete(AuditLog).where(AuditLog.timestamp < cutoff_date)
        )
        deleted_counts["audit_logs"] = result.rowcount
    
    db.commit()
    
    # Log the cleanup action
    client_ip = _get_client_ip(request)
    audit_log(db, "data_cleanup", actor_ip=client_ip, 
              details={"days": days, "deleted": deleted_counts})
    
    return {
        "status": "ok",
        "cutoff_date": cutoff_date.isoformat(),
        "deleted": deleted_counts
    }




# ============================================================================
# Redis Cache Management APIs
# ============================================================================

@app.get("/api/cache/stats")
async def get_cache_statistics():
    """
    Get comprehensive Redis cache statistics and performance metrics.
    """
    from .redis_client import get_cache_stats
    
    stats = get_cache_stats()
    return stats


@app.post("/api/cache/clear")
async def clear_cache(
    request: Request,
    pattern: Optional[str] = Query(None, description="Redis pattern (e.g. 'nodes:*')"),
    all: bool = Query(False, description="Clear all cache entries"),
    db: Session = Depends(get_db)
):
    """
    Clear cache entries (admin only).
    
    - Use `pattern` to clear specific keys matching a pattern
    - Use `all=true` to clear all cache entries
    """
    if not auth_manager().is_authenticated(request):
        raise HTTPException(status_code=401, detail="unauthorized")
    
    from .redis_client import cache_clear_pattern, cache_clear_all
    
    result = {"status": "ok", "cleared": 0}
    
    if all:
        success = cache_clear_all()
        result["message"] = "All cache entries cleared" if success else "Failed to clear cache"
        result["cleared"] = "all" if success else 0
    elif pattern:
        cleared = cache_clear_pattern(pattern)
        result["cleared"] = cleared
        result["pattern"] = pattern
        result["message"] = f"Cleared {cleared} keys matching pattern: {pattern}"
    else:
        raise HTTPException(status_code=400, detail="Must specify either 'pattern' or 'all=true'")
    
    # Log the cache clear action
    client_ip = _get_client_ip(request)
    audit_log(db, "cache_clear", actor_ip=client_ip, 
              details={"pattern": pattern, "all": all, "cleared": result["cleared"]})
    
    return result


@app.post("/api/cache/warmup")
async def warmup_cache(
    request: Request,
    db: Session = Depends(get_db)
):
    """
    Preload frequently accessed data into cache (admin only).
    This helps improve initial response times after cache clear or restart.
    """
    if not auth_manager().is_authenticated(request):
        raise HTTPException(status_code=401, detail="unauthorized")
    
    from .redis_client import cache_set
    
    warmed_count = 0
    
    # Warm up nodes list
    try:
        nodes = db.scalars(select(Node)).all()
        nodes_list = [NodeRead.model_validate(n).model_dump() for n in nodes]
        if cache_set("nodes:list", nodes_list, ttl=60):
            warmed_count += 1
    except Exception as e:
        logger.error(f"Failed to warm up nodes cache: {e}")
    
    # Warm up nodes with status
    try:
        statuses = await health_monitor.get_statuses(db)
        statuses_list = [s.model_dump() for s in statuses]
        if cache_set("nodes:with_status", statuses_list, ttl=10):
            warmed_count += 1
    except Exception as e:
        logger.error(f"Failed to warm up nodes status cache: {e}")
    
    # Log the warmup action
    client_ip = _get_client_ip(request)
    audit_log(db, "cache_warmup", actor_ip=client_ip, 
              details={"warmed_entries": warmed_count})
    
    return {
        "status": "ok",
        "warmed_entries": warmed_count,
        "message": f"Successfully warmed up {warmed_count} cache entries"
    }


# ============================================================================
# Webhook Notification System
# ============================================================================

import httpx
from pathlib import Path

_webhook_config_file = Path(os.getenv("DATA_DIR", "/app/data")) / "webhook_config.json"

def _load_webhook_config() -> dict:
    """Load webhook configuration from file."""
    try:
        if _webhook_config_file.exists():
            import json
            return json.loads(_webhook_config_file.read_text())
    except Exception as e:
        logger.error(f"Failed to load webhook config: {e}")
    return {"enabled": False, "url": "", "type": "generic"}

def _save_webhook_config(config: dict) -> bool:
    """Save webhook configuration to file."""
    try:
        import json
        _webhook_config_file.parent.mkdir(parents=True, exist_ok=True)
        _webhook_config_file.write_text(json.dumps(config, indent=2))
        return True
    except Exception as e:
        logger.error(f"Failed to save webhook config: {e}")
        return False

async def send_webhook_notification(title: str, message: str, level: str = "info"):
    """Send notification to configured webhook."""
    config = _load_webhook_config()
    if not config.get("enabled") or not config.get("url"):
        return
    
    try:
        webhook_type = config.get("type", "generic")
        url = config["url"]
        
        # Format payload based on webhook type
        if webhook_type == "telegram":
            # Telegram Bot API format
            payload = {
                "chat_id": config.get("chat_id", ""),
                "text": f"*{title}*\n{message}",
                "parse_mode": "Markdown"
            }
        elif webhook_type == "discord":
            # Discord webhook format
            color = {"info": 3447003, "warning": 16776960, "error": 15158332}.get(level, 3447003)
            payload = {
                "embeds": [{
                    "title": title,
                    "description": message,
                    "color": color
                }]
            }
        else:
            # Generic webhook format
            payload = {
                "title": title,
                "message": message,
                "level": level,
                "timestamp": datetime.now(timezone.utc).isoformat()
            }
        
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(url, json=payload)
            
    except Exception as e:
        logger.error(f"Webhook notification failed: {e}")


@app.get("/api/webhook/config")
async def get_webhook_config(request: Request):
    """Get current webhook configuration (admin only)."""
    if not auth_manager().is_authenticated(request):
        raise HTTPException(status_code=401, detail="unauthorized")
    
    config = _load_webhook_config()
    # Don't expose full URL for security
    if config.get("url"):
        config["url_configured"] = True
        config["url"] = config["url"][:20] + "..." if len(config.get("url", "")) > 20 else config.get("url", "")
    return config


@app.post("/api/webhook/config")
async def set_webhook_config(
    request: Request,
    enabled: bool = True,
    url: str = "",
    webhook_type: str = "generic",
    chat_id: str = "",
    db: Session = Depends(get_db)
):
    """Configure webhook notifications (admin only)."""
    if not auth_manager().is_authenticated(request):
        raise HTTPException(status_code=401, detail="unauthorized")
    
    config = {
        "enabled": enabled,
        "url": url,
        "type": webhook_type,
        "chat_id": chat_id
    }
    
    if _save_webhook_config(config):
        client_ip = _get_client_ip(request)
        audit_log(db, "webhook_config_changed", actor_ip=client_ip,
                  details={"enabled": enabled, "type": webhook_type})
        return {"status": "ok", "message": "Webhook configuration saved"}
    
    raise HTTPException(status_code=500, detail="Failed to save configuration")


@app.post("/api/webhook/test")
async def test_webhook(request: Request):
    """Send a test notification to configured webhook (admin only)."""
    if not auth_manager().is_authenticated(request):
        raise HTTPException(status_code=401, detail="unauthorized")
    
    config = _load_webhook_config()
    if not config.get("enabled") or not config.get("url"):
        raise HTTPException(status_code=400, detail="Webhook not configured")
    
    await send_webhook_notification(
        "🔔 测试通知",
        "这是来自 iPerf3 测试工具的测试通知。如果您看到此消息，表示 Webhook 配置正确！",
        "info"
    )
    
    return {"status": "ok", "message": "Test notification sent"}


def _agent_config_from_node(node: Node) -> AgentConfigCreate:
    return AgentConfigCreate(
        name=node.name,
        host=node.ip,
        agent_port=node.agent_port,
        iperf_port=node.iperf_port,
        description=node.description,
    )


# ============================================================================
# Audit Logging Helper
# ============================================================================

from .models import AuditLog

def audit_log(
    db: Session,
    action: str,
    actor_ip: str = None,
    actor_type: str = "user",
    resource_type: str = None,
    resource_id: str = None,
    details: dict = None,
    success: bool = True
):
    """Record an audit log entry."""
    try:
        log_entry = AuditLog(
            action=action,
            actor_ip=actor_ip,
            actor_type=actor_type,
            resource_type=resource_type,
            resource_id=resource_id,
            details=details,
            success=success
        )
        db.add(log_entry)
        db.commit()
    except Exception as e:
        logger.error(f"Failed to write audit log: {e}")


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


def _summarize_metrics(raw: dict | None, direction_label: str | None = None) -> dict | None:
    """
    Summarize iperf3 test metrics from raw result.
    
    Args:
        raw: Raw iperf3 result dictionary
        direction_label: Optional direction hint ("upload" or "download") for UDP tests
                        which only have a single 'sum' field, not sum_sent/sum_received
    """
    if not raw:
        return None

    body = raw.get("iperf_result") if isinstance(raw, dict) else None
    result = body or raw
    end = result.get("end", {}) if isinstance(result, dict) else {}

    sum_received = end.get("sum_received") or {}
    sum_sent = end.get("sum_sent") or {}
    sum_general = end.get("sum") or {}  # For UDP which only has 'sum'
    
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
        sum_general.get("bits_per_second"),  # Fallback for UDP
    )

    jitter_ms = _metric(
        (sum_received or {}).get("jitter_ms"),
        (sum_sent or {}).get("jitter_ms"),
        sum_general.get("jitter_ms"),  # Fallback for UDP
        (receiver_stream or {}).get("jitter_ms") if receiver_stream else None,
        (sender_stream or {}).get("jitter_ms") if sender_stream else None,
    )

    lost_percent = _metric(
        (sum_received or {}).get("lost_percent"),
        (sum_sent or {}).get("lost_percent"),
        sum_general.get("lost_percent"),  # Fallback for UDP
        (receiver_stream or {}).get("lost_percent") if receiver_stream else None,
        (sender_stream or {}).get("lost_percent") if sender_stream else None,
    )

    if lost_percent is None:
        # Try to calculate from packets if available
        for sum_obj in [sum_received, sum_general]:
            if sum_obj:
                lost_packets = sum_obj.get("lost_packets")
                packets = sum_obj.get("packets")
                if lost_packets is not None and packets:
                    lost_percent = (lost_packets / packets) * 100
                    break

    latency_ms = _metric(
        (sender_stream or {}).get("mean_rtt") if sender_stream else None,
        (sender_stream or {}).get("rtt") if sender_stream else None,
        (receiver_stream or {}).get("mean_rtt") if receiver_stream else None,
        (receiver_stream or {}).get("rtt") if receiver_stream else None,
    )

    if latency_ms is not None and latency_ms > 1000:
        latency_ms = latency_ms / 1000

    # Determine upload/download bits_per_second
    # Use SOURCE NODE perspective (user configures src → dst direction):
    # - sum_sent = data sent BY source TO target = source's UPLOAD
    # - sum_received = data received BY source FROM target = source's DOWNLOAD
    # This matches user expectation: upload test shows source's upload speed
    upload_bps = (sum_sent or {}).get("bits_per_second")  # Source sends → Source upload
    download_bps = (sum_received or {}).get("bits_per_second")  # Source receives → Source download
    
    # For UDP tests: iperf3 only provides 'sum' field, not sum_sent/sum_received
    # Use direction_label to assign correctly (source perspective)
    if not upload_bps and not download_bps and sum_general.get("bits_per_second"):
        general_bps = sum_general.get("bits_per_second")
        # Direction is from source's perspective
        if direction_label == "upload":
            # Source uploading
            upload_bps = general_bps
        elif direction_label == "download":
            # Source downloading
            download_bps = general_bps
        else:
            # Default to upload if no direction specified
            upload_bps = general_bps

    # Calculate bytes for traffic badge (source perspective)
    upload_bytes = (sum_sent or {}).get("bytes")  # Source's upload bytes
    download_bytes = (sum_received or {}).get("bytes")  # Source's download bytes
    
    # Fallback to general sum for UDP
    if not upload_bytes and not download_bytes and sum_general.get("bytes"):
        general_bytes = sum_general.get("bytes")
        if direction_label == "upload":
            upload_bytes = general_bytes
        elif direction_label == "download":
            download_bytes = general_bytes
        else:
            upload_bytes = general_bytes

    return {
        "bits_per_second": bits_per_second,
        "upload_bits_per_second": upload_bps,
        "download_bits_per_second": download_bps,
        "upload_bytes": upload_bytes,
        "download_bytes": download_bytes,
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


# Flag image cache - stores (image_bytes, timestamp)
_flag_cache: Dict[str, tuple[bytes, float]] = {}
FLAG_IMAGE_CACHE_TTL = 86400  # 24 hours


@app.get("/flags/{code}")
async def get_flag(code: str) -> Response:
    """Proxy and cache flag images from flagcdn.com."""
    # Validate country code (2 letter, lowercase)
    code = code.lower().replace(".png", "")
    if not code or len(code) != 2 or not code.isalpha():
        raise HTTPException(status_code=400, detail="Invalid country code")
    
    cache_key = code
    now = time.time()
    
    # Check cache
    cached = _flag_cache.get(cache_key)
    if cached and now - cached[1] < FLAG_IMAGE_CACHE_TTL:
        return Response(
            content=cached[0],
            media_type="image/png",
            headers={
                "Cache-Control": "public, max-age=86400",
                "X-Cache": "HIT"
            },
        )
    
    # Fetch from flagcdn.com
    url = f"https://flagcdn.com/24x18/{code}.png"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                image_data = resp.content
                # Cache the image
                _flag_cache[cache_key] = (image_data, now)
                return Response(
                    content=image_data,
                    media_type="image/png",
                    headers={
                        "Cache-Control": "public, max-age=86400",
                        "X-Cache": "MISS"
                    },
                )
            else:
                # Return fallback SVG if upstream fails
                return _get_fallback_flag(code)
    except httpx.RequestError as e:
        logger.warning(f"Failed to fetch flag for {code}: {e}")
        # Return fallback instead of error
        return _get_fallback_flag(code)


def _get_fallback_flag(code: str) -> Response:
    """Return a simple SVG placeholder when flag image cannot be fetched."""
    svg = f'''<svg width="24" height="18" xmlns="http://www.w3.org/2000/svg">
        <rect width="24" height="18" fill="#394150"/>
        <text x="12" y="13" font-family="Arial" font-size="10" fill="#9BA3AF" text-anchor="middle">{code.upper()}</text>
    </svg>'''
    return Response(
        content=svg,
        media_type="image/svg+xml",
        headers={
            "Cache-Control": "public, max-age=3600",
            "X-Cache": "FALLBACK"
        },
    )


def _is_authenticated(request: Request) -> bool:
    return dashboard_auth.is_authenticated(request)


def _set_auth_cookie(response: Response, password: str) -> None:
    dashboard_auth.set_auth_cookie(response, password)


def _is_guest(request: Request) -> bool:
    """Check if request is from a guest session."""
    return request.cookies.get("guest_session") == "readonly"




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
        ping_cycle_counter = 0  # Track cycles for ping storage (every 2 = 60s at 30s interval)
        while True:
            try:
                statuses = await self.refresh()
                await self._sync_ports(statuses)
                await self._check_alerts(statuses)  # Check for alerts
                
                # Store ping history every 60 seconds (every 2 cycles at 30s interval)
                ping_cycle_counter += 1
                if ping_cycle_counter >= 2:
                    ping_cycle_counter = 0
                    await self._store_ping_history(statuses)
                    await self._cleanup_old_pings()
            except Exception:
                logger.exception("Failed to refresh node health")
            await asyncio.sleep(self.interval_seconds)

    async def _check_alerts(self, statuses: List[NodeWithStatus]) -> None:
        """Check for alert conditions and trigger notifications.
        
        For offline nodes:
        - If node is offline and no OfflineMessage exists: send new card, create OfflineEvent
        - If node is offline and OfflineMessage exists: update card with new duration
        - If node is online and OfflineMessage exists: delete card, close OfflineEvent
        
        Daily statistics:
        - Track all offline events per day
        - Maintain a persistent daily stats card in Telegram
        """
        from .models import AlertConfig, OfflineMessage, OfflineEvent, DailyStatsMessage
        
        # GMT+8 Beijing timezone
        TZ_BEIJING = timezone(timedelta(hours=8))
        
        db = SessionLocal()
        try:
            thresholds_config = db.query(AlertConfig).filter(AlertConfig.key == "thresholds").first()
            telegram_config = db.query(AlertConfig).filter(AlertConfig.key == "telegram").first()
            
            # Default thresholds
            ping_threshold_ms = 500
            
            if thresholds_config and thresholds_config.value:
                ping_threshold_ms = thresholds_config.value.get("ping_high_ms", 500)
            
            # Check if telegram notifications are enabled
            notify_node_offline = False
            notify_ping_high = False
            bot_token = None
            chat_id = None
            
            if telegram_config and telegram_config.enabled and telegram_config.value:
                notify_node_offline = telegram_config.value.get("notify_node_offline", False)
                notify_ping_high = telegram_config.value.get("notify_ping_high", False)
                bot_token = telegram_config.value.get("bot_token")
                chat_id = telegram_config.value.get("chat_id")
            
            # Node filtering
            node_scope = "all"
            selected_nodes = set()
            if thresholds_config and thresholds_config.value:
                node_scope = thresholds_config.value.get("node_scope", "all")
                selected_nodes = set(thresholds_config.value.get("selected_nodes", []))
            
            # Today's date in Beijing timezone
            now_beijing = datetime.now(TZ_BEIJING)
            today_str = now_beijing.strftime("%Y-%m-%d")
            
            # Track current offline nodes for stats card
            current_offline_durations = {}  # {node_id: seconds_offline}
            
            for status in statuses:
                # Skip nodes not in selection (if using selected mode)
                if node_scope == "selected" and status.id not in selected_nodes:
                    continue
                
                # Handle offline nodes with card system
                if status.status == "offline":
                    # Check if we already have an offline message for this node
                    existing_msg = db.query(OfflineMessage).filter(
                        OfflineMessage.node_id == status.id
                    ).first()
                    
                    if existing_msg:
                        # Calculate current offline duration
                        duration = (datetime.now(timezone.utc) - existing_msg.offline_since).total_seconds()
                        current_offline_durations[status.id] = duration
                        
                        if notify_node_offline and bot_token and chat_id:
                            # Update existing card with new duration
                            await alert_service.edit_offline_card(
                                bot_token=bot_token,
                                chat_id=existing_msg.chat_id,
                                message_id=existing_msg.message_id,
                                node_name=status.name,
                                node_ip=status.ip,
                                offline_since=existing_msg.offline_since
                            )
                            # Update last_updated timestamp
                            existing_msg.last_updated = datetime.now(timezone.utc)
                            db.commit()
                    else:
                        # New offline event - create records
                        offline_since = datetime.now(timezone.utc)
                        current_offline_durations[status.id] = 0
                        
                        # Create OfflineEvent record
                        offline_event = OfflineEvent(
                            node_id=status.id,
                            node_name=status.name,
                            started_at=offline_since,
                            date=today_str
                        )
                        db.add(offline_event)
                        db.commit()
                        logger.info(f"Created offline event for node {status.name}")
                        
                        if notify_node_offline and bot_token and chat_id:
                            # Send new offline card
                            message_id = await alert_service.send_offline_card(
                                bot_token=bot_token,
                                chat_id=chat_id,
                                node_name=status.name,
                                node_ip=status.ip,
                                offline_since=offline_since
                            )
                            
                            if message_id:
                                # Save offline message record
                                offline_msg = OfflineMessage(
                                    node_id=status.id,
                                    message_id=message_id,
                                    chat_id=chat_id,
                                    offline_since=offline_since
                                )
                                db.add(offline_msg)
                                db.commit()
                                logger.info(f"Created offline message record for node {status.name}")
                
                # Handle online nodes - delete any existing offline card and close event
                elif status.status == "online":
                    existing_msg = db.query(OfflineMessage).filter(
                        OfflineMessage.node_id == status.id
                    ).first()
                    
                    if existing_msg:
                        # Close the OfflineEvent
                        open_event = db.query(OfflineEvent).filter(
                            OfflineEvent.node_id == status.id,
                            OfflineEvent.ended_at == None
                        ).first()
                        
                        if open_event:
                            open_event.ended_at = datetime.now(timezone.utc)
                            open_event.duration_seconds = int(
                                (open_event.ended_at - open_event.started_at).total_seconds()
                            )
                            db.commit()
                            logger.info(f"Closed offline event for node {status.name}, duration={open_event.duration_seconds}s")
                        
                        if bot_token:
                            # Delete the offline card from Telegram
                            await alert_service.delete_telegram_message(
                                bot_token=bot_token,
                                chat_id=existing_msg.chat_id,
                                message_id=existing_msg.message_id
                            )
                        
                        # Remove from database
                        db.delete(existing_msg)
                        db.commit()
                        logger.info(f"Deleted offline message for node {status.name} (now online)")
                
                # Check for high ping latency (keep existing behavior)
                if status.backbone_latency and notify_ping_high:
                    for lat in status.backbone_latency:
                        if lat.latency_ms and lat.latency_ms > ping_threshold_ms:
                            await trigger_alert(
                                db=db,
                                alert_type="ping_high",
                                severity="warning",
                                node_id=status.id,
                                node_name=status.name,
                                message=f"节点 {status.name} 的 {lat.name} 延迟过高: {lat.latency_ms}ms",
                                details={
                                    "carrier": lat.name,
                                    "current_value": f"{lat.latency_ms}ms",
                                    "threshold": f"{ping_threshold_ms}ms"
                                }
                            )
            
            # ========== Daily Statistics Card ==========
            if notify_node_offline and bot_token and chat_id:
                # Get all nodes for stats
                all_nodes = db.query(Node).all()
                
                # Calculate daily stats for each node
                node_stats = []
                for node in all_nodes:
                    # Skip if using selected mode and node not selected
                    if node_scope == "selected" and node.id not in selected_nodes:
                        continue
                    
                    # Query offline events for today
                    events = db.query(OfflineEvent).filter(
                        OfflineEvent.node_id == node.id,
                        OfflineEvent.date == today_str
                    ).all()
                    
                    offline_count = len(events)
                    total_duration = 0
                    
                    for event in events:
                        if event.duration_seconds:
                            total_duration += event.duration_seconds
                        elif event.ended_at is None:
                            # Still offline - calculate current duration
                            total_duration += int((datetime.now(timezone.utc) - event.started_at).total_seconds())
                    
                    node_stats.append({
                        "node_id": node.id,
                        "node_name": node.name,
                        "offline_count": offline_count,
                        "total_duration": total_duration
                    })
                
                # Check for existing daily stats message
                stats_msg = db.query(DailyStatsMessage).filter(
                    DailyStatsMessage.date == today_str
                ).first()
                
                # Check if there's a message from a previous day (date changed)
                old_stats_msg = db.query(DailyStatsMessage).filter(
                    DailyStatsMessage.date != today_str
                ).first()
                
                if old_stats_msg:
                    # Date has changed - archive the old day
                    old_date = old_stats_msg.date
                    
                    # Calculate stats for the old day (for archive)
                    old_node_stats = []
                    for node in all_nodes:
                        if node_scope == "selected" and node.id not in selected_nodes:
                            continue
                        events = db.query(OfflineEvent).filter(
                            OfflineEvent.node_id == node.id,
                            OfflineEvent.date == old_date
                        ).all()
                        offline_count = len(events)
                        total_duration = sum(e.duration_seconds or 0 for e in events)
                        old_node_stats.append({
                            "node_id": node.id,
                            "node_name": node.name,
                            "offline_count": offline_count,
                            "total_duration": total_duration
                        })
                    
                    # Send archive card (this persists in chat)
                    await alert_service.send_daily_archive_card(
                        bot_token=bot_token,
                        chat_id=old_stats_msg.chat_id,
                        date_str=old_date,
                        node_stats=old_node_stats
                    )
                    logger.info(f"Sent daily archive for {old_date}")
                    
                    # Delete the old live stats message from Telegram
                    await alert_service.delete_telegram_message(
                        bot_token=bot_token,
                        chat_id=old_stats_msg.chat_id,
                        message_id=old_stats_msg.message_id
                    )
                    
                    # Remove old record from database
                    db.delete(old_stats_msg)
                    db.commit()
                    logger.info(f"Deleted old live stats card for {old_date}")
                
                if stats_msg:
                    # Update existing card
                    await alert_service.edit_daily_stats_card(
                        bot_token=bot_token,
                        chat_id=stats_msg.chat_id,
                        message_id=stats_msg.message_id,
                        date_str=today_str,
                        node_stats=node_stats,
                        current_offline=current_offline_durations
                    )
                    stats_msg.last_updated = datetime.now(timezone.utc)
                    db.commit()
                else:
                    # Send new daily stats card
                    message_id = await alert_service.send_daily_stats_card(
                        bot_token=bot_token,
                        chat_id=chat_id,
                        date_str=today_str,
                        node_stats=node_stats,
                        current_offline=current_offline_durations
                    )
                    
                    if message_id:
                        stats_msg = DailyStatsMessage(
                            date=today_str,
                            message_id=message_id,
                            chat_id=chat_id
                        )
                        db.add(stats_msg)
                        db.commit()
                        logger.info(f"Created daily stats message for {today_str}")
                        
        except Exception as e:
            logger.error(f"Failed to check alerts: {e}")
            import traceback
            traceback.print_exc()
        finally:
            db.close()

    async def _store_ping_history(self, statuses: List[NodeWithStatus]) -> None:
        """Store backbone latency data to PingHistory table for trend analysis."""
        from .models import PingHistory
        
        print(f"[PING] Storing ping history for {len(statuses)} nodes", flush=True)
        db = SessionLocal()
        records_added = 0
        try:
            for status in statuses:
                if not status.backbone_latency:
                    print(f"[PING] Node {status.id} ({status.name}): no backbone_latency", flush=True)
                    continue
                    
                for lat in status.backbone_latency:
                    if lat.latency_ms is None:
                        continue
                    
                    # Map carrier key to standard name
                    carrier_map = {
                        "cu": "CU", "unicom": "CU", "zj_cu": "CU",
                        "cm": "CM", "mobile": "CM", "cmcc": "CM", "zj_cm": "CM",
                        "ct": "CT", "telecom": "CT", "chinanet": "CT", "zj_ct": "CT",
                    }
                    carrier = carrier_map.get(lat.key.lower(), lat.key.upper())
                    
                    ping_record = PingHistory(
                        node_id=status.id,
                        carrier=carrier,
                        latency_ms=int(lat.latency_ms),
                        sample_count=1,
                    )
                    db.add(ping_record)
                    records_added += 1
            
            db.commit()
            print(f"[PING] Stored {records_added} ping records", flush=True)
        except Exception as e:
            logger.error(f"Failed to store ping history: {e}")
            db.rollback()
        finally:
            db.close()

    async def _cleanup_old_pings(self) -> None:
        """Delete ping history older than 24 hours."""
        from .models import PingHistory
        
        db = SessionLocal()
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
            deleted = db.query(PingHistory).filter(PingHistory.recorded_at < cutoff).delete()
            if deleted > 0:
                logger.info(f"Cleaned up {deleted} old ping records (>24h)")
            db.commit()
        except Exception as e:
            logger.error(f"Failed to cleanup old pings: {e}")
            db.rollback()
        finally:
            db.close()

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
    
    # Auto-sync whitelist with master IP on startup
    try:
        db = SessionLocal()
        result = await _sync_whitelist_to_agents(db, force=True)
        logger.info(f"[STARTUP] Whitelist sync: {result.get('success', 0)} agents synced, {result.get('failed', 0)} failed")
        db.close()
    except Exception as e:
        logger.error(f"[STARTUP] Whitelist sync failed: {e}")


@app.on_event("shutdown")
async def _on_shutdown() -> None:
    await health_monitor.stop()
    await backbone_monitor.stop()


@app.get("/geo")
async def geo_lookup(ip: str = Query(..., description="IP 地址")) -> dict:
    info = await lookup_geo_info(ip)
    if info:
        return {"country_code": info.get("country_code"), "isp": info.get("isp")}
    return {"country_code": None, "isp": None}


async def lookup_geo_info(ip: str) -> dict | None:
    now = time.time()
    cached = _geo_cache.get(ip)
    if cached and now - cached[1] < GEO_CACHE_TTL_SECONDS:
        return cached[0]

    resolved_ip, cache_key = await _resolve_geo_ip(ip)
    if not resolved_ip:
        _geo_cache[cache_key] = (None, now)
        return None

    def _parse_asn_from_as_string(as_str: str) -> int | None:
        """Extract ASN number from 'AS1234 Company Name' format."""
        if not as_str:
            return None
        import re
        match = re.match(r'^AS(\d+)', as_str, re.IGNORECASE)
        return int(match.group(1)) if match else None

    async def _fetch_from_ipapi(client: httpx.AsyncClient, target_ip: str) -> dict | None:
        try:
            resp = await client.get(f"https://ipapi.co/{target_ip}/json/")
            if resp.status_code == 200:
                data = resp.json()
                code = data.get("country_code")
                isp = data.get("org") or data.get("asn") 
                asn = _parse_asn_from_as_string(data.get("asn"))  # ipapi.co returns asn as "AS1234"
                if code:
                    return {"country_code": code.upper(), "isp": isp, "asn": asn}
        except Exception:
            pass
        return None

    async def _fetch_from_ip_api(client: httpx.AsyncClient, target_ip: str) -> dict | None:
        try:
            resp = await client.get(
                f"http://ip-api.com/json/{target_ip}?fields=status,countryCode,isp,org,as,message"
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("status") == "success":
                    code = data.get("countryCode")
                    isp = data.get("isp") or data.get("org")
                    as_str = data.get("as") or ""  # format: "AS1234 Company Name"
                    asn = _parse_asn_from_as_string(as_str)
                    if isinstance(code, str) and len(code) == 2:
                        return {"country_code": code.upper(), "isp": isp, "asn": asn}
        except Exception:
            pass
        return None

    try:
        async with httpx.AsyncClient(timeout=5) as client:
            for fetcher in (
                lambda c: _fetch_from_ip_api(c, resolved_ip), # ip-api.com usually has better ISP names
                lambda c: _fetch_from_ipapi(c, resolved_ip),
            ):
                result = await fetcher(client)
                if result:
                    _geo_cache[cache_key] = (result, now)
                    if cache_key != resolved_ip:
                        _geo_cache[resolved_ip] = (result, now)
                    return result
    except Exception:  # pragma: no cover - external dependency
        logger.exception("Failed to lookup geo info for %s", ip)

    _geo_cache[cache_key] = (None, now)
    return None




# --- SALVAGED CODE SPLICE 1 ---

@app.get("/auth/status")
def auth_status(request: Request) -> dict:
    return {
        "authenticated": _is_authenticated(request),
        "isGuest": _is_guest(request)
    }


# ============================================================================
# Login Rate Limiting
# ============================================================================

class LoginRateLimiter:
    """Simple IP-based rate limiter for login attempts."""
    
    def __init__(self, max_attempts: int = 5, window_seconds: int = 300):
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._attempts: Dict[str, list] = {}  # IP -> list of timestamps
        self._lock = threading.Lock()
    
    def _cleanup(self, ip: str, now: float):
        """Remove expired attempts."""
        if ip in self._attempts:
            self._attempts[ip] = [t for t in self._attempts[ip] if now - t < self.window_seconds]
            if not self._attempts[ip]:
                del self._attempts[ip]
    
    def is_rate_limited(self, ip: str) -> tuple[bool, int]:
        """
        Check if IP is rate limited.
        Returns: (is_limited, seconds_until_reset)
        """
        now = time.time()
        with self._lock:
            self._cleanup(ip, now)
            attempts = self._attempts.get(ip, [])
            if len(attempts) >= self.max_attempts:
                oldest = min(attempts)
                seconds_left = int(self.window_seconds - (now - oldest)) + 1
                return True, seconds_left
            return False, 0
    
    def record_attempt(self, ip: str):
        """Record a login attempt."""
        now = time.time()
        with self._lock:
            self._cleanup(ip, now)
            if ip not in self._attempts:
                self._attempts[ip] = []
            self._attempts[ip].append(now)
    
    def clear(self, ip: str):
        """Clear attempts for an IP after successful login."""
        with self._lock:
            self._attempts.pop(ip, None)


import threading
_login_limiter = LoginRateLimiter(max_attempts=5, window_seconds=300)


def _get_client_ip(request: Request) -> str:
    """Get client IP from request, handling proxies."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    real_ip = request.headers.get("x-real-ip")
    if real_ip:
        return real_ip
    return request.client.host if request.client else "unknown"


@app.post("/auth/login")
def login(request: Request, response: Response, payload: dict = Body(...), db: Session = Depends(get_db)) -> dict:
    client_ip = _get_client_ip(request)
    
    # Check rate limiting
    is_limited, wait_seconds = _login_limiter.is_rate_limited(client_ip)
    if is_limited:
        audit_log(db, "login_rate_limited", actor_ip=client_ip, success=False,
                  details={"wait_seconds": wait_seconds})
        raise HTTPException(
            status_code=429, 
            detail=f"rate_limited:请等待 {wait_seconds} 秒后重试"
        )
    
    raw_password = payload.get("password")
    if raw_password is None or not str(raw_password).strip():
        raise HTTPException(status_code=400, detail="empty_password")

    # Record attempt before checking password
    _login_limiter.record_attempt(client_ip)
    
    if not auth_manager().verify_password(raw_password):
        audit_log(db, "login_failed", actor_ip=client_ip, success=False)
        raise HTTPException(status_code=401, detail="invalid_password")

    # Clear rate limit on successful login
    _login_limiter.clear(client_ip)
    audit_log(db, "login_success", actor_ip=client_ip, success=True)
    _set_auth_cookie(response, str(raw_password))
    return {"status": "ok"}


@app.post("/auth/guest")
def guest_login(response: Response) -> dict:
    """Guest login - sets a guest session cookie for read-only access."""
    response.set_cookie(
        key="guest_session",
        value="readonly",
        httponly=True,
        max_age=86400 * 7,  # 7 days
        samesite="lax",
        secure=False,
    )
    return {"status": "ok", "mode": "guest"}


@app.post("/auth/logout")
def logout(response: Response) -> dict:
    response.delete_cookie(settings.dashboard_cookie_name)
    response.delete_cookie("guest_session")  # Also clear guest session
    return {"status": "logged_out"}


@app.get("/api/audit-logs")
def get_audit_logs(
    request: Request,
    action: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db)
) -> dict:
    """Get audit logs (admin only)."""
    if not auth_manager().is_authenticated(request):
        raise HTTPException(status_code=401, detail="unauthorized")
    
    query = select(AuditLog).order_by(AuditLog.timestamp.desc())
    
    if action:
        query = query.where(AuditLog.action == action)
    
    query = query.offset(offset).limit(limit)
    logs = db.scalars(query).all()
    
    # Get total count
    count_query = select(func.count(AuditLog.id))
    if action:
        count_query = count_query.where(AuditLog.action == action)
    total = db.scalar(count_query) or 0
    
    return {
        "logs": [
            {
                "id": log.id,
                "timestamp": log.timestamp.isoformat() if log.timestamp else None,
                "action": log.action,
                "actor_ip": log.actor_ip,
                "actor_type": log.actor_type,
                "resource_type": log.resource_type,
                "resource_id": log.resource_id,
                "details": log.details,
                "success": log.success
            }
            for log in logs
        ],
        "total": total,
        "limit": limit,
        "offset": offset
    }


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
    checked_at = int(datetime.now(timezone.utc).timestamp())
    
    # For NAT/reverse mode nodes, check heartbeat instead of HTTP
    node_mode = getattr(node, "agent_mode", "normal") or "normal"
    
    # Debug: Print to stdout for guaranteed visibility
    last_heartbeat = getattr(node, "last_heartbeat", None)
    print(f"[HEALTH] Checking {node.name}: agent_mode='{node_mode}', last_heartbeat={last_heartbeat}", flush=True)
    
    if node_mode == "reverse":
        agent_version = getattr(node, "agent_version", None)
        
        if last_heartbeat:
            # Handle timezone-aware vs naive datetime comparison
            now = datetime.now(timezone.utc)
            if last_heartbeat.tzinfo is None:
                # Naive datetime from SQLite - assume it's UTC
                last_heartbeat = last_heartbeat.replace(tzinfo=timezone.utc)
            
            heartbeat_age = (now - last_heartbeat).total_seconds()
            logger.info(f"[HEALTH] {node.name}: heartbeat_age={heartbeat_age:.1f}s (threshold=60s)")
            
            # Check if heartbeat is within 60 seconds (online threshold)
            if heartbeat_age < 60:
                logger.info(f"[HEALTH] {node.name}: ONLINE (reverse mode, heartbeat fresh)")
                return NodeWithStatus(
                    id=node.id,
                    name=node.name,
                    ip=node.ip,
                    agent_port=node.agent_port,
                    description=node.description,
                    iperf_port=node.iperf_port,
                    status="online",
                    server_running=True,  # Assume server is running for reverse agents
                    health_timestamp=int(last_heartbeat.timestamp()),
                    checked_at=checked_at,
                    detected_iperf_port=node.iperf_port,
                    detected_agent_port=node.agent_port,
                    backbone_latency=None,
                    streaming=None,
                    streaming_checked_at=None,
                    whitelist_sync_status=getattr(node, "whitelist_sync_status", "unknown"),
                    whitelist_sync_message=getattr(node, "whitelist_sync_message", None),
                    whitelist_sync_at=getattr(node, "whitelist_sync_at", None),
                    agent_version=agent_version,
                    agent_mode=node_mode,
                )
        
        # NAT node is offline (no recent heartbeat)
        return NodeWithStatus(
            id=node.id,
            name=node.name,
            ip=node.ip,
            agent_port=node.agent_port,
            iperf_port=node.iperf_port,
            description=node.description,
            status="offline",
            server_running=None,
            health_timestamp=int(last_heartbeat.timestamp()) if last_heartbeat else None,
            checked_at=checked_at,
            detected_iperf_port=None,
            detected_agent_port=None,
            whitelist_sync_status=getattr(node, "whitelist_sync_status", "unknown"),
            whitelist_sync_at=getattr(node, "whitelist_sync_at", None),
            agent_version=agent_version,
            agent_mode=node_mode,
        )
    
    # Normal nodes - check via HTTP health endpoint
    url = f"http://{node.ip}:{node.agent_port}/health"
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
                    whitelist_sync_status=getattr(node, "whitelist_sync_status", "unknown"),
                    whitelist_sync_message=getattr(node, "whitelist_sync_message", None),
                    whitelist_sync_at=getattr(node, "whitelist_sync_at", None),
                    agent_version=data.get("version"),
                    agent_mode=node_mode,
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
        whitelist_sync_status=getattr(node, "whitelist_sync_status", "unknown"),
        whitelist_sync_at=getattr(node, "whitelist_sync_at", None),
        agent_mode=node_mode,
    )



# --- Reverse Agent Registration (for internal/NAT devices) ---
from pydantic import BaseModel

class AgentRegistration(BaseModel):
    node_name: str
    iperf_port: int = 5201
    master_url: str = ""  # For reference
    agent_version: str = ""

# In-memory task queue for internal agents
_internal_agent_tasks: dict[str, list[dict]] = {}  # node_name -> list of pending tasks
_task_results: dict[int, dict] = {}  # task_id -> result
_task_id_counter = 0

# NOTE: Old register_reverse_agent endpoint removed - now using agent_register at line ~8248
# which properly handles agent_mode, last_heartbeat, and whitelist sync

@app.get("/api/agent/tasks")
async def get_agent_tasks(node_name: str, db: Session = Depends(get_db)):
    """
    Get pending tasks for an internal agent.
    Returns tasks from BOTH in-memory queue AND PendingTask database table.
    """
    global _internal_agent_tasks
    
    # 1. Get tasks from in-memory queue (for immediate tests via create_test)
    memory_tasks = _internal_agent_tasks.get(node_name, [])
    if memory_tasks:
        _internal_agent_tasks[node_name] = []  # Clear after returning
    
    # 2. Get tasks from PendingTask database table (for scheduled tests)
    db_tasks = []
    try:
        pending_db_tasks = db.scalars(
            select(PendingTask).where(
                PendingTask.node_name == node_name,
                PendingTask.status == "pending"
            )
        ).all()
        
        for pt in pending_db_tasks:
            # Build task dict matching what agent expects
            task_dict = {
                "id": pt.id,
                "task_type": pt.task_type,  # Must be "task_type" not "type" for agent
                "task_data": pt.task_data,  # Keep task_data as nested object
                **pt.task_data,  # Also spread for backward compatibility
            }
            db_tasks.append(task_dict)
            
            # Mark as claimed so it's not picked up again
            pt.status = "claimed"
            pt.claimed_at = datetime.now(timezone.utc)
        
        if db_tasks:
            db.commit()
            print(f"[AGENT-TASKS] Delivered {len(db_tasks)} DB tasks to {node_name}", flush=True)
    except Exception as e:
        print(f"[AGENT-TASKS] Error getting DB tasks for {node_name}: {e}", flush=True)
    
    # Combine both sources
    all_tasks = memory_tasks + db_tasks
    
    # Get whitelist from database (Node IPs + Master IP)
    whitelist_ips = []
    try:
        # Get all node IPs as whitelist (same logic as _sync_whitelist_to_agents)
        nodes = db.scalars(select(Node)).all()
        whitelist_ips = [n.ip for n in nodes]
        
        # Add Master IP if configured
        master_ip = os.getenv("MASTER_IP", "")
        if master_ip and master_ip not in whitelist_ips:
            whitelist_ips.append(master_ip)
        
        # Update node's whitelist sync status
        node = db.scalars(select(Node).where(Node.name == node_name)).first()
        if node:
            node.whitelist_sync_status = "synced"
            node.whitelist_sync_at = datetime.now(timezone.utc)
            db.commit()
    except Exception as e:
        print(f"[TASKS] Error getting whitelist: {e}", flush=True)
    
    return {"status": "ok", "tasks": all_tasks, "whitelist": whitelist_ips}



class TaskResult(BaseModel):
    node_name: str
    result: dict

@app.post("/api/agent/tasks/{task_id}/result")
async def submit_task_result(task_id: int, result: TaskResult):
    """
    Receive task result from internal agent (alternative endpoint).
    """
    global _task_results
    _task_results[task_id] = {
        "node_name": result.node_name,
        "result": result.result,
        "received_at": datetime.now(timezone.utc).isoformat()
    }
    return {"status": "ok", "task_id": task_id}


class AgentResultPayload(BaseModel):
    """Payload for agent result reporting - matches what agent sends."""
    task_id: int
    result: dict


@app.post("/api/agent/result")
async def receive_agent_result(payload: AgentResultPayload, db: Session = Depends(get_db)):
    """
    Receive task result from reverse mode (NAT) agent.
    Updates PendingTask in DB and creates TestResult/ScheduleResult.
    """
    global _task_results
    task_id = payload.task_id
    result = payload.result
    
    # Store in memory for legacy callers
    _task_results[task_id] = {
        "result": result,
        "received_at": datetime.now(timezone.utc).isoformat()
    }
    
    # Update PendingTask in database
    pending_task = db.scalars(
        select(PendingTask).where(PendingTask.id == task_id)
    ).first()
    
    if pending_task:
        pending_task.status = "completed" if result.get("status") == "ok" else "failed"
        pending_task.completed_at = datetime.now(timezone.utc)
        pending_task.result_data = result
        if result.get("status") != "ok":
            pending_task.error_message = result.get("error", "Unknown error")
        
        schedule_id = pending_task.schedule_id
        logger.info(f"[REVERSE-RESULT] Task {task_id} completed, schedule_id={schedule_id}, status={result.get('status')}")
        print(f"[REVERSE-RESULT] Task {task_id} completed, schedule_id={schedule_id}, status={result.get('status')}", flush=True)
        
        # If this task is from a schedule, update the ScheduleResult
        if schedule_id:
            try:
                # Find the pending schedule result for this task
                schedule_result = db.scalars(
                    select(ScheduleResult).where(
                        ScheduleResult.schedule_id == schedule_id,
                        ScheduleResult.status == "pending",
                        ScheduleResult.error_message.like(f"Queued as task #{task_id}%")
                    )
                ).first()
                
                if result.get("status") == "ok" and result.get("iperf_result"):
                    # Parse iperf result and create TestResult
                    iperf_data = result["iperf_result"]
                    
                    # Get schedule info for src/dst nodes
                    schedule = db.scalars(select(TestSchedule).where(TestSchedule.id == schedule_id)).first()
                    if schedule:
                        # Get direction_label from task_data for proper UDP direction assignment
                        direction_label = pending_task.task_data.get("direction_label")
                        summary = _summarize_metrics({"iperf_result": iperf_data} if iperf_data else result, direction_label=direction_label)
                        test_result = TestResult(
                            src_node_id=schedule.src_node_id,
                            dst_node_id=schedule.dst_node_id,
                            protocol=pending_task.task_data.get("protocol", "tcp"),
                            params=pending_task.task_data,
                            raw_result=iperf_data,
                            summary=summary,
                        )
                        db.add(test_result)
                        db.flush()
                        
                        if schedule_result:
                            schedule_result.status = "success"
                            schedule_result.test_result_id = test_result.id
                            schedule_result.error_message = None
                        else:
                            # Create new schedule result if not found
                            schedule_result = ScheduleResult(
                                schedule_id=schedule_id,
                                test_result_id=test_result.id,
                                status="success",
                            )
                            db.add(schedule_result)
                        
                        print(f"[REVERSE-RESULT] Created TestResult #{test_result.id} for schedule {schedule_id}", flush=True)
                else:
                    # Test failed
                    if schedule_result:
                        schedule_result.status = "error"
                        schedule_result.error_message = result.get("error", "Unknown error")
                    else:
                        schedule_result = ScheduleResult(
                            schedule_id=schedule_id,
                            status="error",
                            error_message=result.get("error", "Unknown error"),
                        )
                        db.add(schedule_result)
                
                db.commit()
            except Exception as e:
                logger.error(f"[REVERSE-RESULT] Error updating ScheduleResult: {e}")
                print(f"[REVERSE-RESULT] Error updating ScheduleResult: {e}", flush=True)
        else:
            db.commit()
    else:
        # This is expected for tasks queued via queue_task_for_internal_agent()
        # which uses memory-only task queue without PendingTask DB records
        logger.info(f"[REVERSE-RESULT] Task {task_id} result received (memory-only task, no DB record)")
    
    return {"status": "ok", "task_id": task_id}


def queue_task_for_internal_agent(node_name: str, task: dict) -> int:
    """
    Queue a test task for an internal agent.
    Returns the task ID.
    """
    global _internal_agent_tasks, _task_id_counter
    _task_id_counter += 1
    task["id"] = _task_id_counter
    
    if node_name not in _internal_agent_tasks:
        _internal_agent_tasks[node_name] = []
    _internal_agent_tasks[node_name].append(task)
    
    return _task_id_counter


async def _call_reverse_agent_test(src: Node, payload: dict, duration: int) -> dict:
    """
    Execute test via reverse mode (NAT) agent using task queue.
    
    The NAT agent polls for tasks, so we:
    1. Queue the task
    2. Wait for agent to pick it up and report result
    3. Return the result
    """
    global _task_results
    
    # Prepare task for the reverse agent
    task = {
        "type": "iperf_test",
        "target_ip": payload.get("target"),
        "target_port": payload.get("port"),
        "duration": payload.get("duration", 10),
        "protocol": payload.get("protocol", "tcp"),
        "parallel": payload.get("parallel", 1),
        "reverse": payload.get("reverse", False),
        "bandwidth": payload.get("bandwidth"),
    }
    
    task_id = queue_task_for_internal_agent(src.name, task)
    logger.info(f"[REVERSE-TEST] Queued task {task_id} for {src.name}: {task.get('target_ip')}")
    
    # Wait for result with timeout
    # Agent polls every ~10 seconds, so we need to wait long enough
    # Account for: polling delay (up to 15s) + test duration + result reporting
    poll_interval = 2  # seconds
    max_wait = duration + 60  # Generous timeout for NAT agents
    waited = 0
    
    while waited < max_wait:
        await asyncio.sleep(poll_interval)
        waited += poll_interval
        
        # Check if result is available
        if task_id in _task_results:
            result_data = _task_results.pop(task_id)
            result = result_data.get("result", {})
            logger.info(f"[REVERSE-TEST] Received result for task {task_id}: status={result.get('status')}")
            logger.info(f"[REVERSE-TEST] Result keys: {list(result.keys()) if isinstance(result, dict) else 'not a dict'}")
            
            if result.get("status") == "ok":
                # Return in same format as direct agent call
                iperf_result = result.get("iperf_result", {})
                logger.info(f"[REVERSE-TEST] Returning iperf_result with {len(iperf_result) if isinstance(iperf_result, dict) else 0} keys")
                return {"status": "ok", "iperf_result": iperf_result}
            else:
                error_msg = result.get("error", "Unknown error from reverse agent")
                logger.warning(f"[REVERSE-TEST] Task {task_id} failed: {error_msg}")
                raise HTTPException(status_code=502, detail=f"reverse agent test failed: {error_msg}")
        
        if waited % 10 == 0:  # Log every 10 seconds
            logger.info(f"[REVERSE-TEST] Waiting for task {task_id}, {waited}s/{max_wait}s")
    
    # Timeout - clean up task if still pending
    if src.name in _internal_agent_tasks:
        _internal_agent_tasks[src.name] = [t for t in _internal_agent_tasks[src.name] if t.get("id") != task_id]
    
    raise HTTPException(
        status_code=504, 
        detail=f"Timeout waiting for reverse agent {src.name} to complete test (waited {max_wait}s)"
    )


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
    
    return obj


@app.get("/nodes", response_model=List[NodeRead])
def list_nodes(db: Session = Depends(get_db)):
    # Try cache first
    from .redis_client import cache_get, cache_set
    cache_key = "nodes:list"
    
    cached = cache_get(cache_key)
    if cached is not None:
        logger.debug(f"Cache HIT: {cache_key}")
        return cached
    
    # Cache miss - query database
    logger.debug(f"Cache MISS: {cache_key}")
    nodes = db.scalars(select(Node)).all()
    nodes_list = [NodeRead.model_validate(n) for n in nodes]
    
    # Cache for 60 seconds
    cache_set(cache_key, [n.model_dump() for n in nodes_list], ttl=60)
    
    return nodes_list


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
    
    # Invalidate nodes cache
    from .redis_client import cache_delete
    cache_delete("nodes:list")
    return node


@app.delete("/nodes/{node_id}")
def delete_node(node_id: int, db: Session = Depends(get_db)):
    from .models import TestSchedule, TraceSchedule, TraceResult, PingHistory, ScheduleResult
    from sqlalchemy import update
    
    node = db.get(Node, node_id)
    if not node:
        raise HTTPException(status_code=404, detail="node not found")

    # First, get test result IDs to delete related ScheduleResult records
    related_test_ids = db.scalars(
        select(TestResult.id).where(
            or_(
                TestResult.src_node_id == node_id,
                TestResult.dst_node_id == node_id,
            )
        )
    ).all()
    
    # Delete ScheduleResult records that reference these test results
    if related_test_ids:
        db.execute(
            ScheduleResult.__table__.delete().where(
                ScheduleResult.test_result_id.in_(related_test_ids)
            )
        )
    
    # Delete related TestResult records
    db.execute(
        TestResult.__table__.delete().where(
            or_(
                TestResult.src_node_id == node_id,
                TestResult.dst_node_id == node_id,
            )
        )
    )
    
    # Delete related TestSchedule records
    related_schedules = db.scalars(
        select(TestSchedule).where(
            or_(
                TestSchedule.src_node_id == node_id,
                TestSchedule.dst_node_id == node_id,
            )
        )
    ).all()
    for sched in related_schedules:
        # Remove from scheduler
        try:
            scheduler.remove_job(f"schedule_{sched.id}")
        except:
            pass
        # Delete related ScheduleResult records for this schedule
        db.execute(
            ScheduleResult.__table__.delete().where(ScheduleResult.schedule_id == sched.id)
        )
        db.delete(sched)
    
    # Delete related TraceSchedule records
    related_trace_scheds = db.scalars(
        select(TraceSchedule).where(
            or_(
                TraceSchedule.src_node_id == node_id,
                TraceSchedule.target_node_id == node_id,
            )
        )
    ).all()
    for ts in related_trace_scheds:
        # Remove from scheduler
        try:
            scheduler.remove_job(f"trace_{ts.id}")
        except:
            pass
        # Unlink trace results before deleting schedule
        db.execute(
            update(TraceResult).where(TraceResult.schedule_id == ts.id).values(schedule_id=None)
        )
        db.delete(ts)
    
    # Delete TraceResult records from this node (src_node_id is NOT NULL, so can't nullify)
    db.execute(
        TraceResult.__table__.delete().where(TraceResult.src_node_id == node_id)
    )
    
    # Delete related PingHistory records
    db.execute(
        PingHistory.__table__.delete().where(PingHistory.node_id == node_id)
    )

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


# ============== Traceroute API ==============

class TracerouteRequest(BaseModel):
    target: str
    max_hops: int = 30
    include_geo: bool = True


class TracerouteHop(BaseModel):
    hop: int
    ip: str
    rtt_avg: Optional[float] = None
    rtt_best: Optional[float] = None
    rtt_worst: Optional[float] = None
    loss_pct: Optional[float] = None
    geo: Optional[dict] = None


class TracerouteResponse(BaseModel):
    status: str
    target: str
    source_node_id: int
    source_node_name: str
    total_hops: int
    hops: list
    route_hash: str
    tool_used: str
    elapsed_ms: int


@app.post("/api/trace/run", response_model=TracerouteResponse)
async def run_traceroute(
    node_id: int, 
    req: TracerouteRequest, 
    save_result: bool = Query(False, description="Save result to history"),
    source_type: str = Query("single", description="Source type: single, multisrc, scheduled"),
    db: Session = Depends(get_db)
):
    """Execute traceroute from specified node to target."""
    node = db.get(Node, node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Node not found")
    
    # Check if this is a reverse agent
    is_reverse = node.agent_mode == "reverse"
    
    if is_reverse:
        # For reverse agents, create a pending task and wait for result
        response = await _run_traceroute_via_task_queue(node, req, db)
    else:
        # For normal agents, check online and call directly
        node_status = await health_monitor.check_node(node)
        if node_status.status != "online":
            raise HTTPException(status_code=503, detail=f"Node {node.name} is offline")
        
        response = await _run_traceroute_direct(node, req)
    
    # Save to history if requested
    if save_result and response.status == "ok":
        try:
            trace_result = TraceResult(
                src_node_id=node.id,
                target=req.target,
                total_hops=response.total_hops,
                hops=response.hops,
                route_hash=response.route_hash,
                tool_used=response.tool_used,
                elapsed_ms=response.elapsed_ms,
                source_type=source_type,
                has_change=False,
            )
            db.add(trace_result)
            db.commit()
            logger.info(f"[TRACE] Saved {source_type} trace result: {node.name} -> {req.target}")
        except Exception as e:
            logger.error(f"[TRACE] Failed to save result: {e}")
    
    return response


async def _run_traceroute_direct(node: Node, req: TracerouteRequest) -> TracerouteResponse:
    """Execute traceroute by directly calling the agent."""
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            response = await client.post(
                f"http://{node.ip}:{node.agent_port}/traceroute",
                json={
                    "target": req.target,
                    "max_hops": req.max_hops,
                    "include_geo": req.include_geo,
                }
            )
            
            if response.status_code != 200:
                raise HTTPException(status_code=response.status_code, detail="Agent traceroute failed")
            
            result = response.json()
            
            return TracerouteResponse(
                status="ok",
                target=req.target,
                source_node_id=node.id,
                source_node_name=node.name,
                total_hops=result.get("total_hops", 0),
                hops=result.get("hops", []),
                route_hash=result.get("route_hash", ""),
                tool_used=result.get("tool_used", "unknown"),
                elapsed_ms=result.get("elapsed_ms", 0),
            )
            
    except httpx.TimeoutException:
        raise HTTPException(status_code=504, detail="Traceroute timed out")
    except Exception as e:
        logger.error(f"Traceroute error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


async def _run_traceroute_via_task_queue(node: Node, req: TracerouteRequest, db: Session) -> TracerouteResponse:
    """Execute traceroute for reverse agent via task queue (agent polls for task)."""
    from datetime import timezone
    
    # Create pending task
    task = PendingTask(
        node_name=node.name,
        task_type="traceroute",
        task_data={
            "target": req.target,
            "max_hops": req.max_hops,
            "include_geo": req.include_geo,
        },
        status="pending",
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    
    task_id = task.id
    logger.info(f"Created traceroute task {task_id} for reverse agent {node.name}")
    
    # Poll for result (agent will pick up task and submit result)
    max_wait = 180  # 3 minutes max wait
    poll_interval = 2
    elapsed = 0
    
    print(f"[TRACE-QUEUE] Created task {task_id}, waiting for result (max {max_wait}s)...", flush=True)
    
    while elapsed < max_wait:
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval
        
        # Check task status - expire cache to get fresh data from DB
        db.expire_all()  # Clear SQLAlchemy cache
        task = db.get(PendingTask, task_id)
        
        if not task:
            raise HTTPException(status_code=500, detail="Task disappeared")
        
        # Log status every 10 seconds
        if elapsed % 10 == 0:
            print(f"[TRACE-QUEUE] Task {task_id} status: {task.status} (elapsed: {elapsed}s)", flush=True)
        
        if task.status == "completed" and task.result_data:
            result = task.result_data
            print(f"[TRACE-QUEUE] Task {task_id} completed with {result.get('total_hops', 0)} hops", flush=True)
            
            # Clean up task
            db.delete(task)
            db.commit()
            
            return TracerouteResponse(
                status="ok",
                target=req.target,
                source_node_id=node.id,
                source_node_name=node.name,
                total_hops=result.get("total_hops", 0),
                hops=result.get("hops", []),
                route_hash=result.get("route_hash", ""),
                tool_used=result.get("tool_used", "unknown"),
                elapsed_ms=result.get("elapsed_ms", 0),
            )
        
        if task.status == "failed":
            error_msg = task.error_message or "Traceroute failed"
            db.delete(task)
            db.commit()
            raise HTTPException(status_code=500, detail=error_msg)
    
    # Timeout - mark task as expired
    print(f"[TRACE-QUEUE] Task {task_id} TIMEOUT after {elapsed}s (last status: {task.status})", flush=True)
    task.status = "expired"
    db.commit()
    raise HTTPException(status_code=504, detail=f"Traceroute timed out waiting for reverse agent {node.name}")

# ============== Telegram Settings API ==============

import json
from pathlib import Path

TG_SETTINGS_FILE = Path(__file__).resolve().parent.parent / "data" / "telegram_settings.json"

def _load_tg_settings() -> dict:
    """Load Telegram settings from file."""
    if not TG_SETTINGS_FILE.exists():
        return {"bot_token": "", "chat_id": ""}
    try:
        with TG_SETTINGS_FILE.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {"bot_token": "", "chat_id": ""}

def _save_tg_settings(bot_token: str, chat_id: str) -> None:
    """Save Telegram settings to file."""
    TG_SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with TG_SETTINGS_FILE.open("w", encoding="utf-8") as f:
        json.dump({"bot_token": bot_token, "chat_id": chat_id}, f, indent=2)

@app.get("/api/settings/telegram")
def get_telegram_settings(request: Request):
    """Get Telegram bot settings."""
    from .auth import auth_manager
    if not auth_manager().is_authenticated(request):
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    settings = _load_tg_settings()
    # Mask token for security (show first/last 4 chars)
    token = settings.get("bot_token", "")
    if len(token) > 10:
        token = token[:4] + "*" * (len(token) - 8) + token[-4:]
    
    return {"bot_token": token, "chat_id": settings.get("chat_id", "")}

@app.post("/api/settings/telegram")
def save_telegram_settings(request: Request, payload: dict):
    """Save Telegram bot settings."""
    from .auth import auth_manager
    if not auth_manager().is_authenticated(request):
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    bot_token = payload.get("bot_token", "").strip()
    chat_id = payload.get("chat_id", "").strip()
    
    # If token is masked (contains *), only update chat_id
    if "*" in bot_token:
        current = _load_tg_settings()
        bot_token = current.get("bot_token", "")
    
    _save_tg_settings(bot_token, chat_id)
    return {"status": "ok", "message": "Settings saved"}

@app.post("/api/settings/telegram/test")
async def test_telegram_settings(request: Request):
    """Send a test message via Telegram."""
    from .auth import auth_manager
    if not auth_manager().is_authenticated(request):
        raise HTTPException(status_code=401, detail="Not authenticated")
    
    settings = _load_tg_settings()
    if not settings.get("bot_token") or not settings.get("chat_id"):
        return {"success": False, "message": "Bot Token 或 Chat ID 未配置"}
    
    text = "🔔 *iPerf3 测试工具 - 测试消息*\n\n✅ Telegram 告警配置成功！\n\n当 traceroute 定时监控检测到路由变化时，您将收到告警通知。"
    url = f"https://api.telegram.org/bot{settings['bot_token']}/sendMessage"
    
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(url, json={
                "chat_id": settings["chat_id"],
                "text": text,
                "parse_mode": "Markdown"
            })
            if response.status_code == 200:
                return {"success": True, "message": "测试消息已发送"}
            else:
                error_data = response.json()
                return {"success": False, "message": f"API 错误: {error_data.get('description', response.status_code)}"}
    except Exception as e:
        return {"success": False, "message": f"请求失败: {str(e)}"}


# ============== TraceSchedule CRUD API ==============

from .models import TraceSchedule, TraceResult
from .schemas import TraceScheduleCreate, TraceScheduleUpdate, TraceScheduleRead, TraceResultRead


@app.get("/api/trace/schedules", response_model=List[TraceScheduleRead])
def list_trace_schedules(db: Session = Depends(get_db)):
    """List all traceroute schedules."""
    schedules = db.scalars(select(TraceSchedule)).all()
    return schedules


@app.post("/api/trace/schedules", response_model=TraceScheduleRead)
def create_trace_schedule(payload: TraceScheduleCreate, db: Session = Depends(get_db)):
    """Create a new traceroute schedule."""
    from datetime import timezone
    
    schedule = TraceSchedule(**payload.model_dump())
    schedule.next_run_at = datetime.now(timezone.utc) + timedelta(seconds=schedule.interval_seconds)
    
    db.add(schedule)
    db.commit()
    db.refresh(schedule)
    
    # Register with scheduler
    _register_trace_schedule_job(schedule)
    
    return schedule


@app.get("/api/trace/schedules/{schedule_id}", response_model=TraceScheduleRead)
def get_trace_schedule(schedule_id: int, db: Session = Depends(get_db)):
    """Get a specific traceroute schedule."""
    schedule = db.get(TraceSchedule, schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return schedule


@app.put("/api/trace/schedules/{schedule_id}", response_model=TraceScheduleRead)
def update_trace_schedule(schedule_id: int, payload: TraceScheduleUpdate, db: Session = Depends(get_db)):
    """Update a traceroute schedule."""
    schedule = db.get(TraceSchedule, schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    
    updates = payload.model_dump(exclude_unset=True)
    for key, value in updates.items():
        setattr(schedule, key, value)
    
    # Recalculate next run if interval changed
    if "interval_seconds" in updates:
        from datetime import timezone
        schedule.next_run_at = datetime.now(timezone.utc) + timedelta(seconds=schedule.interval_seconds)
    
    db.commit()
    db.refresh(schedule)
    
    # Re-register with scheduler
    _register_trace_schedule_job(schedule)
    
    return schedule


@app.delete("/api/trace/schedules/{schedule_id}")
def delete_trace_schedule(schedule_id: int, db: Session = Depends(get_db)):
    """Delete a traceroute schedule."""
    schedule = db.get(TraceSchedule, schedule_id)
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    
    # Remove from scheduler
    try:
        scheduler.remove_job(f"trace_{schedule_id}")
    except Exception:
        pass
    
    # Unlink related trace results (set schedule_id to NULL instead of deleting)
    # This preserves the historical trace data
    from sqlalchemy import update
    db.execute(
        update(TraceResult)
        .where(TraceResult.schedule_id == schedule_id)
        .values(schedule_id=None)
    )
    
    db.delete(schedule)
    db.commit()
    return {"status": "ok", "message": "Schedule deleted"}


def _mask_ip_address(ip: str) -> str:
    """Mask IP address for guest users (last two octets -> **)."""
    if not ip or ip == "*":
        return ip
    parts = ip.split(".")
    if len(parts) == 4:  # IPv4
        return f"{parts[0]}.{parts[1]}.**.**"
    return ip  # IPv6 or hostname - return as-is


def _mask_hop_ips(hop: dict) -> dict:
    """Mask IPs in hop data for guest users."""
    masked = hop.copy()
    if "ip" in masked:
        masked["ip"] = _mask_ip_address(masked["ip"])
    return masked

@app.get("/api/trace/results", response_model=List[TraceResultRead])
def list_trace_results(
    request: Request,
    schedule_id: Optional[int] = None,
    limit: int = 50,
    db: Session = Depends(get_db)
):
    """List traceroute results, optionally filtered by schedule."""
    from .auth import auth_manager
    
    query = select(TraceResult).order_by(TraceResult.executed_at.desc()).limit(limit)
    
    if schedule_id:
        query = query.where(TraceResult.schedule_id == schedule_id)
    
    results = db.scalars(query).all()
    
    # For guests (not authenticated), mask IPs in results
    is_guest = not auth_manager().is_authenticated(request)
    
    if is_guest:
        # Convert to dicts and mask IPs
        masked_results = []
        for r in results:
            result_dict = {
                "id": r.id,
                "schedule_id": r.schedule_id,
                "src_node_id": r.src_node_id,
                "target": _mask_ip_address(r.target),
                "executed_at": r.executed_at,
                "total_hops": r.total_hops,
                "hops": [_mask_hop_ips(h) for h in (r.hops or [])],
                "route_hash": r.route_hash,
                "tool_used": r.tool_used,
                "elapsed_ms": r.elapsed_ms,
                "has_change": r.has_change,
                "change_summary": r.change_summary,
                "previous_route_hash": r.previous_route_hash,
                "source_type": getattr(r, "source_type", "scheduled"),
            }
            masked_results.append(result_dict)
        return masked_results
    
    return results


# ============== ASN Cache API ==============

@app.get("/api/asn/stats")
def get_asn_cache_stats(db: Session = Depends(get_db)):
    """Get ASN cache statistics."""
    count = get_asn_count(db)
    
    # Get tier distribution
    tier_counts = db.execute(
        text("SELECT tier, COUNT(*) FROM asn_cache GROUP BY tier ORDER BY COUNT(*) DESC")
    ).fetchall()
    
    return {
        "status": "ok",
        "total_cached": count,
        "tiers": {row[0]: row[1] for row in tier_counts if row[0]},
        "last_sync": None  # TODO: Add last sync timestamp tracking
    }


@app.post("/api/asn/sync")
async def trigger_asn_sync(db: Session = Depends(get_db)):
    """
    Manually trigger PeeringDB ASN sync.
    This may take several minutes for the initial sync (~15k networks).
    """
    import threading
    
    def _sync_in_background():
        try:
            sync_db = SessionLocal()
            stats = sync_peeringdb(sync_db)
            logger.info(f"[ASN-SYNC] Manual sync complete: {stats}")
            sync_db.close()
        except Exception as e:
            logger.error(f"[ASN-SYNC] Manual sync failed: {e}")
    
    # Run in background thread to avoid blocking
    thread = threading.Thread(target=_sync_in_background)
    thread.start()
    
    return {
        "status": "ok",
        "message": "PeeringDB sync started in background. Check logs for progress.",
        "current_count": get_asn_count(db)
    }


@app.get("/api/asn/{asn}")
def get_asn_details(asn: int, db: Session = Depends(get_db)):
    """
    Get cached ASN information from PeeringDB.
    Returns tier classification (T1/T2/T3/IX/CDN/ISP), network type, and scope.
    """
    info = get_asn_info(db, asn)
    if info:
        return {"status": "ok", **info}
    return {"status": "not_found", "asn": asn, "message": "ASN not in cache"}


# ============== Telegram Settings API ==============

TELEGRAM_SETTINGS_FILE = os.path.join(os.path.dirname(__file__), "..", "data", "telegram_settings.json")


def _load_tg_settings() -> dict:
    """Load Telegram settings from file."""
    try:
        if os.path.exists(TELEGRAM_SETTINGS_FILE):
            with open(TELEGRAM_SETTINGS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        logger.error(f"Failed to load Telegram settings: {e}")
    return {}


def _save_tg_settings(settings: dict) -> bool:
    """Save Telegram settings to file."""
    try:
        os.makedirs(os.path.dirname(TELEGRAM_SETTINGS_FILE), exist_ok=True)
        with open(TELEGRAM_SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(settings, f, indent=2)
        return True
    except Exception as e:
        logger.error(f"Failed to save Telegram settings: {e}")
        return False


@app.get("/admin/telegram")
def get_telegram_settings():
    """Get Telegram bot settings (token partially masked for security)."""
    settings = _load_tg_settings()
    # Mask the bot token for security
    if settings.get("bot_token"):
        token = settings["bot_token"]
        if len(token) > 10:
            settings["bot_token"] = token[:6] + "..." + token[-4:]
    return settings


class TelegramSettingsRequest(BaseModel):
    bot_token: Optional[str] = None
    chat_id: Optional[str] = None
    notify_route_change: bool = True
    notify_schedule_failure: bool = False
    notify_node_offline: bool = False
    notify_daily_report: bool = False


@app.post("/admin/telegram", response_model=None)
def save_telegram_settings_json(data: TelegramSettingsRequest):
    """Save Telegram bot settings (JSON body)."""
    current = _load_tg_settings()
    
    # Only update token if a new full token is provided (not masked)
    if data.bot_token and "..." not in data.bot_token:
        current["bot_token"] = data.bot_token
    
    if data.chat_id:
        current["chat_id"] = data.chat_id
    
    current["notify_route_change"] = data.notify_route_change
    current["notify_schedule_failure"] = data.notify_schedule_failure
    current["notify_node_offline"] = data.notify_node_offline
    current["notify_daily_report"] = data.notify_daily_report
    
    if _save_tg_settings(current):
        return {"status": "ok", "message": "Telegram settings saved"}
    else:
        raise HTTPException(status_code=500, detail="Failed to save settings")


@app.post("/admin/telegram/test")
async def test_telegram_message():
    """Send a test message to verify Telegram configuration."""
    settings = _load_tg_settings()
    
    if not settings.get("bot_token") or not settings.get("chat_id"):
        raise HTTPException(status_code=400, detail="Telegram bot_token or chat_id not configured")
    
    test_message = "🧪 *测试消息*\n\n这是来自 iperf3-test-tools 的测试通知。\n如果您收到此消息，说明 Telegram 配置正确！"
    
    url = f"https://api.telegram.org/bot{settings['bot_token']}/sendMessage"
    
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(url, json={
                "chat_id": settings["chat_id"],
                "text": test_message,
                "parse_mode": "Markdown"
            })
            
            if response.status_code == 200:
                return {"status": "ok", "message": "Test message sent successfully"}
            else:
                error_data = response.json() if response.text else {}
                error_desc = error_data.get("description", response.text)
                raise HTTPException(status_code=response.status_code, detail=f"Telegram API error: {error_desc}")
    except httpx.RequestError as e:
        raise HTTPException(status_code=500, detail=f"Failed to connect to Telegram API: {str(e)}")


def _register_trace_schedule_job(schedule: TraceSchedule):
    """Register or update a traceroute schedule in the APScheduler."""
    job_id = f"trace_{schedule.id}"
    
    # Remove existing job if any
    try:
        scheduler.remove_job(job_id)
    except Exception:
        pass
    
    if not schedule.enabled:
        return
    
    # Add new job - use None for next_run_time to execute based on interval from now
    # This ensures jobs run after startup even if stored next_run_at is stale
    scheduler.add_job(
        _execute_trace_schedule,
        trigger=IntervalTrigger(seconds=schedule.interval_seconds),
        id=job_id,
        args=[schedule.id],
        replace_existing=True,
        next_run_time=datetime.now(timezone.utc) + timedelta(seconds=30),  # First run 30s after registration
    )
    logger.info(f"Registered trace schedule job: {job_id}, interval={schedule.interval_seconds}s")


def _load_trace_schedules_on_startup():
    """Load all enabled trace schedules into APScheduler on startup."""
    db = SessionLocal()
    try:
        schedules = db.scalars(
            select(TraceSchedule).where(TraceSchedule.enabled == True)
        ).all()
        
        loaded_count = 0
        for schedule in schedules:
            try:
                _register_trace_schedule_job(schedule)
                loaded_count += 1
                logger.info(f"Loaded trace schedule {schedule.id}: {schedule.name}, interval={schedule.interval_seconds}s")
            except Exception as e:
                logger.error(f"Failed to load trace schedule {schedule.id}: {e}")
        
        logger.info(f"Loaded {loaded_count} trace schedules on startup")
    finally:
        db.close()


async def _send_telegram_alert(title: str, message: str) -> bool:
    """Send alert message via Telegram bot using stored settings."""
    tg_settings = _load_tg_settings()
    
    if not tg_settings.get("bot_token") or not tg_settings.get("chat_id"):
        logger.warning("Telegram alert skipped: bot token or chat_id not configured")
        return False
    
    text = f"*{title}*\n\n{message}"
    url = f"https://api.telegram.org/bot{tg_settings['bot_token']}/sendMessage"
    
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(url, json={
                "chat_id": tg_settings["chat_id"],
                "text": text,
                "parse_mode": "Markdown"
            })
            if response.status_code == 200:
                logger.info(f"Telegram alert sent: {title}")
                return True
            else:
                logger.error(f"Telegram API error: {response.status_code} - {response.text}")
                return False
    except Exception as e:
        logger.error(f"Telegram alert failed: {e}")
        return False


async def _execute_trace_schedule(schedule_id: int):
    """Execute a scheduled traceroute."""
    db = SessionLocal()
    try:
        schedule = db.get(TraceSchedule, schedule_id)
        if not schedule or not schedule.enabled:
            return
        
        # Determine target
        if schedule.target_type == "node" and schedule.target_node_id:
            target_node = db.get(Node, schedule.target_node_id)
            target = target_node.ip if target_node else schedule.target_address
        else:
            target = schedule.target_address
        
        if not target:
            logger.error(f"No target for trace schedule {schedule_id}")
            return
        
        # Get source node
        src_node = db.get(Node, schedule.src_node_id)
        if not src_node:
            logger.error(f"Source node not found for trace schedule {schedule_id}")
            return
        
        # Execute traceroute
        logger.info(f"Executing trace schedule {schedule.name}: {src_node.name} -> {target}")
        
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                response = await client.post(
                    f"http://{src_node.ip}:{src_node.agent_port}/traceroute",
                    json={"target": target, "max_hops": schedule.max_hops, "include_geo": True}
                )
                
                if response.status_code != 200:
                    logger.error(f"Traceroute failed: {response.status_code}")
                    return
                
                result = response.json()
        except Exception as e:
            logger.error(f"Traceroute request failed: {e}")
            return
        
        # Get previous result for comparison
        prev_result = db.scalars(
            select(TraceResult)
            .where(TraceResult.schedule_id == schedule_id)
            .order_by(TraceResult.executed_at.desc())
            .limit(1)
        ).first()
        
        # Check for route change using smart comparison (ignores timeout variations)
        has_change = False
        change_summary = None
        previous_hash = prev_result.route_hash if prev_result else None
        
        if prev_result:
            # Use smart comparison that ignores timeout-only changes
            has_change, change_summary = _compare_routes(prev_result.hops, result.get("hops", []))
        
        # Save result
        trace_result = TraceResult(
            schedule_id=schedule_id,
            src_node_id=schedule.src_node_id,
            target=target,
            total_hops=result.get("total_hops", 0),
            hops=result.get("hops", []),
            route_hash=result.get("route_hash", ""),
            tool_used=result.get("tool_used", "unknown"),
            elapsed_ms=result.get("elapsed_ms", 0),
            has_change=has_change,
            change_summary=change_summary,
            previous_route_hash=previous_hash,
        )
        db.add(trace_result)
        
        # Update schedule
        from datetime import timezone
        schedule.last_run_at = datetime.now(timezone.utc)
        schedule.next_run_at = datetime.now(timezone.utc) + timedelta(seconds=schedule.interval_seconds)
        
        db.commit()
        
        # Trigger alert if route changed
        if has_change and schedule.alert_on_change:
            logger.warning(f"Route change detected for schedule {schedule.name}!")
            alert_channels = schedule.alert_channels or []
            
            # Telegram notification
            if "telegram" in alert_channels:
                try:
                    await _send_telegram_alert(
                        title=f"🔔 路由变化告警: {schedule.name}",
                        message=f"源节点: {src_node.name}\n目标: {target}\n变化详情: {change_summary}"
                    )
                except Exception as e:
                    logger.error(f"Failed to send Telegram alert: {e}")
        
    finally:
        db.close()


def _compare_routes(old_hops: list, new_hops: list) -> tuple[bool, dict]:
    """Compare two route traces and return (has_change, summary).
    
    Returns has_change=False if:
    - Hop counts are the same AND
    - At each position, IPs are either the same OR one/both is timeout (*)
    
    Returns has_change=True if:
    - Hop counts differ, OR
    - At same position, both have real IPs that are different
    """
    old_hops = old_hops or []
    new_hops = new_hops or []
    
    # Extract IPs, treating * as None for comparison
    def get_real_ip(hop):
        ip = hop.get("ip", "*")
        return ip if ip != "*" else None
    
    # Compare by position - find actual IP changes (not just visibility changes)
    max_len = max(len(old_hops), len(new_hops))
    changed_positions = []  # List of hop indices where actual IP changed
    
    for i in range(max_len):
        old_ip = get_real_ip(old_hops[i]) if i < len(old_hops) else None
        new_ip = get_real_ip(new_hops[i]) if i < len(new_hops) else None
        
        # If both have real IPs and they're different, that's a real change
        if old_ip and new_ip and old_ip != new_ip:
            changed_positions.append(i)
    
    # Collect added/removed IPs for summary
    old_ip_set = set(get_real_ip(h) for h in old_hops if get_real_ip(h))
    new_ip_set = set(get_real_ip(h) for h in new_hops if get_real_ip(h))
    added = list(new_ip_set - old_ip_set)
    removed = list(old_ip_set - new_ip_set)
    
    # Determine if this is a real route change:
    # 1. Different hop count is a change
    # 2. Same position with different real IPs is a change
    hop_count_same = len(old_hops) == len(new_hops)
    has_position_changes = len(changed_positions) > 0
    
    # Only flag as change if:
    # - Hop count changed, OR
    # - There are position-based IP changes
    has_change = (not hop_count_same) or has_position_changes
    
    return has_change, {
        "added_hops": added,
        "removed_hops": removed,
        "old_hop_count": len(old_hops),
        "new_hop_count": len(new_hops),
        "changed_positions": changed_positions,  # For UI highlighting
    }

async def _ensure_iperf_server_running(dst: Node, requested_port: int) -> int:
    """Ensure iperf server is running on the requested port.
    
    Returns the port number the server is running on.
    """
    dst_status = await health_monitor.check_node(dst)
    current_port = dst_status.detected_iperf_port or dst_status.iperf_port
    
    logger.info(f"Checking iperf server on {dst.name}: running={dst_status.server_running}, current_port={current_port}, requested_port={requested_port}")
    
    if not dst_status.server_running:
        logger.info(f"Starting iperf server on {dst.name} at port {requested_port}")
        await _start_iperf_server(dst, requested_port)
        return requested_port
    
    if current_port != requested_port:
        logger.info(f"Restarting iperf server on {dst.name}: {current_port} -> {requested_port}")
        await _stop_iperf_server(dst)
        await _start_iperf_server(dst, requested_port)
        return requested_port
    
    logger.info(f"Iperf server already running on correct port {current_port}")
    return current_port


async def _sync_whitelist_to_agents(db: Session, max_retries: int = 2, force: bool = False) -> dict:
    """
    Synchronize IP whitelist to all agents with retry mechanism.
    Whitelist includes all node IPs + Master's own IP.
    
    Args:
        db: Database session
        max_retries: Maximum number of retry attempts for failed agents
    
    Returns:
        Dictionary with sync results including success/failure counts
    """
    # Get all node IPs
    nodes = db.scalars(select(Node)).all()
    whitelist = [node.ip for node in nodes]
    
    # Add Master's own IP (configured or auto-detected)
    master_ip = os.getenv("MASTER_IP", "")
    if not master_ip:
        # Auto-detect public IP
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get("https://api.ipify.org")
                if resp.status_code == 200:
                    master_ip = resp.text.strip()
                    logger.info(f"Auto-detected Master IP: {master_ip}")
        except Exception as e:
            logger.warning(f"Failed to auto-detect Master IP: {e}")
    
    if master_ip and master_ip not in whitelist:
        whitelist.append(master_ip)
    
    # Add extra IPs (for multi-master or additional trusted sources)
    # Format: comma or space separated IPs, e.g., "103.214.22.58,160.191.28.149"
    extra_ips_str = os.getenv("EXTRA_WHITELIST_IPS", "")
    if extra_ips_str:
        # Support both comma and space as separators
        extra_ips = [ip.strip() for ip in extra_ips_str.replace(",", " ").split() if ip.strip()]
        for ip in extra_ips:
            if ip and ip not in whitelist:
                whitelist.append(ip)
        logger.info(f"Added {len(extra_ips)} extra IPs to whitelist: {extra_ips}")
    
    logger.info(f"Syncing whitelist with {len(whitelist)} IPs to {len(nodes)} agents")
    
    # Smart sync: Check if whitelist has changed
    current_hash = _compute_whitelist_hash(whitelist)
    stored_hash = _load_whitelist_hash()
    
    if not force and stored_hash == current_hash:
        logger.info(f"Whitelist unchanged (hash: {current_hash[:8]}...), skipping sync")
        return {
            "total_agents": len(nodes),
            "success": len(nodes),
            "failed": 0,
            "errors": [],
            "retried": 0,
            "skipped": True,
            "reason": "whitelist_unchanged"
        }
    
    logger.info(f"Hash changed: {stored_hash[:8] if stored_hash else 'None'}... -> {current_hash[:8]}...")
    
    results = {
        "total_agents": len(nodes),
        "success": 0,
        "failed": 0,
        "errors": [],
        "retried": 0,
        "skipped": False
    }
    
    failed_nodes = []
    
    # First attempt - send whitelist to each agent
    async with httpx.AsyncClient(timeout=10) as client:
        for node in nodes:
            success = await _sync_to_single_agent(client, node, whitelist, results, db=db)
            if not success:
                failed_nodes.append(node)
    
    # Retry failed nodes
    if failed_nodes and max_retries > 0:
        logger.info(f"Retrying whitelist sync for {len(failed_nodes)} failed agents")
        
        for retry_attempt in range(max_retries):
            if not failed_nodes:
                break
            
            await asyncio.sleep(1)  # Brief delay before retry
            
            still_failed = []
            async with httpx.AsyncClient(timeout=10) as client:
                for node in failed_nodes:
                    success = await _sync_to_single_agent(client, node, whitelist, results, is_retry=True, db=db)
                    if success:
                        results["retried"] += 1
                    else:
                        still_failed.append(node)
            
            failed_nodes = still_failed
    
    # Save hash after successful sync
    if results["failed"] == 0:
        _save_whitelist_hash(current_hash)
        logger.info(f"Whitelist hash saved: {current_hash[:8]}...")
    
    return results


async def _sync_to_single_agent(
    client: httpx.AsyncClient,
    node: Node,
    whitelist: list[str],
    results: dict,
    is_retry: bool = False,
    db: Session | None = None
) -> bool:
    """
    Sync whitelist to a single agent.
    
    Returns:
        True if successful, False otherwise
    """
    from datetime import datetime
    
    status = "failed"
    try:
        # Use merge_whitelist endpoint (APPEND mode) to support multi-Master scenarios
        # This allows multiple Masters to share the same Agent without overwriting each other's IPs
        url = f"http://{node.ip}:{node.agent_port}/merge_whitelist"
        response = await client.post(url, json={"allowed_ips": whitelist})
        
        if response.status_code == 200:
            status = "synced"
            if not is_retry:
                results["success"] += 1
            else:
                # Move from failed to success on retry
                results["failed"] -= 1
                results["success"] += 1
                # Remove error from list
                results["errors"] = [e for e in results["errors"] if not e.startswith(f"{node.name}:")]
            
            logger.info(f"Whitelist synced to {node.name} ({node.ip})" + (" [retry]" if is_retry else ""))
            
            # Update Node status
            # Update Node status
            if db:
                node.whitelist_sync_status = "synced"
                node.whitelist_sync_message = "正常"
                node.whitelist_sync_at = datetime.utcnow()
                db.commit()
            
            return True
        else:
            error_detail = f"HTTP {response.status_code}"
            # Try to get error message from response
            try:
                err_json = response.json()
                if "error" in err_json:
                     error_detail += f" - {err_json['error']}"
            except:
                pass
                
            if not is_retry:
                results["failed"] += 1
                error_msg = f"{node.name}: {error_detail}"
                results["errors"].append(error_msg)
                logger.warning(f"Failed to sync whitelist to {node.name}: {error_msg}")
            
            # Update Node status (only if not retrying anymore or if we want to show intermediate failure)
            # We'll update it to failed for now, if retry succeeds it will overwrite
            if db:
                node.whitelist_sync_status = "failed"
                node.whitelist_sync_message = error_detail
                node.whitelist_sync_at = datetime.utcnow()
                db.commit()
                
            return False
    except Exception as e:
        if not is_retry:
            results["failed"] += 1
            results["errors"].append(f"{node.name}: {str(e)}")
            
        if db:
            node.whitelist_sync_status = "failed"
            node.whitelist_sync_message = str(e)
            node.whitelist_sync_at = datetime.utcnow()
            db.commit()
            
        return False



@app.get("/api/daily_schedule_traffic_stats")
async def daily_schedule_traffic_stats(
    date: Optional[str] = None,
    db: Session = Depends(get_db)
):
    """
    Get daily traffic statistics per schedule.
    Traffic is calculated for the specified date or today.
    
    Args:
        date: Optional date string in YYYY-MM-DD format. Defaults to today.
    
    Returns:
        stats: dict mapping schedule_id to total_bytes for that day
    """
    from sqlalchemy import func as sqla_func
    
    # Use UTC+8 (China/Hong Kong time) for daily reset
    utc_plus_8 = timezone(timedelta(hours=8))
    now = datetime.now(utc_plus_8)
    
    # Parse date parameter or use today
    if date:
        try:
            target_date = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=utc_plus_8)
        except ValueError:
            return {"status": "error", "message": "Invalid date format. Use YYYY-MM-DD"}
    else:
        target_date = now
    
    day_start = target_date.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    
    # Query schedule results with related test results to get traffic data
    from sqlalchemy.orm import joinedload
    
    results = db.scalars(
        select(ScheduleResult)
        .options(joinedload(ScheduleResult.test_result))
        .where(ScheduleResult.executed_at >= day_start)
        .where(ScheduleResult.executed_at < day_end)
        .where(ScheduleResult.status == "success")
    ).all()
    
    # Aggregate bytes per schedule from test result summaries
    stats = {}
    for sr in results:
        schedule_id = sr.schedule_id
        if schedule_id not in stats:
            stats[schedule_id] = 0
        
        # Extract bytes from test result summary
        if sr.test_result and sr.test_result.summary:
            summary = sr.test_result.summary
            upload_bytes = summary.get("upload_bytes", 0) or 0
            download_bytes = summary.get("download_bytes", 0) or 0
            stats[schedule_id] += upload_bytes + download_bytes
    
    return {
        "status": "ok",
        "date": day_start.strftime("%Y-%m-%d"),
        "stats": stats
    }


@app.get("/api/ping/history/{node_id}")
async def get_ping_history(node_id: int, db: Session = Depends(get_db)):
    """
    Get 24-hour ping history for a node with trend indicators.
    
    Returns ping data for CU/CM/CT carriers with trend arrows.
    """
    from .models import PingHistory
    
    # Get last 24 hours of data
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    
    records = db.query(PingHistory).filter(
        PingHistory.node_id == node_id,
        PingHistory.recorded_at >= cutoff
    ).order_by(PingHistory.recorded_at.asc()).all()
    
    # Group by carrier
    carriers = {}
    for rec in records:
        if rec.carrier not in carriers:
            carriers[rec.carrier] = []
        carriers[rec.carrier].append({
            "ts": rec.recorded_at.isoformat() if rec.recorded_at else None,
            "ms": rec.latency_ms
        })
    
    # Calculate trend for each carrier
    def calc_trend(data_points):
        """Calculate trend: current value vs 24h average with percentage thresholds."""
        if len(data_points) < 5:  # Need at least 5 samples for meaningful average
            return {"symbol": "→", "color": "#94a3b8", "diff": 0, "direction": "stable", "samples": len(data_points)}
        
        # Get current (latest) value and calculate average of previous values
        current = data_points[-1]["ms"]
        previous_values = [p["ms"] for p in data_points[:-1]]
        avg_24h = sum(previous_values) / len(previous_values)
        
        # Calculate difference and percentage change
        diff = round(current - avg_24h, 1)
        pct_change = (diff / avg_24h * 100) if avg_24h > 0 else 0
        
        # Use percentage thresholds for more meaningful comparison
        if pct_change < -15:  # More than 15% improvement
            return {"symbol": "↓↓↓", "color": "#22c55e", "diff": diff, "direction": "sharp_down", "avg_24h": round(avg_24h, 1), "pct": round(pct_change, 1)}
        elif pct_change < -8:  # 8-15% improvement
            return {"symbol": "↓↓", "color": "#22c55e", "diff": diff, "direction": "down", "avg_24h": round(avg_24h, 1), "pct": round(pct_change, 1)}
        elif pct_change < -3:  # 3-8% improvement
            return {"symbol": "↓", "color": "#22c55e", "diff": diff, "direction": "slight_down", "avg_24h": round(avg_24h, 1), "pct": round(pct_change, 1)}
        elif pct_change <= 3:  # Within ±3% = stable
            return {"symbol": "→", "color": "#94a3b8", "diff": diff, "direction": "stable", "avg_24h": round(avg_24h, 1), "pct": round(pct_change, 1)}
        elif pct_change <= 8:  # 3-8% degradation
            return {"symbol": "↗", "color": "#eab308", "diff": diff, "direction": "slight_up", "avg_24h": round(avg_24h, 1), "pct": round(pct_change, 1)}
        elif pct_change <= 15:  # 8-15% degradation
            return {"symbol": "↑↑", "color": "#f97316", "diff": diff, "direction": "up", "avg_24h": round(avg_24h, 1), "pct": round(pct_change, 1)}
        else:  # More than 15% degradation
            return {"symbol": "↑↑↑", "color": "#ef4444", "diff": diff, "direction": "sharp_up", "avg_24h": round(avg_24h, 1), "pct": round(pct_change, 1)}
    
    trends = {}
    for carrier, points in carriers.items():
        trends[carrier] = calc_trend(points)
    
    return {
        "status": "ok",
        "node_id": node_id,
        "carriers": carriers,
        "trends": trends
    }


@app.get("/api/ping/history/batch")
async def get_ping_history_batch(
    node_ids: str = Query(..., description="Comma-separated node IDs"),
    db: Session = Depends(get_db)
):
    """
    Get 24-hour ping history for multiple nodes in one request.
    
    This reduces the number of API calls when displaying trend indicators
    for many nodes on the dashboard.
    
    Args:
        node_ids: Comma-separated list of node IDs (e.g., "1,2,3")
    
    Returns:
        Dictionary mapping node_id to their carriers and trends data
    """
    from .models import PingHistory
    
    # Parse node IDs
    try:
        ids = [int(x.strip()) for x in node_ids.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid node_ids format")
    
    if not ids:
        return {"status": "ok", "nodes": {}}
    
    if len(ids) > 50:
        raise HTTPException(status_code=400, detail="Maximum 50 nodes per request")
    
    # Get last 24 hours of data for all nodes
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    
    records = db.query(PingHistory).filter(
        PingHistory.node_id.in_(ids),
        PingHistory.recorded_at >= cutoff
    ).order_by(PingHistory.node_id, PingHistory.recorded_at.asc()).all()
    
    # Group by node and carrier
    nodes_data = {}
    for rec in records:
        if rec.node_id not in nodes_data:
            nodes_data[rec.node_id] = {}
        if rec.carrier not in nodes_data[rec.node_id]:
            nodes_data[rec.node_id][rec.carrier] = []
        nodes_data[rec.node_id][rec.carrier].append({
            "ts": rec.recorded_at.isoformat() if rec.recorded_at else None,
            "ms": rec.latency_ms
        })
    
    # Calculate trend for each carrier (reuse calc_trend logic)
    def calc_trend(data_points):
        if len(data_points) < 5:
            return {"symbol": "→", "color": "#94a3b8", "diff": 0, "direction": "stable", "samples": len(data_points)}
        
        current = data_points[-1]["ms"]
        previous_values = [p["ms"] for p in data_points[:-1]]
        avg_24h = sum(previous_values) / len(previous_values)
        diff = round(current - avg_24h, 1)
        pct_change = (diff / avg_24h * 100) if avg_24h > 0 else 0
        
        if pct_change < -15:
            return {"symbol": "↓↓↓", "color": "#22c55e", "diff": diff, "direction": "sharp_down", "avg_24h": round(avg_24h, 1), "pct": round(pct_change, 1)}
        elif pct_change < -8:
            return {"symbol": "↓↓", "color": "#22c55e", "diff": diff, "direction": "down", "avg_24h": round(avg_24h, 1), "pct": round(pct_change, 1)}
        elif pct_change < -3:
            return {"symbol": "↓", "color": "#22c55e", "diff": diff, "direction": "slight_down", "avg_24h": round(avg_24h, 1), "pct": round(pct_change, 1)}
        elif pct_change <= 3:
            return {"symbol": "→", "color": "#94a3b8", "diff": diff, "direction": "stable", "avg_24h": round(avg_24h, 1), "pct": round(pct_change, 1)}
        elif pct_change <= 8:
            return {"symbol": "↗", "color": "#eab308", "diff": diff, "direction": "slight_up", "avg_24h": round(avg_24h, 1), "pct": round(pct_change, 1)}
        elif pct_change <= 15:
            return {"symbol": "↑↑", "color": "#f97316", "diff": diff, "direction": "up", "avg_24h": round(avg_24h, 1), "pct": round(pct_change, 1)}
        else:
            return {"symbol": "↑↑↑", "color": "#ef4444", "diff": diff, "direction": "sharp_up", "avg_24h": round(avg_24h, 1), "pct": round(pct_change, 1)}
    
    # Build response
    result = {}
    for node_id in ids:
        carriers = nodes_data.get(node_id, {})
        trends = {carrier: calc_trend(points) for carrier, points in carriers.items()}
        result[node_id] = {
            "carriers": carriers,
            "trends": trends
        }
    
    return {
        "status": "ok",
        "nodes": result
    }

@app.get("/api/daily_traffic_stats")
async def daily_traffic_stats(db: Session = Depends(get_db)):
    """
    Get daily traffic statistics per node.
    Traffic is calculated from midnight (00:00) local time.
    """
    from sqlalchemy.orm import joinedload
    
    # Use UTC+8 (China/Hong Kong time) for daily reset
    utc_plus_8 = timezone(timedelta(hours=8))
    now = datetime.now(utc_plus_8)
    today_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    
    # Get all nodes
    nodes = db.scalars(select(Node)).all()
    
    # Get health status for all nodes
    health_statuses = await health_monitor.get_statuses()
    status_by_id = {s.id: s for s in health_statuses}
    
    # Initialize traffic counters with backbone latency and trends
    from .models import PingHistory
    
    # Helper function to calculate trend
    def calc_carrier_trend(node_id: int, carrier: str, current_ms: float = None):
        """Calculate ping trend: current value vs 24h average for stability."""
        # Map carrier keys to standard names (database stores CU/CM/CT)
        carrier_map = {
            "ZJ_CU": "CU", "ZJ_CM": "CM", "ZJ_CT": "CT",
            "CU": "CU", "CM": "CM", "CT": "CT",
        }
        db_carrier = carrier_map.get(carrier.upper(), carrier.upper())
        
        cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
        records = db.query(PingHistory).filter(
            PingHistory.node_id == node_id,
            PingHistory.carrier == db_carrier,
            PingHistory.recorded_at >= cutoff
        ).order_by(PingHistory.recorded_at.desc()).all()
        
        if len(records) < 5:  # Need at least 5 samples for meaningful average
            return {"symbol": "→", "color": "#94a3b8", "diff": 0, "avg_24h": None, "samples": len(records)}
        
        # Calculate 24h average (excluding the most recent sample)
        all_latencies = [r.latency_ms for r in records]
        avg_24h = sum(all_latencies[1:]) / len(all_latencies[1:]) if len(all_latencies) > 1 else all_latencies[0]
        
        # Use provided current value or latest record
        current = current_ms if current_ms is not None else all_latencies[0]
        
        # Calculate difference: positive = latency increased (bad), negative = latency decreased (good)
        diff = round(current - avg_24h, 1)
        pct_change = (diff / avg_24h * 100) if avg_24h > 0 else 0
        
        # Use percentage thresholds for more meaningful comparison
        # Small absolute changes on high latency links shouldn't trigger alarms
        if pct_change < -15:  # More than 15% improvement
            return {"symbol": "↓↓↓", "color": "#22c55e", "diff": diff, "avg_24h": round(avg_24h, 1), "pct": round(pct_change, 1)}
        elif pct_change < -8:  # 8-15% improvement
            return {"symbol": "↓↓", "color": "#22c55e", "diff": diff, "avg_24h": round(avg_24h, 1), "pct": round(pct_change, 1)}
        elif pct_change < -3:  # 3-8% improvement
            return {"symbol": "↓", "color": "#22c55e", "diff": diff, "avg_24h": round(avg_24h, 1), "pct": round(pct_change, 1)}
        elif pct_change <= 3:  # Within ±3% = stable
            return {"symbol": "→", "color": "#94a3b8", "diff": diff, "avg_24h": round(avg_24h, 1), "pct": round(pct_change, 1)}
        elif pct_change <= 8:  # 3-8% degradation
            return {"symbol": "↗", "color": "#eab308", "diff": diff, "avg_24h": round(avg_24h, 1), "pct": round(pct_change, 1)}
        elif pct_change <= 15:  # 8-15% degradation
            return {"symbol": "↑↑", "color": "#f97316", "diff": diff, "avg_24h": round(avg_24h, 1), "pct": round(pct_change, 1)}
        else:  # More than 15% degradation
            return {"symbol": "↑↑↑", "color": "#ef4444", "diff": diff, "avg_24h": round(avg_24h, 1), "pct": round(pct_change, 1)}
    
    traffic_by_node = {}
    for n in nodes:
        hs = status_by_id.get(n.id)
        latency_data = []
        if hs and hs.backbone_latency:
            for lat in hs.backbone_latency:
                if lat.latency_ms is None:
                    continue
                carrier_key = lat.key.upper() if lat.key else "N/A"
                trend = calc_carrier_trend(n.id, carrier_key)
                latency_data.append({
                    "key": lat.key, 
                    "name": lat.name, 
                    "ms": lat.latency_ms,
                    "trend_symbol": trend["symbol"],
                    "trend_color": trend["color"],
                    "trend_diff": trend["diff"]
                })
        traffic_by_node[n.id] = {
            "bytes": 0, 
            "name": n.name, 
            "ip": n.ip, 
            "status": hs.status if hs else "offline",
            "backbone_latency": latency_data
        }
    
    # Query schedule results from today only
    # Eager load test_result and schedule to ensure data is available
    results = db.scalars(
        select(ScheduleResult)
        .options(
            joinedload(ScheduleResult.test_result),
            joinedload(ScheduleResult.schedule)
        )
        .where(ScheduleResult.executed_at >= today_midnight)
        .where(ScheduleResult.status == "success")
    ).unique().all()
    
    # Sum up bytes transferred per node
    for result in results:
        if not result.test_result:
            continue
        
        raw = result.test_result.raw_result or {}
        
        # 正确解析数据结构：可能是 raw.end 或 raw.iperf_result.end
        end = raw.get("end") or {}
        if not end and "iperf_result" in raw:
            end = raw.get("iperf_result", {}).get("end", {})
        
        # 获取发送和接收的字节数（可能不同，因为网络丢包等原因）
        bytes_sent = 0
        bytes_received = 0
        
        # UDP测试只有sum
        if "sum" in end:
            # UDP模式下，发送和接收视为相同
            bytes_sent = end["sum"].get("bytes", 0)
            bytes_received = bytes_sent
        else:
            # TCP模式下，分别获取发送和接收
            bytes_sent = end.get("sum_sent", {}).get("bytes", 0)
            bytes_received = end.get("sum_received", {}).get("bytes", 0)
            
        if not bytes_sent and not bytes_received:
            continue
        
        # Get schedule to find src/dst nodes and direction
        schedule = result.schedule
        if schedule:
            src_id = schedule.src_node_id
            dst_id = schedule.dst_node_id
            direction = getattr(schedule, 'direction', 'upload') or 'upload'
            
            # 根据方向分配流量：
            # - upload (默认): 源节点发送，目标节点接收
            # - download (reverse): 目标节点发送，源节点接收
            # - bidirectional: 两边都发送和接收
            
            if direction == 'download':
                # Reverse模式：目标节点发送数据到源节点
                if dst_id in traffic_by_node:
                    traffic_by_node[dst_id]["bytes"] += bytes_sent
                if src_id in traffic_by_node:
                    traffic_by_node[src_id]["bytes"] += bytes_received
            elif direction == 'bidirectional':
                # 双向模式：两边都发送和接收，取两者之和
                total_bytes = bytes_sent + bytes_received
                if src_id in traffic_by_node:
                    traffic_by_node[src_id]["bytes"] += total_bytes // 2  # 平分
                if dst_id in traffic_by_node:
                    traffic_by_node[dst_id]["bytes"] += total_bytes // 2  # 平分
            else:
                # Upload模式（默认）：源节点发送，目标节点接收
                if src_id in traffic_by_node:
                    traffic_by_node[src_id]["bytes"] += bytes_sent
                if dst_id in traffic_by_node:
                    traffic_by_node[dst_id]["bytes"] += bytes_received
    
    # Format response
    node_stats = []
    for node_id, data in traffic_by_node.items():
        total_gb = round(data["bytes"] / (1024 ** 3), 2)
        node_stats.append({
            "node_id": node_id,
            "name": data["name"],
            "ip": data["ip"],
            "total_bytes": data["bytes"],
            "total_gb": total_gb,
            "status": data["status"],
            "backbone_latency": data.get("backbone_latency", [])
        })
    
    # Sort by traffic (descending)
    node_stats.sort(key=lambda x: x["total_bytes"], reverse=True)
    
    return {
        "status": "ok",
        "date": today_midnight.strftime("%Y-%m-%d"),
        "nodes": node_stats
    }



@app.post("/admin/sync_whitelist")
async def sync_whitelist_endpoint(db: Session = Depends(get_db)):
    """
    Manually trigger whitelist synchronization to all agents.
    Returns detailed sync results including success/failure counts.
    """
    results = await _sync_whitelist_to_agents(db, force=True)  # Manual sync always forces
    
    return {
        "status": "ok",
        "message": f"Whitelist synced to {results['success']}/{results['total_agents']} agents",
        "results": results
    }


@app.get("/admin/whitelist")
async def get_whitelist(db: Session = Depends(get_db)):
    """
    Get current IP whitelist.
    Returns all node IPs that are allowed to access agents.
    """
    nodes = db.scalars(select(Node)).all()
    whitelist = [node.ip for node in nodes]
    
    # Add Master's own IP if configured
    master_ip = os.getenv("MASTER_IP", "")
    if master_ip and master_ip not in whitelist:
        whitelist.append(master_ip)
    
    return {
        "status": "ok",
        "whitelist": sorted(whitelist),
        "count": len(whitelist),
        "nodes": [
            {
                "id": node.id,
                "name": node.name,
                "ip": node.ip,
                "description": node.description,
                "whitelist_sync_status": getattr(node, "whitelist_sync_status", "unknown"),
                "whitelist_sync_at": getattr(node, "whitelist_sync_at", None),
                "whitelist_sync_message": getattr(node, "whitelist_sync_message", None)
            }
            for node in nodes
        ]
    }


@app.post("/admin/whitelist/add")
async def add_to_whitelist(
    ip: str = Body(..., embed=True),
    description: str = Body(None, embed=True),
    db: Session = Depends(get_db)
):
    """
    Add an IP address to the whitelist by creating a virtual node entry.
    This allows adding IPs that don't correspond to actual agent nodes.
    """
    import ipaddress
    
    # Validate IP address format
    try:
        ipaddress.ip_address(ip)
    except ValueError:
        # Check if it's CIDR notation
        try:
            ipaddress.ip_network(ip, strict=False)
        except ValueError:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid IP address format: {ip}"
            )
    
    # Check if IP already exists
    existing = db.scalars(select(Node).where(Node.ip == ip)).first()
    if existing:
        return {
            "status": "ok",
            "message": f"IP {ip} already in whitelist (node: {existing.name})",
            "node_id": existing.id
        }
    
    # Create a virtual node entry for this IP
    node_name = f"whitelist-{ip.replace('.', '-').replace(':', '-').replace('/', '-')}"
    new_node = Node(
        name=node_name,
        ip=ip,
        agent_port=8000,  # Default, won't be used
        iperf_port=DEFAULT_IPERF_PORT,
        description=description or f"Whitelisted IP added manually"
    )
    
    db.add(new_node)
    db.commit()
    db.refresh(new_node)
    
    # Sync whitelist to all agents
    try:
        asyncio.create_task(_sync_whitelist_to_agents(db))
    except Exception as e:
        logger.error(f"Failed to trigger whitelist sync: {e}")
    
    return {
        "status": "ok",
        "message": f"IP {ip} added to whitelist",
        "node_id": new_node.id,
        "node_name": new_node.name
    }


@app.delete("/admin/whitelist/remove")
async def remove_from_whitelist(
    ip: str = Body(..., embed=True),
    db: Session = Depends(get_db)
):
    """
    Remove an IP address from the whitelist.
    This will delete the corresponding node entry.
    """
    node = db.scalars(select(Node).where(Node.ip == ip)).first()
    
    if not node:
        raise HTTPException(
            status_code=404,
            detail=f"IP {ip} not found in whitelist"
        )
    
    node_name = node.name
    db.delete(node)
    db.commit()
    
    # Sync whitelist to all agents
    try:
        asyncio.create_task(_sync_whitelist_to_agents(db))
    except Exception as e:
        logger.error(f"Failed to trigger whitelist sync: {e}")
    
    return {
        "status": "ok",
        "message": f"IP {ip} removed from whitelist (node: {node_name})"
    }


@app.get("/admin/whitelist/status")
async def get_whitelist_status(db: Session = Depends(get_db)):
    """
    Get whitelist synchronization status across all agents.
    Queries each agent to verify their current whitelist.
    """
    nodes = db.scalars(select(Node)).all()
    master_whitelist = [node.ip for node in nodes]
    
    # Auto-detect master IP (same logic as _sync_whitelist_to_agents)
    master_ip = os.getenv("MASTER_IP", "")
    if not master_ip:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get("https://api.ipify.org")
                if resp.status_code == 200:
                    master_ip = resp.text.strip()
        except Exception:
            pass
    
    if master_ip and master_ip not in master_whitelist:
        master_whitelist.append(master_ip)
    
    # Add extra IPs (same logic as _sync_whitelist_to_agents)
    extra_ips_str = os.getenv("EXTRA_WHITELIST_IPS", "")
    if extra_ips_str:
        extra_ips = [ip.strip() for ip in extra_ips_str.replace(",", " ").split() if ip.strip()]
        for ip in extra_ips:
            if ip and ip not in master_whitelist:
                master_whitelist.append(ip)
    
    agent_statuses = []
    
    from datetime import datetime
    
    async with httpx.AsyncClient(timeout=5) as client:
        for node in nodes:
            try:
                url = f"http://{node.ip}:{node.agent_port}/whitelist"
                response = await client.get(url)
                
                if response.status_code == 200:
                    data = response.json()
                    agent_whitelist = data.get("allowed_ips", [])
                    
                    # Check if whitelists match (ignoring localhost)
                    ignored_ips = {'127.0.0.1', '::1'}
                    agent_ips_clean = {ip for ip in agent_whitelist if ip not in ignored_ips}
                    master_ips_clean = {ip for ip in master_whitelist if ip not in ignored_ips}
                    
                    in_sync = master_ips_clean == agent_ips_clean
                    
                    # Update DB status
                    node.whitelist_sync_status = "synced" if in_sync else "not_synced"
                    node.whitelist_sync_message = "正常" if in_sync else "内容不一致"
                    node.whitelist_sync_at = datetime.utcnow()
                    
                    agent_statuses.append({
                        "node_id": node.id,
                        "node_name": node.name,
                        "ip": node.ip,
                        "status": "in_sync" if in_sync else "out_of_sync",
                        "whitelist_count": len(agent_whitelist),
                        "in_sync": in_sync
                    })
                else:
                    # Update DB status
                    error_msg = f"HTTP {response.status_code}"
                    node.whitelist_sync_status = "failed"
                    node.whitelist_sync_message = error_msg
                    node.whitelist_sync_at = datetime.utcnow()
                    
                    agent_statuses.append({
                        "node_id": node.id,
                        "node_name": node.name,
                        "ip": node.ip,
                        "status": "error",
                        "error": error_msg,
                        "in_sync": False
                    })
            except Exception as e:
                # Update DB status
                error_msg = str(e)
                node.whitelist_sync_status = "failed"
                node.whitelist_sync_message = error_msg
                node.whitelist_sync_at = datetime.utcnow()
                
                agent_statuses.append({
                    "node_id": node.id,
                    "node_name": node.name,
                    "ip": node.ip,
                    "status": "unreachable",
                    "error": error_msg,
                    "in_sync": False
                })
    
    db.commit()
    
    in_sync_count = sum(1 for s in agent_statuses if s.get("in_sync", False))
    
    return {
        "status": "ok",
        "master_whitelist": sorted(master_whitelist),
        "master_whitelist_count": len(master_whitelist),
        "agents": agent_statuses,
        "total_agents": len(agent_statuses),
        "in_sync_count": in_sync_count,
        "out_of_sync_count": len(agent_statuses) - in_sync_count
    }


# ============== Schedule Sync to Agents ==============

async def _sync_schedule_to_agent(schedule, src_node: Node, dst_node: Node, db: Session):
    """
    Sync a schedule to the source agent for local cron execution.
    
    The agent will store the schedule and execute tests at the specified cron times.
    """
    if not schedule.cron_expression:
        return {"status": "skipped", "reason": "No cron_expression set"}
    
    if not src_node:
        return {"status": "error", "error": "Source node not found"}
    
    # Determine test direction
    reverse = schedule.direction in ["download", "bidirectional"]
    bidir = schedule.direction == "bidirectional"
    
    schedule_payload = {
        "schedule_id": schedule.id,
        "name": schedule.name,
        "cron_expression": schedule.cron_expression,
        "target_ip": dst_node.ip if dst_node else None,
        "target_port": schedule.port or 5201,
        "protocol": schedule.protocol or "tcp",
        "duration": schedule.duration or 10,
        "parallel": schedule.parallel or 1,
        "reverse": reverse,
        "bidir": bidir,
        "bandwidth": schedule.udp_bandwidth,
        "enabled": schedule.enabled,
        "synced_at": datetime.now(timezone.utc).isoformat()
    }
    
    # Normal agent - push schedule directly
    if src_node.agent_mode != "reverse":
        try:
            agent_url = f"http://{src_node.ip}:{src_node.agent_port}/schedules"
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(agent_url, json=schedule_payload)
                if resp.status_code == 200:
                    # Update sync timestamp
                    schedule.schedule_synced_at = datetime.now(timezone.utc)
                    db.commit()
                    return {"status": "ok", "agent": src_node.name}
                else:
                    return {"status": "error", "error": f"Agent returned {resp.status_code}"}
        except Exception as e:
            return {"status": "error", "error": str(e)}
    else:
        # Reverse/NAT agent - schedule will be synced via poll
        # Store in a way that the agent can pick up when it polls
        return {"status": "pending", "reason": "Reverse agent will sync on next poll"}


async def _delete_schedule_from_agent(schedule_id: int, src_node: Node):
    """Delete a schedule from the agent."""
    if not src_node or src_node.agent_mode == "reverse":
        return {"status": "skipped"}
    
    try:
        agent_url = f"http://{src_node.ip}:{src_node.agent_port}/schedules/{schedule_id}"
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.delete(agent_url)
            return {"status": "ok" if resp.status_code == 200 else "error"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


class ScheduleResultPayload(BaseModel):
    """Payload for receiving schedule test results from agents."""
    schedule_id: int
    node_name: str
    result: dict
    executed_at: str


@app.post("/api/schedule/result")
async def receive_schedule_result(payload: ScheduleResultPayload, db: Session = Depends(get_db)):
    """
    Receive test result from agent executing a scheduled test.
    
    Called by agents after executing a cron-scheduled iperf3 test.
    """
    from .models import TestSchedule, ScheduleResult, TestResult
    
    schedule = db.query(TestSchedule).filter(TestSchedule.id == payload.schedule_id).first()
    if not schedule:
        raise HTTPException(status_code=404, detail=f"Schedule {payload.schedule_id} not found")
    
    # Find source node by name
    from .models import Node
    src_node = db.query(Node).filter(Node.name == payload.node_name).first()
    
    # Parse executed_at
    try:
        executed_at = datetime.fromisoformat(payload.executed_at.replace('Z', '+00:00'))
    except:
        executed_at = datetime.now(timezone.utc)
    
    # Create test result if iperf data present
    test_result_id = None
    iperf_data = payload.result.get("iperf_result")
    if iperf_data and payload.result.get("status") == "ok":
        # Summarize metrics with direction info
        direction_label = "download" if schedule.direction == "download" else "upload"
        summary = _summarize_metrics({"iperf_result": iperf_data}, direction_label=direction_label)
        
        test_result = TestResult(
            src_node_id=schedule.src_node_id,
            dst_node_id=schedule.dst_node_id,
            protocol=schedule.protocol,
            params={"schedule_id": schedule.id, "from_agent_cron": True},
            raw_result=iperf_data,
            summary=summary,
            created_at=executed_at
        )
        db.add(test_result)
        db.flush()
        test_result_id = test_result.id
    
    # Create schedule result record
    status = "success" if payload.result.get("status") == "ok" else "error"
    error_msg = payload.result.get("error") if status == "error" else None
    
    schedule_result = ScheduleResult(
        schedule_id=schedule.id,
        test_result_id=test_result_id,
        executed_at=executed_at,
        status=status,
        error_message=error_msg
    )
    db.add(schedule_result)
    
    # Update schedule last_run_at
    schedule.last_run_at = executed_at
    
    db.commit()
    
    return {
        "status": "ok",
        "schedule_id": schedule.id,
        "test_result_id": test_result_id,
        "result_status": status
    }


@app.post("/api/schedules/{schedule_id}/sync")
async def sync_schedule_to_agent(schedule_id: int, db: Session = Depends(get_db)):
    """
    Manually trigger sync of a schedule to its source agent.
    """
    from .models import TestSchedule
    
    schedule = db.query(TestSchedule).filter(TestSchedule.id == schedule_id).first()
    if not schedule:
        raise HTTPException(status_code=404, detail=f"Schedule {schedule_id} not found")
    
    src_node = schedule.src_node
    dst_node = schedule.dst_node
    
    result = await _sync_schedule_to_agent(schedule, src_node, dst_node, db)
    return result


# Note: Duplicate /api/daily_traffic_stats removed - using the one at ~line 5962
@app.get("/api/daily_schedule_traffic_stats")
async def get_daily_schedule_traffic_stats(db: Session = Depends(get_db)):
    """
    Get daily traffic statistics per schedule.
    """
    from datetime import timedelta
    
    # Use UTC+8 (same as VPS traffic stats) for consistency
    utc_plus_8 = timezone(timedelta(hours=8))
    now = datetime.now(utc_plus_8)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    
    # Query ScheduleResults joined with TestResults
    rows = db.execute(
        select(TestResult.raw_result, ScheduleResult.schedule_id)
        .join(ScheduleResult, TestResult.id == ScheduleResult.test_result_id)
        .where(ScheduleResult.executed_at >= today_start)
        .where(ScheduleResult.status == "success")
    ).all()
    
    stats = {} # schedule_id -> total_bytes
    
    for raw_result, schedule_id in rows:
        if not raw_result or not isinstance(raw_result, dict):
            continue
            
        try:
            # Handle different data structures:
            # 1. Direct: raw_result.end
            # 2. Nested: raw_result.iperf_result.end
            end_data = raw_result.get("end", {})
            if not end_data and "iperf_result" in raw_result:
                end_data = raw_result.get("iperf_result", {}).get("end", {})
            
            # TCP: sum_sent + sum_received
            bytes_sent = end_data.get("sum_sent", {}).get("bytes", 0)
            bytes_recvd = end_data.get("sum_received", {}).get("bytes", 0)
            
            # UDP: just sum
            if bytes_sent == 0 and bytes_recvd == 0:
                bytes_sent = end_data.get("sum", {}).get("bytes", 0)
            
            total = bytes_sent + bytes_recvd
            if total > 0:
                stats[schedule_id] = stats.get(schedule_id, 0) + total
        except Exception:
            continue
            
    return {"status": "ok", "stats": stats}



async def _call_agent_test(src: Node, payload: dict, duration: int) -> dict:
    # Check if source node is reverse mode (NAT) - use task queue instead of direct HTTP
    src_mode = getattr(src, "agent_mode", "normal") or "normal"
    if src_mode == "reverse":
        logger.info(f"[AGENT-TEST] Source {src.name} is reverse mode, using task queue")
        return await _call_reverse_agent_test(src, payload, duration)
    
    # Normal mode - direct HTTP call to agent
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

    # Check if we need to swap direction for NAT/reverse mode nodes
    # When dst is reverse mode, swap src/dst and toggle -R flag
    direction_swapped = False
    original_src_id = src.id
    original_dst_id = dst.id
    effective_reverse = test.reverse
    
    dst_mode = getattr(dst, "agent_mode", "normal") or "normal"
    src_mode = getattr(src, "agent_mode", "normal") or "normal"
    
    if dst_mode == "reverse":
        # Destination is behind NAT, so we need to swap
        # The NAT node will initiate connection to the public node
        logger.info(f"[NAT-SWAP] Destination {dst.name} is reverse mode, swapping direction")
        src, dst = dst, src  # Swap nodes
        effective_reverse = not test.reverse  # Toggle reverse flag
        direction_swapped = True
        logger.info(f"[NAT-SWAP] New: src={src.name}, dst={dst.name}, reverse={effective_reverse}")
    elif src_mode == "reverse":
        # Source is behind NAT - this is the expected case, no swap needed
        logger.info(f"[NAT] Source {src.name} is reverse mode, using as-is")

    src_status = await health_monitor.check_node(src)
    if src_status.status != "online":
        raise HTTPException(status_code=503, detail="source node is offline or unreachable")

    dst_status = await health_monitor.check_node(dst)
    if dst_status.status != "online":
        raise HTTPException(status_code=503, detail="destination node is offline or unreachable")

    # Priority: detected port from health check > node's configured port > request's port
    requested_port = dst_status.detected_iperf_port or dst.iperf_port or test.port
    logger.info(f"Using iperf port {requested_port} for test (detected: {dst_status.detected_iperf_port}, node: {dst.iperf_port}, request: {test.port})")

    server_started = False
    actual_port = await _ensure_iperf_server_running(dst, requested_port)
    server_started = True  # Server is always running after this call

    payload = {
        "target": dst.ip,
        "port": actual_port,  # Use the actual port returned by ensure function
        "duration": test.duration,
        "protocol": test.protocol,
        "parallel": test.parallel,
        "reverse": effective_reverse,  # Use potentially toggled reverse flag
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
        # Determine direction_label based on reverse flag for proper UDP direction assignment
        direction_label = "download" if effective_reverse else "upload"
        summary = _summarize_metrics(raw_data, direction_label=direction_label)
    except Exception as exc:
        snippet = response.text[:200]
        logger.exception("Failed to summarize agent response")
        raise HTTPException(
            status_code=502,
            detail=f"agent response JSON could not be processed: {snippet}",
        ) from exc
    
    # Store with ORIGINAL user-specified src/dst for proper display
    obj = TestResult(
        src_node_id=original_src_id,
        dst_node_id=original_dst_id,
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

    # Check if we need to swap direction for NAT/reverse mode nodes
    direction_swapped = False
    original_src_id = src.id
    original_dst_id = dst.id
    
    dst_mode = getattr(dst, "agent_mode", "normal") or "normal"
    src_mode = getattr(src, "agent_mode", "normal") or "normal"
    
    if dst_mode == "reverse":
        # Destination is behind NAT, so we need to swap
        logger.info(f"[NAT-SWAP] Suite: Destination {dst.name} is reverse mode, swapping direction")
        src, dst = dst, src  # Swap nodes
        direction_swapped = True
        logger.info(f"[NAT-SWAP] Suite: New src={src.name}, dst={dst.name}")
    elif src_mode == "reverse":
        logger.info(f"[NAT] Suite: Source {src.name} is reverse mode, using as-is")

    src_status = await health_monitor.check_node(src)
    if src_status.status != "online":
        raise HTTPException(status_code=503, detail="source node is offline or unreachable")

    dst_status = await health_monitor.check_node(dst)
    if dst_status.status != "online":
        raise HTTPException(status_code=503, detail="destination node is offline or unreachable")

    # Priority: detected port from health check > node's configured port > request's port
    requested_port = dst_status.detected_iperf_port or dst.iperf_port or test.port
    logger.info(f"Using iperf port {requested_port} for suite test (detected: {dst_status.detected_iperf_port}, node: {dst.iperf_port}, request: {test.port})")

    server_started = False
    actual_port = 0
    try:
        actual_port = await _ensure_iperf_server_running(dst, requested_port)
        server_started = True  # Server is always running after this call
        
        # When direction is swapped, we need to toggle the reverse flags
        # so that "去程" and "回程" maintain their original meaning
        plan = [
            ("TCP 去程", "tcp", direction_swapped, test.tcp_bandwidth),       # Normal: False, Swapped: True
            ("TCP 回程", "tcp", not direction_swapped, test.tcp_bandwidth),   # Normal: True, Swapped: False
            ("UDP 去程", "udp", direction_swapped, test.udp_bandwidth),       # Normal: False, Swapped: True
            ("UDP 回程", "udp", not direction_swapped, test.udp_bandwidth),   # Normal: True, Swapped: False
        ]

        results: list[dict] = []
        for label, protocol, reverse, bandwidth in plan:
            payload = {
                "target": dst.ip,
                "port": actual_port,  # Use actual port from ensure function
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
            # Determine direction_label based on reverse flag
            direction_label = "download" if reverse else "upload"
            results.append(
                {
                    "label": label,
                    "protocol": protocol,
                    "reverse": reverse,
                    "raw": raw_data,
                    "summary": _summarize_metrics(raw_data, direction_label=direction_label),
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

    # Store with ORIGINAL user-specified src/dst for proper display
    obj = TestResult(
        src_node_id=original_src_id,
        dst_node_id=original_dst_id,
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
    # Filter out tests that are part of a schedule (have a matching ScheduleResult)
    # Use outer join and check for null on the right side
    results = db.scalars(
        select(TestResult)
        .outerjoin(ScheduleResult, ScheduleResult.test_result_id == TestResult.id)
        .where(ScheduleResult.id == None)
        .order_by(TestResult.created_at.desc())
    ).all()
    return results


@app.delete("/tests")
def delete_all_tests(db: Session = Depends(get_db)):
    # First, nullify references in schedule_results to avoid FK violation
    db.execute(
        text("UPDATE schedule_results SET test_result_id = NULL WHERE test_result_id IS NOT NULL")
    )
    
    # Now delete all test results
    results = db.scalars(select(TestResult)).all()
    for test in results:
        db.delete(test)
    db.commit()
    _persist_state(db)
    return {"status": "deleted", "count": len(results)}


# ============================================================================
# Admin Endpoints - Database Management
# ============================================================================

@app.post("/admin/clear_all_test_data")
def clear_all_test_data(db: Session = Depends(get_db)):
    """
    Clear all test data from the database.
    This includes: test_results and schedule_results.
    Does NOT delete: nodes, schedules (configurations).
    """
    # Count before deletion
    test_count = db.execute(text("SELECT COUNT(*) FROM test_results")).scalar()
    schedule_result_count = db.execute(text("SELECT COUNT(*) FROM schedule_results")).scalar()
    
    # Delete schedule_results first (has FK to test_results)
    db.execute(text("DELETE FROM schedule_results"))
    
    # Delete test_results
    db.execute(text("DELETE FROM test_results"))
    
    db.commit()
    _persist_state(db)
    
    logger.info(f"[ADMIN] Cleared all test data: {test_count} test_results, {schedule_result_count} schedule_results")
    
    return {
        "status": "ok",
        "test_results_deleted": test_count,
        "schedule_results_deleted": schedule_result_count
    }


@app.post("/admin/clear_schedule_results")
def clear_schedule_results(db: Session = Depends(get_db)):
    """
    Clear only schedule_results from the database.
    This preserves test_results from manual/single tests.
    """
    # Count before deletion
    count = db.execute(text("SELECT COUNT(*) FROM schedule_results")).scalar()
    
    # Delete all schedule_results
    db.execute(text("DELETE FROM schedule_results"))
    
    db.commit()
    _persist_state(db)
    
    logger.info(f"[ADMIN] Cleared schedule results: {count} records")
    
    return {
        "status": "ok",
        "count": count
    }


@app.delete("/tests/{test_id}")
def delete_test(test_id: int, db: Session = Depends(get_db)):
    test = db.get(TestResult, test_id)
    if not test:
        raise HTTPException(status_code=404, detail="test not found")

    db.delete(test)
    db.commit()
    _persist_state(db)
    return {"status": "deleted"}


def _add_schedule_to_scheduler(schedule: TestSchedule, next_run_time: datetime | None = None):
    """添加任务到调度器"""
    job_id = f"schedule_{schedule.id}"
    
    # 移除旧任务(如果存在)
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    
    # 根据 cron_expression 或 interval_seconds 选择 trigger
    if schedule.cron_expression:
        # 使用 CronTrigger
        parts = schedule.cron_expression.strip().split()
        if len(parts) >= 5:
            trigger = CronTrigger(
                minute=parts[0],
                hour=parts[1],
                day=parts[2],
                month=parts[3],
                day_of_week=parts[4],
                timezone='UTC'
            )
            scheduler.add_job(
                func=_execute_schedule_task,
                trigger=trigger,
                id=job_id,
                args=[schedule.id],
                replace_existing=True
            )
            logger.info(f"Added schedule {schedule.id} to scheduler with cron: {schedule.cron_expression}")
        else:
            logger.error(f"Invalid cron expression for schedule {schedule.id}: {schedule.cron_expression}")
    elif schedule.interval_seconds:
        # 兼容旧的 IntervalTrigger
        scheduler.add_job(
            func=_execute_schedule_task,
            trigger=IntervalTrigger(seconds=schedule.interval_seconds),
            id=job_id,
            args=[schedule.id],
            replace_existing=True,
            next_run_time=next_run_time
        )
        log_time = next_run_time if next_run_time else "interval default"
        logger.info(f"Added schedule {schedule.id} to scheduler with interval {schedule.interval_seconds}s, next_run: {log_time}")
    else:
        logger.warning(f"Schedule {schedule.id} has no cron_expression or interval_seconds, skipping")


def _remove_schedule_from_scheduler(schedule_id: int):
    """从调度器移除任务"""
    job_id = f"schedule_{schedule_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
        logger.info(f"Removed schedule {schedule_id} from scheduler")


def _load_schedules_on_startup():
    """应用启动时加载所有启用的定时任务"""
    db = SessionLocal()
    try:
        schedules = db.scalars(
            select(TestSchedule).where(TestSchedule.enabled == True)
        ).all()
        
        for schedule in schedules:
            now = datetime.now(timezone.utc)
            
            # 对于 cron 表达式的任务，重新计算 next_run_at
            if schedule.cron_expression:
                try:
                    cron = croniter(schedule.cron_expression, now)
                    run_at = cron.get_next(datetime)
                    # 更新数据库中的 next_run_at
                    schedule.next_run_at = run_at
                    db.commit()
                    logger.info(f"Schedule {schedule.id} ({schedule.name}): recalculated next_run from cron '{schedule.cron_expression}' -> {run_at}")
                except Exception as e:
                    logger.error(f"Failed to parse cron for schedule {schedule.id}: {e}")
                    run_at = now + timedelta(minutes=10)
            else:
                # 旧的间隔模式
                run_at = schedule.next_run_at
                if not run_at or run_at <= now:
                    logger.warning(f"Schedule {schedule.id} ({schedule.name}) missed execution or not set. Scheduling immediately.")
                    run_at = now + timedelta(seconds=5)
            
            _add_schedule_to_scheduler(schedule, next_run_time=run_at)
            logger.info(f"Loaded schedule {schedule.id}: {schedule.name}, next run scheduled at {run_at}")
    finally:
        db.close()


async def _execute_schedule_task(schedule_id: int):
    """执行定时任务"""
    print(f">>> EXECUTING SCHEDULE {schedule_id} <<<", flush=True)
    
    db = SessionLocal()
    try:
        schedule = db.get(TestSchedule, schedule_id)
        if not schedule or not schedule.enabled:
            print(f">>> SCHEDULE {schedule_id} NOT FOUND OR DISABLED <<<", flush=True)
            return
        
        print(f">>> SCHEDULE {schedule_id} RUNNING: {schedule.name} <<<", flush=True)
        logger.info(f"Executing schedule {schedule_id}: {schedule.name}")
        
        # 更新执行时间
        schedule.last_run_at = datetime.now(timezone.utc)
        
        # 计算下次运行时间
        if schedule.cron_expression:
            # 使用 croniter 从 cron 表达式计算下次运行时间
            try:
                cron = croniter(schedule.cron_expression, schedule.last_run_at)
                schedule.next_run_at = cron.get_next(datetime)
            except Exception as e:
                logger.error(f"Failed to parse cron expression: {e}")
                schedule.next_run_at = schedule.last_run_at + timedelta(minutes=20)
        elif schedule.interval_seconds:
            # 兼容旧的 interval_seconds
            schedule.next_run_at = schedule.last_run_at + timedelta(seconds=schedule.interval_seconds)
        else:
            # 默认 20 分钟
            schedule.next_run_at = schedule.last_run_at + timedelta(minutes=20)
        
        # 执行测试
        try:
            src_node = db.get(Node, schedule.src_node_id)
            dst_node = db.get(Node, schedule.dst_node_id)
            
            if not src_node or not dst_node:
                raise Exception("Source or destination node not found")
            
            # Get current detected port from health check
            # Priority: detected port from health check > node's configured port > schedule's port
            dst_status = await health_monitor.check_node(dst_node)
            current_port = dst_status.detected_iperf_port or dst_node.iperf_port or schedule.port
            logger.info(f"Schedule {schedule_id} using iperf port {current_port} (detected: {dst_status.detected_iperf_port}, node: {dst_node.iperf_port}, schedule: {schedule.port})")
            
            # Determine protocols to run
            protocols = []
            if schedule.protocol == "tcp_udp":
                protocols = ["tcp", "udp"]
            else:
                protocols = [schedule.protocol]

            # Determine direction params
            direction = getattr(schedule, "direction", "upload") or "upload"
            direction = direction.lower() # Normalize case
            
            # For bidirectional: run TWO SEPARATE tests (upload first, then download)
            # For upload/download: run ONE test with appropriate reverse flag
            if direction == "bidirectional":
                # Two separate tests: first upload (reverse=False), then download (reverse=True)
                direction_tests = [
                    {"direction_name": "upload", "reverse": False},
                    {"direction_name": "download", "reverse": True},
                ]
            elif direction == "download":
                direction_tests = [{"direction_name": "download", "reverse": True}]
            else:  # upload
                direction_tests = [{"direction_name": "upload", "reverse": False}]

            # Use a shared timestamp for all tests in this schedule run (for Chart align)
            execution_time = datetime.now(timezone.utc)
            
            # Check node modes
            src_mode = getattr(src_node, "agent_mode", "normal") or "normal"
            dst_mode = getattr(dst_node, "agent_mode", "normal") or "normal"
            
            # NAT DESTINATION SWAP LOGIC
            # If destination is a reverse mode (NAT) agent, it cannot receive inbound connections.
            # We need to swap the direction: run test FROM the NAT node TO the normal node,
            # and toggle the reverse flag to maintain the correct traffic direction semantics.
            effective_src = src_node
            effective_dst = dst_node
            is_nat_swapped = False
            
            if dst_mode == "reverse" and src_mode != "reverse":
                # Swap: NAT node becomes source, original source becomes destination
                effective_src = dst_node
                effective_dst = src_node
                is_nat_swapped = True
                logger.info(f"[NAT-SWAP] Schedule {schedule_id}: Swapping because {dst_node.name} is NAT. "
                           f"Effective: {effective_src.name} -> {effective_dst.name}")
                
                # Get port from the new destination
                new_dst_status = await health_monitor.check_node(effective_dst)
                current_port = new_dst_status.detected_iperf_port or effective_dst.iperf_port or schedule.port
            
            is_nat_source = (getattr(effective_src, "agent_mode", "normal") or "normal") == "reverse"

            # Iterate over protocols AND directions
            for proto in protocols:
                for dir_test in direction_tests:
                    try:
                        # Calculate effective reverse flag
                        # If NAT swapped, toggle the reverse flag
                        effective_reverse = dir_test["reverse"]
                        if is_nat_swapped:
                            effective_reverse = not effective_reverse
                        
                        direction_label = dir_test["direction_name"]
                        logger.info(f"Schedule {schedule_id}: Running {proto} {direction_label} test (effective_reverse={effective_reverse})")
                        
                        # 构造测试参数
                        # Use effective nodes after NAT swap logic
                        test_params = {
                            "target_ip": effective_dst.ip,
                            "target_port": current_port,
                            "duration": schedule.duration,
                            "protocol": proto,
                            "parallel": schedule.parallel,
                            "reverse": effective_reverse,
                            "bidir": False,  # No longer using --bidir, doing two separate tests
                            "direction_label": direction_label,  # For result tracking
                        }
                    
                        if is_nat_source:
                            # NAT source node: queue task for agent to poll and execute
                            # IMPORTANT: Start iperf server on destination BEFORE queuing task
                            # so the NAT agent can connect when it picks up the task
                            print(f"[NAT-SCHEDULE-DEBUG] Schedule {schedule_id}: effective_src={effective_src.name}, effective_dst={effective_dst.name} (IP: {effective_dst.ip}), requested_port={current_port}", flush=True)
                            logger.info(f"[NAT-SCHEDULE-DEBUG] Schedule {schedule_id}: effective_src={effective_src.name}, effective_dst={effective_dst.name} (IP: {effective_dst.ip}), requested_port={current_port}")
                            try:
                                server_port = await _ensure_iperf_server_running(effective_dst, current_port)
                                # Update test_params with the actual port the server is running on
                                test_params["target_port"] = server_port
                                print(f"[NAT-SCHEDULE] Started iperf server on {effective_dst.name}:{server_port} for NAT agent {effective_src.name}, task target_ip={test_params['target_ip']}", flush=True)
                                logger.info(f"[NAT-SCHEDULE] Started iperf server on {effective_dst.name}:{server_port} for NAT agent {effective_src.name}")
                            except Exception as e:
                                print(f"[NAT-SCHEDULE] FAILED to start iperf server on {effective_dst.name}: {e}", flush=True)
                                logger.error(f"[NAT-SCHEDULE] Failed to start iperf server on {effective_dst.name}: {e}")
                                raise
                            
                            pending_task = PendingTask(
                                node_name=effective_src.name,
                                task_type="iperf_test",
                                task_data=test_params,
                                schedule_id=schedule_id,
                                status="pending",
                            )
                            db.add(pending_task)
                            db.flush()  # Get pending_task.id
                            
                            # Record pending schedule result immediately
                            schedule_result = ScheduleResult(
                                schedule_id=schedule_id,
                                test_result_id=None,
                                executed_at=execution_time,
                                status="pending",
                                error_message=f"Queued as task #{pending_task.id} for NAT agent {effective_src.name}",
                            )
                            db.add(schedule_result)
                            db.commit()
                            
                            logger.info(f"[REVERSE] Schedule {schedule_id} ({proto}) queued as PendingTask #{pending_task.id} for NAT agent {effective_src.name}")
                            # Don't wait for result - agent will report via /api/agent/result
                            continue
                        
                        # Normal source node: call agent directly
                        # Adjust params format for _call_agent_test (uses "target" not "target_ip")
                        call_params = {
                            "target": test_params["target_ip"],
                            "port": test_params["target_port"],
                            "duration": test_params["duration"],
                            "protocol": test_params["protocol"],
                            "parallel": test_params["parallel"],
                            "reverse": test_params["reverse"],
                            "bidir": test_params.get("bidir", False),
                        }
                        raw_data = await _call_agent_test(effective_src, call_params, schedule.duration)
                        summary = _summarize_metrics(raw_data, direction_label=direction_label)
                        # Note: direction filtering is now handled inside _summarize_metrics

                        
                        # 保存测试结果
                        test_result = TestResult(
                            src_node_id=schedule.src_node_id,
                            dst_node_id=schedule.dst_node_id,
                            protocol=proto,
                            params=test_params,
                            raw_result=raw_data,
                            summary=summary,
                            created_at=execution_time,
                        )
                        db.add(test_result)
                        db.flush()
                        
                        # 保存schedule结果
                        schedule_result = ScheduleResult(
                            schedule_id=schedule_id,
                            test_result_id=test_result.id,
                            executed_at=execution_time,
                            status="success",
                        )
                        db.add(schedule_result)
                        db.commit()
                        
                        logger.info(f"Schedule {schedule_id} ({proto}) executed successfully")

                    except Exception as inner_e:
                        logger.error(f"Schedule {schedule_id} ({proto}) execution failed: {inner_e}")
                        # Keep going for next protocol if any
                        schedule_result = ScheduleResult(
                            schedule_id=schedule_id,
                            test_result_id=None,
                            executed_at=datetime.now(timezone.utc),
                            status="failed",
                            error_message=f"{proto}: {str(inner_e)}",
                        )
                        db.add(schedule_result)
                        db.commit()

        except Exception as e:
            logger.error(f"Schedule {schedule_id} setup failed: {e}")
            
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
            # Check for missed run or invalid next_run_at
            run_at = schedule.next_run_at
            now = datetime.now(timezone.utc)
            
            # If never run or next run is in the past (missed run due to downtime), run immediately
            if not run_at or run_at <= now:
                logger.warning(f"Schedule {schedule.id} ({schedule.name}) missed execution or not set. Scheduling immediately.")
                from datetime import timedelta
                run_at = now + timedelta(seconds=5) # Add small buffer
                # Note: We don't update DB here, the execution task will update next_run_at
            
            _add_schedule_to_scheduler(schedule, next_run_time=run_at)
            logger.info(f"Loaded schedule {schedule.id}: {schedule.name}, next run scheduled at {run_at}")
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
        cron_expression=schedule.cron_expression,
        enabled=schedule.enabled,
        direction=schedule.direction,
        udp_bandwidth=schedule.udp_bandwidth,
        notes=schedule.notes,
    )
    
    # 计算下次执行时间
    if schedule.enabled:
        now = datetime.now(timezone.utc)
        if schedule.cron_expression:
            # 验证并使用 croniter 从 cron 表达式计算下次运行时间
            try:
                cron = croniter(schedule.cron_expression, now)
                db_schedule.next_run_at = cron.get_next(datetime)
                logger.info(f"Calculated next run from cron '{schedule.cron_expression}': {db_schedule.next_run_at}")
            except Exception as e:
                logger.error(f"Invalid cron expression '{schedule.cron_expression}': {e}")
                raise HTTPException(
                    status_code=400, 
                    detail=f"无效的 cron 表达式: {schedule.cron_expression}. 错误: {str(e)}"
                )
        elif schedule.interval_seconds:
            # 兼容旧的 interval_seconds
            db_schedule.next_run_at = now + timedelta(seconds=schedule.interval_seconds)
        else:
            # 默认 20 分钟
            db_schedule.next_run_at = now + timedelta(minutes=20)
    
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
    
    # 动态重新计算所有启用任务的 next_run_at
    now = datetime.now(timezone.utc)
    for schedule in schedules:
        if not schedule.enabled:
            continue
            
        if schedule.cron_expression:
            # Cron 表达式模式
            try:
                cron = croniter(schedule.cron_expression, now)
                schedule.next_run_at = cron.get_next(datetime)
            except Exception as e:
                logger.error(f"Failed to calculate next_run for schedule {schedule.id}: {e}")
        elif schedule.interval_seconds:
            # Interval 模式：基于 last_run_at 或当前时间计算下次运行
            if schedule.last_run_at:
                # 确保 last_run_at 是 timezone-aware (假设存储的是 UTC)
                last_run = schedule.last_run_at
                if last_run.tzinfo is None:
                    last_run = last_run.replace(tzinfo=timezone.utc)
                
                # 从上次运行时间开始计算
                next_run = last_run + timedelta(seconds=schedule.interval_seconds)
                # 如果计算出的下次时间已经过去，则循环添加间隔直到找到未来时间
                while next_run <= now:
                    next_run += timedelta(seconds=schedule.interval_seconds)
                schedule.next_run_at = next_run
            else:
                # 如果从未运行过，设置为当前时间后的一个间隔
                schedule.next_run_at = now + timedelta(seconds=schedule.interval_seconds)
    
    # 提交更新到数据库
    try:
        db.commit()
    except Exception:
        db.rollback()
    
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
    
    # 删除相关的 PendingTask 记录
    db.execute(
        text("DELETE FROM pending_tasks WHERE schedule_id = :sid"),
        {"sid": schedule_id}
    )
    
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
        # 计算下次执行时间
        now = datetime.now(timezone.utc)
        if db_schedule.cron_expression:
            # 使用 croniter 从 cron 表达式计算
            try:
                cron = croniter(db_schedule.cron_expression, now)
                next_run_at = cron.get_next(datetime)
            except Exception as e:
                logger.error(f"Failed to parse cron for toggle: {e}")
                next_run_at = now + timedelta(minutes=10)
        elif db_schedule.interval_seconds:
            next_run_at = now + timedelta(seconds=db_schedule.interval_seconds)
        else:
            next_run_at = now + timedelta(minutes=10)
        
        db_schedule.next_run_at = next_run_at
        # 关键修复：传递next_run_time参数到调度器
        _add_schedule_to_scheduler(db_schedule, next_run_time=next_run_at)
        logger.info(f"Enabled schedule {schedule_id}, next run at {next_run_at}")
    else:
        db_schedule.next_run_at = None
        _remove_schedule_from_scheduler(schedule_id)
        logger.info(f"Disabled schedule {schedule_id}")
    
    db.commit()
    db.refresh(db_schedule)
    _persist_state(db)
    
    return {
        "enabled": db_schedule.enabled, 
        "schedule_id": schedule_id,
        "next_run_at": db_schedule.next_run_at.isoformat() if db_schedule.next_run_at else None
    }


@app.get("/debug/scheduler")
def debug_scheduler():
    """调试端点：显示APScheduler中当前注册的任务"""
    jobs = scheduler.get_jobs()
    return {
        "scheduler_running": scheduler.running,
        "job_count": len(jobs),
        "jobs": [
            {
                "id": job.id,
                "name": job.name,
                "next_run_time": job.next_run_time.isoformat() if job.next_run_time else None,
                "trigger": str(job.trigger),
                "func": str(job.func),
            }
            for job in jobs
        ]
    }

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


# ============================================================================
# Reverse Mode (NAT) Agent API Endpoints
# ============================================================================

class AgentRegisterRequest(BaseModel):
    """Request model for agent registration."""
    node_name: str
    iperf_port: int = 5201
    agent_version: str | None = None
    mode: str = "reverse"


@app.post("/api/agent/register")
async def agent_register(
    request: Request,
    payload: AgentRegisterRequest,
    db: Session = Depends(get_db)
):
    """
    Register a reverse mode (NAT) agent with the master.
    Called periodically by agents behind NAT to maintain heartbeat.
    """
    # DEBUG: Print to stdout at very start
    print(f"[REGISTER] === AGENT REGISTRATION RECEIVED ===", flush=True)
    print(f"[REGISTER] node={payload.node_name}, mode={payload.mode}, version={payload.agent_version}", flush=True)
    
    node_name = payload.node_name
    iperf_port = payload.iperf_port
    agent_version = payload.agent_version
    mode = payload.mode
    
    # Get client IP from request
    client_ip = request.client.host if request.client else "unknown"
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        client_ip = forwarded.split(",")[0].strip()
    
    print(f"[REGISTER] client_ip={client_ip}", flush=True)
    
    # Find or create node
    node = db.scalars(select(Node).where(Node.name == node_name)).first()
    
    if node:
        # Update existing node
        print(f"[REGISTER] Updating existing node: {node_name} (id={node.id})", flush=True)
        node.last_heartbeat = datetime.now(timezone.utc)
        node.agent_version = agent_version
        node.agent_mode = mode
        node.iperf_port = iperf_port
        # Update IP if it changed (NAT agents may have dynamic IPs)
        if node.ip != client_ip and client_ip != "unknown":
            print(f"[REGISTER] IP changed: {node.ip} -> {client_ip}", flush=True)
            node.ip = client_ip
    else:
        # Create new node for this agent
        print(f"[REGISTER] Creating new node: {node_name}", flush=True)
        node = Node(
            name=node_name,
            ip=client_ip,
            agent_port=8000,  # Not used for reverse mode
            iperf_port=iperf_port,
            agent_mode=mode,
            agent_version=agent_version,
            last_heartbeat=datetime.now(timezone.utc),
            is_internal=True,  # NAT agents are internal
        )
        db.add(node)
    
    db.commit()
    db.refresh(node)
    
    # Invalidate health monitor cache to ensure fresh node data is used
    health_monitor.invalidate(node.id)
    
    print(f"[REGISTER] Success: node_id={node.id}, agent_mode={node.agent_mode}", flush=True)
    
    # Check if agent needs update
    update_available = False
    update_info = None
    
    if agent_version:
        try:
            # Import compare_versions locally to avoid circular import issues
            def _compare_versions(v1: str, v2: str) -> int:
                import re
                def parse_ver(v):
                    if not v:
                        return (0, 0, 0)
                    v = v.lstrip('v')
                    parts = v.split('.')
                    result = []
                    for p in parts[:3]:
                        m = re.match(r'(\d+)', p)
                        result.append(int(m.group(1)) if m else 0)
                    while len(result) < 3:
                        result.append(0)
                    return tuple(result)
                t1, t2 = parse_ver(v1), parse_ver(v2)
                return 1 if t1 > t2 else (-1 if t1 < t2 else 0)
            
            comparison = _compare_versions(EXPECTED_AGENT_VERSION, agent_version)
            
            # Check if this agent was pending update and now has the correct version
            if comparison == 0 and node.update_status == "pending":
                # Agent successfully updated!
                node.update_status = "updated"
                node.update_message = f"Auto-updated to v{agent_version}"
                node.update_at = datetime.now(timezone.utc)
                db.commit()
                print(f"[REGISTER] Agent {node_name} successfully auto-updated to v{agent_version}", flush=True)
            elif comparison > 0:
                update_available = True
                update_info = {
                    "target_version": EXPECTED_AGENT_VERSION,
                    "agent_image": settings.agent_image,
                    "message": f"Agent update available: {agent_version} -> {EXPECTED_AGENT_VERSION}"
                }
                print(f"[REGISTER] Update available for {node_name}: {agent_version} -> {EXPECTED_AGENT_VERSION}", flush=True)
                
                # Record update notification in database
                node.update_status = "pending"
                node.update_message = f"Update to v{EXPECTED_AGENT_VERSION} notified"
                node.update_at = datetime.now(timezone.utc)
                db.commit()
        except Exception as e:
            print(f"[REGISTER] Version check error: {e}", flush=True)
    
    response = {
        "status": "ok",
        "node_id": node.id,
        "node_name": node.name,
        "agent_mode": node.agent_mode,
        "message": "registered",
        # Update notification for reverse agents
        "update_available": update_available,
        "update_info": update_info
    }
    
    return response


@app.get("/api/agent/tasks")
async def agent_get_tasks(
    node_name: str = Query(...),
    db: Session = Depends(get_db)
):
    """
    Get pending tasks for a reverse mode (NAT) agent.
    Agent polls this endpoint to receive tasks to execute.
    Also returns the current whitelist for the agent to sync.
    """
    # Find pending tasks for this agent
    tasks = db.scalars(
        select(PendingTask)
        .where(PendingTask.node_name == node_name)
        .where(PendingTask.status == "pending")
        .order_by(PendingTask.created_at)
        .limit(10)  # Limit to prevent overload
    ).all()
    
    # Mark tasks as claimed
    task_list = []
    now = datetime.now(timezone.utc)
    for task in tasks:
        task.status = "claimed"
        task.claimed_at = now
        task_list.append({
            "id": task.id,
            "type": task.task_type,
            "target_ip": task.task_data.get("target_ip"),
            "target_port": task.task_data.get("target_port", 5201),
            "duration": task.task_data.get("duration", 10),
            "protocol": task.task_data.get("protocol", "tcp"),
            "parallel": task.task_data.get("parallel", 1),
            "reverse": task.task_data.get("reverse", True),  # Default to reverse for NAT agents
            "bandwidth": task.task_data.get("bandwidth"),
            "schedule_id": task.schedule_id,
        })
    
    db.commit()
    
    if task_list:
        logger.info(f"[REVERSE] Agent {node_name} claimed {len(task_list)} tasks")
    
    # Include whitelist in response for reverse agents to sync
    nodes = db.scalars(select(Node)).all()
    whitelist = [n.ip for n in nodes if n.ip]
    
    # Add master's own IP
    master_ip = os.getenv("MASTER_IP", "")
    if not master_ip:
        try:
            import httpx
            resp = httpx.get("https://api.ipify.org", timeout=5)
            if resp.status_code == 200:
                master_ip = resp.text.strip()
        except Exception:
            pass
    if master_ip and master_ip not in whitelist:
        whitelist.append(master_ip)
    
    # Update node's whitelist sync status
    node = db.scalars(select(Node).where(Node.name == node_name)).first()
    if node:
        node.whitelist_sync_status = "synced"
        node.whitelist_sync_at = now
        db.commit()
    
    return {"tasks": task_list, "whitelist": whitelist}


@app.post("/api/agent/result")
async def agent_report_result(
    task_id: int = Body(...),
    result: dict = Body(...),
    db: Session = Depends(get_db)
):
    """
    Report task execution result from a reverse mode (NAT) agent.
    """
    task = db.get(PendingTask, task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    
    now = datetime.now(timezone.utc)
    task.completed_at = now
    task.result_data = result
    
    if result.get("status") == "ok":
        task.status = "completed"
        
        # If this task is linked to a schedule, create TestResult and ScheduleResult
        if task.schedule_id:
            schedule = db.get(TestSchedule, task.schedule_id)
            if schedule:
                try:
                    iperf_data = result.get("iperf_result", {})
                    # Get direction_label from task_data for proper UDP direction assignment
                    direction_label = task.task_data.get("direction_label")
                    summary = _summarize_metrics({"iperf_result": iperf_data} if iperf_data else result, direction_label=direction_label)
                    
                    test_result = TestResult(
                        src_node_id=schedule.src_node_id,
                        dst_node_id=schedule.dst_node_id,
                        protocol=task.task_data.get("protocol", "tcp"),
                        params=task.task_data,
                        raw_result=result,
                        summary=summary,
                        created_at=now,
                    )
                    db.add(test_result)
                    db.flush()
                    
                    schedule_result = ScheduleResult(
                        schedule_id=task.schedule_id,
                        test_result_id=test_result.id,
                        executed_at=now,
                        status="success",
                    )
                    db.add(schedule_result)
                    
                    logger.info(f"[REVERSE] Task {task_id} completed, TestResult {test_result.id} created")
                except Exception as e:
                    logger.error(f"[REVERSE] Failed to create TestResult for task {task_id}: {e}")
                    schedule_result = ScheduleResult(
                        schedule_id=task.schedule_id,
                        test_result_id=None,
                        executed_at=now,
                        status="failed",
                        error_message=f"Failed to process result: {e}",
                    )
                    db.add(schedule_result)
    else:
        task.status = "failed"
        task.error_message = result.get("error", "Unknown error")
        
        # Create failed ScheduleResult if linked to schedule
        if task.schedule_id:
            schedule_result = ScheduleResult(
                schedule_id=task.schedule_id,
                test_result_id=None,
                executed_at=now,
                status="failed",
                error_message=task.error_message,
            )
            db.add(schedule_result)
        
        logger.warning(f"[REVERSE] Task {task_id} failed: {task.error_message}")
    
    db.commit()
    
    return {"status": "ok", "task_id": task_id}


@app.get("/api/agent/pending_count")
async def agent_pending_count(
    node_name: str = Query(None),
    db: Session = Depends(get_db)
):
    """
    Get count of pending tasks, optionally filtered by node.
    Useful for monitoring task queue status.
    """
    query = select(PendingTask).where(PendingTask.status == "pending")
    if node_name:
        query = query.where(PendingTask.node_name == node_name)
    
    tasks = db.scalars(query).all()
    
    return {
        "pending_count": len(tasks),
        "node_name": node_name
    }



# --- SALVAGED CODE SPLICE 2 ---

class AlertConfigUpdate(BaseModel):
    """Request model for updating alert configuration."""
    key: str  # "telegram", "webhook", "thresholds"
    value: dict
    enabled: bool = True


@app.get("/api/alerts/config")
async def get_alert_configs(db: Session = Depends(get_db)):
    """Get all alert configurations."""
    configs = db.scalars(select(AlertConfig)).all()
    return {
        "status": "ok",
        "configs": {c.key: {"value": c.value, "enabled": c.enabled} for c in configs}
    }


@app.post("/api/alerts/config")
async def update_alert_config(payload: AlertConfigUpdate, db: Session = Depends(get_db)):
    """Update alert configuration."""
    config = db.query(AlertConfig).filter(AlertConfig.key == payload.key).first()
    if config:
        config.value = payload.value
        config.enabled = payload.enabled
    else:
        config = AlertConfig(key=payload.key, value=payload.value, enabled=payload.enabled)
        db.add(config)
    
    db.commit()
    return {"status": "ok", "message": f"Alert config '{payload.key}' updated"}


class TestAlertRequest(BaseModel):
    """Request model for testing alert notification."""
    channel: str = "telegram"  # "telegram" or "webhook"


@app.post("/api/alerts/test")
async def test_alert_notification(payload: TestAlertRequest, db: Session = Depends(get_db)):
    """Send a test notification to verify configuration."""
    # Get config
    config = db.query(AlertConfig).filter(AlertConfig.key == payload.channel).first()
    if not config or not config.enabled:
        return {"status": "error", "message": f"Channel '{payload.channel}' not configured or disabled"}
    
    message = alert_service.format_alert_message(
        alert_type="test_alert",
        severity="info",
        node_name="测试节点",
        message="这是一条测试告警消息。如果您看到此消息，说明告警配置正确！",
        details={"current_value": "N/A", "threshold": "N/A"}
    )
    
    success = False
    if payload.channel == "telegram":
        bot_token = config.value.get("bot_token")
        chat_id = config.value.get("chat_id")
        success = await alert_service.send_telegram(bot_token, chat_id, message)
    elif payload.channel == "webhook":
        webhook_url = config.value.get("url")
        webhook_payload = alert_service.format_webhook_payload(
            alert_type="test_alert",
            severity="info",
            node_id=0,
            node_name="测试节点",
            message="这是一条测试告警消息",
            details={}
        )
        success = await alert_service.send_webhook(webhook_url, webhook_payload)
    
    if success:
        return {"status": "ok", "message": "测试消息发送成功！"}
    else:
        return {"status": "error", "message": "发送失败，请检查配置"}


@app.get("/api/alerts/history")
async def get_alert_history(limit: int = 50, db: Session = Depends(get_db)):
    """Get alert history."""
    alerts = db.scalars(
        select(AlertHistory)
        .order_by(AlertHistory.created_at.desc())
        .limit(limit)
    ).all()
    
    return {
        "status": "ok",
        "alerts": [
            {
                "id": a.id,
                "alert_type": a.alert_type,
                "severity": a.severity,
                "node_id": a.node_id,
                "node_name": a.node_name,
                "message": a.message,
                "channels_sent": a.channels_sent,
                "is_resolved": a.is_resolved,
                "created_at": a.created_at.isoformat() if a.created_at else None
            }
            for a in alerts
        ]
    }


async def trigger_alert(
    db: Session,
    alert_type: str,
    severity: str,
    node_id: int | None,
    node_name: str,
    message: str,
    details: dict = None
):
    """Core function to trigger an alert - checks rate limit, sends notification, logs history."""
    # Check rate limit
    if alert_service.is_rate_limited(alert_type, node_id):
        return False
    
    channels_sent = []
    
    # Get configs
    telegram_config = db.query(AlertConfig).filter(AlertConfig.key == "telegram").first()
    webhook_config = db.query(AlertConfig).filter(AlertConfig.key == "webhook").first()
    
    # Send Telegram
    if telegram_config and telegram_config.enabled and telegram_config.value:
        bot_token = telegram_config.value.get("bot_token")
        chat_id = telegram_config.value.get("chat_id")
        if bot_token and chat_id:
            formatted_msg = alert_service.format_alert_message(
                alert_type, severity, node_name, message, details
            )
            if await alert_service.send_telegram(bot_token, chat_id, formatted_msg):
                channels_sent.append("telegram")
    
    # Send Webhook
    if webhook_config and webhook_config.enabled and webhook_config.value:
        webhook_url = webhook_config.value.get("url")
        if webhook_url:
            payload = alert_service.format_webhook_payload(
                alert_type, severity, node_id, node_name, message, details
            )
            if await alert_service.send_webhook(webhook_url, payload):
                channels_sent.append("webhook")
    
    # Log to history if any channel was sent
    if channels_sent:
        history = AlertHistory(
            alert_type=alert_type,
            severity=severity,
            node_id=node_id,
            node_name=node_name,
            message=message,
            details=details,
            channels_sent=channels_sent
        )
        db.add(history)
        db.commit()
        alert_service.record_alert_sent(alert_type, node_id)
        return True
    
    return False

