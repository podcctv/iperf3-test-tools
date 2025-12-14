"""
Schedule Manager for Agent - Handles local cron-based scheduling
"""
import json
import os
import shlex
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from croniter import croniter

# Configuration
SCHEDULES_FILE = Path(os.getenv("SCHEDULES_FILE", "/app/data/schedules.json"))
MASTER_URL = os.getenv("MASTER_URL", "")
NODE_NAME = os.getenv("NODE_NAME", "")

# Global scheduler instance
_scheduler: Optional[BackgroundScheduler] = None
_schedules: Dict[int, Dict[str, Any]] = {}
_schedule_lock = threading.Lock()


def _ensure_data_dir():
    """Ensure data directory exists"""
    SCHEDULES_FILE.parent.mkdir(parents=True, exist_ok=True)


def _load_schedules() -> Dict[int, Dict[str, Any]]:
    """Load schedules from persistent storage"""
    global _schedules
    if SCHEDULES_FILE.exists():
        try:
            data = json.loads(SCHEDULES_FILE.read_text())
            _schedules = {int(k): v for k, v in data.items()}
            print(f"[SCHEDULER] Loaded {len(_schedules)} schedules from {SCHEDULES_FILE}", flush=True)
            return _schedules
        except Exception as e:
            print(f"[SCHEDULER] Failed to load schedules: {e}", flush=True)
    return {}


def _save_schedules():
    """Save schedules to persistent storage"""
    _ensure_data_dir()
    try:
        SCHEDULES_FILE.write_text(json.dumps(_schedules, indent=2, default=str))
        print(f"[SCHEDULER] Saved {len(_schedules)} schedules to {SCHEDULES_FILE}", flush=True)
    except Exception as e:
        print(f"[SCHEDULER] Failed to save schedules: {e}", flush=True)


def _execute_iperf_test(schedule: Dict[str, Any]) -> Dict[str, Any]:
    """Execute an iperf3 test based on schedule configuration"""
    target_ip = schedule.get("target_ip")
    target_port = schedule.get("target_port", 5201)
    protocol = schedule.get("protocol", "tcp")
    duration = schedule.get("duration", 10)
    parallel = schedule.get("parallel", 1)
    reverse = schedule.get("reverse", False)
    bidir = schedule.get("bidir", False)
    bandwidth = schedule.get("bandwidth")
    
    # Build iperf3 command
    cmd_parts = [
        "iperf3", "-c", str(target_ip), "-p", str(target_port),
        "-t", str(duration), "-P", str(parallel), "-J"
    ]
    
    if protocol == "udp":
        cmd_parts.append("-u")
        if bandwidth:
            cmd_parts.extend(["-b", str(bandwidth)])
    
    if reverse:
        cmd_parts.append("-R")
        cmd_parts.append("--get-server-output")
    
    if bidir:
        cmd_parts.append("--bidir")
    
    print(f"[SCHEDULER] Executing: {' '.join(cmd_parts)}", flush=True)
    
    try:
        result = subprocess.run(
            cmd_parts,
            capture_output=True,
            text=True,
            timeout=duration + 30
        )
        
        if result.returncode != 0:
            error_msg = result.stderr.strip() or result.stdout.strip() or f"iperf3 exit code {result.returncode}"
            return {"status": "error", "error": error_msg}
        
        output = json.loads(result.stdout)
        return {"status": "ok", "iperf_result": output}
        
    except subprocess.TimeoutExpired:
        return {"status": "error", "error": "Test execution timed out"}
    except json.JSONDecodeError as e:
        return {"status": "error", "error": f"Failed to parse iperf3 output: {e}"}
    except Exception as e:
        return {"status": "error", "error": str(e)}


def _report_result_to_master(schedule_id: int, result: Dict[str, Any]):
    """Report test result back to master"""
    if not MASTER_URL:
        print(f"[SCHEDULER] No MASTER_URL configured, skipping result report", flush=True)
        return
    
    report_url = f"{MASTER_URL.rstrip('/')}/api/schedule/result"
    payload = {
        "schedule_id": schedule_id,
        "node_name": NODE_NAME,
        "result": result,
        "executed_at": datetime.now(timezone.utc).isoformat()
    }
    
    try:
        resp = requests.post(report_url, json=payload, timeout=30)
        if resp.ok:
            print(f"[SCHEDULER] Result reported for schedule {schedule_id}", flush=True)
        else:
            print(f"[SCHEDULER] Failed to report result: {resp.status_code} - {resp.text[:200]}", flush=True)
    except Exception as e:
        print(f"[SCHEDULER] Report error: {e}", flush=True)


