"""Utilities for storing and retrieving agent configuration metadata."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import List

from .schemas import AgentConfigCreate, AgentConfigRead, AgentConfigUpdate


class AgentConfigStore:
    """Persist agent configuration to a JSON file on disk.

    The store keeps data in a plain JSON array for easy inspection and manual
    editing. Access is guarded with a simple threading lock to prevent
    concurrent write corruption when multiple requests attempt to modify the
    file simultaneously.
    """

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            self.path.write_text("[]", encoding="utf-8")

    def _load_raw(self) -> list:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8") or "[]")
            if isinstance(data, list):
                return data
        except json.JSONDecodeError:
            pass
        return []

    def _save_raw(self, data: list) -> None:
        self.path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def list_configs(self) -> List[AgentConfigRead]:
        return [AgentConfigRead(**item) for item in self._load_raw()]

    def get_config(self, name: str) -> AgentConfigRead | None:
        for item in self._load_raw():
            if item.get("name") == name:
                return AgentConfigRead(**item)
        return None

    def upsert(self, config: AgentConfigCreate | AgentConfigUpdate) -> AgentConfigRead:
        """Create or update a config based on its name."""

        payload = config.model_dump(exclude_unset=True)
        name = payload.get("name")
        if not name:
            raise ValueError("agent config requires a name")

        with self.lock:
            data = self._load_raw()
            for idx, item in enumerate(data):
                if item.get("name") == name:
                    item.update(payload)
                    data[idx] = item
                    self._save_raw(data)
                    return AgentConfigRead(**item)

            data.append(payload)
            self._save_raw(data)
            return AgentConfigRead(**payload)

    def delete(self, name: str) -> None:
        with self.lock:
            data = self._load_raw()
            new_data = [item for item in data if item.get("name") != name]
            if len(new_data) == len(data):
                raise KeyError(f"config {name} not found")
            self._save_raw(new_data)

    def replace_all(self, configs: List[AgentConfigCreate]) -> List[AgentConfigRead]:
        payloads = [config.model_dump(exclude_none=True) for config in configs]
        with self.lock:
            self._save_raw(payloads)
        return [AgentConfigRead(**payload) for payload in payloads]

