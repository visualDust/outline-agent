from __future__ import annotations

import json
import threading
from pathlib import Path


class ProcessedEventStore:
    def __init__(self, path: Path, max_entries: int = 10000):
        self.path = path
        self.max_entries = max_entries
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def contains(self, key: str) -> bool:
        data = self._load()
        return key in data.get("keys", [])

    def add(self, key: str) -> None:
        with self._lock:
            data = self._load()
            keys = list(data.get("keys", []))
            if key in keys:
                return
            keys.append(key)
            if len(keys) > self.max_entries:
                keys = keys[-self.max_entries :]
            self.path.write_text(json.dumps({"keys": keys}, ensure_ascii=False, indent=2), encoding="utf-8")

    def _load(self) -> dict[str, list[str]]:
        if not self.path.exists():
            return {"keys": []}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {"keys": []}
        if not isinstance(payload, dict):
            return {"keys": []}
        keys = payload.get("keys")
        if not isinstance(keys, list):
            return {"keys": []}
        return {"keys": [key for key in keys if isinstance(key, str)]}