def _run_scheduled_job(schedule_id: int):
    """Job function executed by APScheduler"""
    with _schedule_lock:
        schedule = _schedules.get(schedule_id)
        if not schedule:
            print(f"[SCHEDULER] Schedule {schedule_id} not found, skipping", flush=True)
            return
        
        if not schedule.get("enabled", True):
            print(f"[SCHEDULER] Schedule {schedule_id} is disabled, skipping", flush=True)
            return
    
    schedule_name = schedule.get("name", f"Schedule-{schedule_id}")
    print(f"[SCHEDULER] Running job: {schedule_name} (ID: {schedule_id})", flush=True)
    
    # Execute the test
    result = _execute_iperf_test(schedule)
    
    # Update last run time
    with _schedule_lock:
        if schedule_id in _schedules:
            _schedules[schedule_id]["last_run_at"] = datetime.now(timezone.utc).isoformat()
            _save_schedules()
    
    # Report result to master
    _report_result_to_master(schedule_id, result)
    
    print(f"[SCHEDULER] Job completed: {schedule_name} - Status: {result.get('status')}", flush=True)


def _add_job_to_scheduler(schedule_id: int, schedule: Dict[str, Any]):
    """Add a schedule as a cron job to APScheduler"""
    global _scheduler
    if not _scheduler:
        return
    
    cron_expr = schedule.get("cron_expression")
    if not cron_expr:
        print(f"[SCHEDULER] Schedule {schedule_id} has no cron expression, skipping", flush=True)
        return
    
    job_id = f"schedule_{schedule_id}"
    
    # Remove existing job if any
    try:
        _scheduler.remove_job(job_id)
    except:
        pass
    
    if not schedule.get("enabled", True):
        print(f"[SCHEDULER] Schedule {schedule_id} is disabled, not adding job", flush=True)
        return
    
    try:
        # Parse cron expression: "minute hour day month day_of_week"
        parts = cron_expr.strip().split()
        if len(parts) >= 5:
            trigger = CronTrigger(
                minute=parts[0],
                hour=parts[1],
                day=parts[2],
                month=parts[3],
                day_of_week=parts[4]
            )
            _scheduler.add_job(
                _run_scheduled_job,
                trigger=trigger,
                args=[schedule_id],
                id=job_id,
                name=schedule.get("name", f"Schedule-{schedule_id}"),
                replace_existing=True
            )
            print(f"[SCHEDULER] Added job {job_id} with cron: {cron_expr}", flush=True)
        else:
            print(f"[SCHEDULER] Invalid cron expression for schedule {schedule_id}: {cron_expr}", flush=True)
    except Exception as e:
        print(f"[SCHEDULER] Failed to add job {schedule_id}: {e}", flush=True)


def init_scheduler():
    """Initialize the background scheduler"""
    global _scheduler
    
    if _scheduler is not None:
        return
    
    _scheduler = BackgroundScheduler(timezone="UTC")
    _load_schedules()
    
    # Add all loaded schedules as jobs
    for schedule_id, schedule in _schedules.items():
        _add_job_to_scheduler(schedule_id, schedule)
    
    _scheduler.start()
    print(f"[SCHEDULER] Scheduler started with {len(_schedules)} schedules", flush=True)


def shutdown_scheduler():
    """Shutdown the scheduler"""
    global _scheduler
    if _scheduler:
        _scheduler.shutdown(wait=False)
        _scheduler = None
        print("[SCHEDULER] Scheduler stopped", flush=True)


def add_schedule(schedule_id: int, schedule_data: Dict[str, Any]) -> bool:
    """Add or update a schedule"""
    with _schedule_lock:
        _schedules[schedule_id] = schedule_data
        _save_schedules()
    
    _add_job_to_scheduler(schedule_id, schedule_data)
    return True


def remove_schedule(schedule_id: int) -> bool:
    """Remove a schedule"""
    global _scheduler
    
    with _schedule_lock:
        if schedule_id in _schedules:
            del _schedules[schedule_id]
            _save_schedules()
    
    if _scheduler:
        job_id = f"schedule_{schedule_id}"
        try:
            _scheduler.remove_job(job_id)
            print(f"[SCHEDULER] Removed job {job_id}", flush=True)
        except:
            pass
    
    return True


def get_schedules() -> Dict[int, Dict[str, Any]]:
    """Get all schedules"""
    with _schedule_lock:
        return dict(_schedules)


def get_schedule(schedule_id: int) -> Optional[Dict[str, Any]]:
    """Get a specific schedule"""
    with _schedule_lock:
        return _schedules.get(schedule_id)


def get_next_run_time(schedule_id: int) -> Optional[str]:
    """Get the next run time for a schedule"""
    global _scheduler
    if not _scheduler:
        return None
    
    job_id = f"schedule_{schedule_id}"
    try:
        job = _scheduler.get_job(job_id)
        if job and job.next_run_time:
            return job.next_run_time.isoformat()
    except:
        pass
    
    return None


def get_scheduler_status() -> Dict[str, Any]:
    """Get scheduler status"""
    global _scheduler
    
    jobs = []
    if _scheduler:
        for job in _scheduler.get_jobs():
            jobs.append({
                "id": job.id,
                "name": job.name,
                "next_run_time": job.next_run_time.isoformat() if job.next_run_time else None
            })
    
    return {
        "running": _scheduler is not None and _scheduler.running if _scheduler else False,
        "schedule_count": len(_schedules),
        "job_count": len(jobs),
        "jobs": jobs
    }
