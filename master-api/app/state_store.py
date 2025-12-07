from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from .constants import DEFAULT_IPERF_PORT
from .models import Node, TestResult, TestSchedule


@dataclass
class StateStore:
    path: Path
    recent_tests_limit: int = 50

    def _ensure_dir(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            with self.path.open("r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            return {}

    def snapshot(self, db: Session) -> dict[str, Any]:
        nodes = [
            {
                "id": node.id,
                "name": node.name,
                "ip": node.ip,
                "agent_port": node.agent_port,
                "iperf_port": node.iperf_port,
                "description": node.description,
            }
            for node in db.scalars(select(Node)).all()
        ]

        tests = [
            {
                "id": test.id,
                "src_node_id": test.src_node_id,
                "dst_node_id": test.dst_node_id,
                "protocol": test.protocol,
                "params": test.params,
                "raw_result": test.raw_result,
                "summary": test.summary,
                "created_at": test.created_at.isoformat() if test.created_at else None,
            }
            for test in self._recent_tests(db)
        ]

        schedules = [
            {
                "id": schedule.id,
                "name": schedule.name,
                "src_node_id": schedule.src_node_id,
                "dst_node_id": schedule.dst_node_id,
                "protocol": schedule.protocol,
                "duration": schedule.duration,
                "parallel": schedule.parallel,
                "port": schedule.port,
                "interval_seconds": schedule.interval_seconds,
                "enabled": schedule.enabled,
                "notes": schedule.notes,
                "last_run_at": schedule.last_run_at.isoformat() if schedule.last_run_at else None,
                "next_run_at": schedule.next_run_at.isoformat() if schedule.next_run_at else None,
            }
            for schedule in db.scalars(select(TestSchedule)).all()
        ]

        return {
            "version": 1,
            "nodes": nodes,
            "recent_tests": tests,
            "schedules": schedules,
        }

    def persist(self, db: Session) -> None:
        snapshot = self.snapshot(db)
        self._ensure_dir()
        with self.path.open("w", encoding="utf-8") as f:
            json.dump(snapshot, f, indent=2, ensure_ascii=False)

    def restore(self, db: Session) -> None:
        data = self.load()
        if not data:
            return

        if self._has_existing_data(db):
            return

        for node_data in data.get("nodes", []):
            node = Node(
                name=node_data["name"],
                ip=node_data["ip"],
                agent_port=node_data.get("agent_port", 8000),
                iperf_port=node_data.get("iperf_port", DEFAULT_IPERF_PORT),
                description=node_data.get("description"),
            )
            db.add(node)
        db.commit()

        for test_data in data.get("recent_tests", []):
            created_at = test_data.get("created_at")
            parsed_created_at = None
            if created_at:
                try:
                    parsed_created_at = datetime.fromisoformat(created_at)
                except ValueError:
                    parsed_created_at = None

            test = TestResult(
                src_node_id=test_data["src_node_id"],
                dst_node_id=test_data["dst_node_id"],
                protocol=test_data.get("protocol", "tcp"),
                params=test_data.get("params", {}),
                raw_result=test_data.get("raw_result"),
                summary=test_data.get("summary"),
                created_at=parsed_created_at,
            )
            db.add(test)
        db.commit()

        for sched in data.get("schedules", []):
            last_run_at = sched.get("last_run_at")
            next_run_at = sched.get("next_run_at")
            parsed_last = datetime.fromisoformat(last_run_at) if last_run_at else None
            parsed_next = datetime.fromisoformat(next_run_at) if next_run_at else None
            schedule = TestSchedule(
                name=sched["name"],
                src_node_id=sched["src_node_id"],
                dst_node_id=sched["dst_node_id"],
                protocol=sched.get("protocol", "tcp"),
                duration=sched.get("duration", 10),
                parallel=sched.get("parallel", 1),
                port=sched.get("port", DEFAULT_IPERF_PORT),
                interval_seconds=sched.get("interval_seconds", 3600),
                enabled=sched.get("enabled", True),
                notes=sched.get("notes"),
                last_run_at=parsed_last,
                next_run_at=parsed_next,
            )
            db.add(schedule)
        db.commit()

    def _has_existing_data(self, db: Session) -> bool:
        has_nodes = bool(db.scalars(select(Node)).first())
        has_tests = bool(db.scalars(select(TestResult)).first())
        has_schedules = bool(db.scalars(select(TestSchedule)).first())
        return has_nodes or has_tests or has_schedules

    def _recent_tests(self, db: Session) -> Iterable[TestResult]:
        results = db.scalars(select(TestResult)).all()
        sorted_results = sorted(
            results,
            key=lambda r: r.created_at or datetime.fromtimestamp(0),
            reverse=True,
        )
        return sorted_results[: self.recent_tests_limit]
