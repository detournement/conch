"""Background task scheduler."""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, asdict, fields
from datetime import datetime
from pathlib import Path
from typing import Callable, List, Optional


def _state_dir() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "conch"


def _tasks_path() -> Path:
    return _state_dir() / "tasks.json"


def _parse_interval(value: str) -> int:
    value = value.strip().lower()
    aliases = {
        "hourly": 3600,
        "daily": 86400,
        "weekly": 604800,
    }
    if value in aliases:
        return aliases[value]
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    total = 0
    number = ""
    for ch in value:
        if ch.isdigit():
            number += ch
            continue
        if ch in units and number:
            total += int(number) * units[ch]
            number = ""
        else:
            return 0
    return total


def _format_interval(seconds: int) -> str:
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


@dataclass
class Task:
    id: int
    prompt: str
    interval: int
    run_once: bool = False
    active: bool = True
    run_count: int = 0
    last_run: str = ""
    next_run_at: float = 0.0
    last_error: str = ""

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "Task":
        valid_fields = {field.name for field in fields(cls)}
        filtered = {key: value for key, value in data.items() if key in valid_fields}
        return cls(**filtered)


class Scheduler:
    def __init__(self):
        self._executor: Optional[Callable[[str, Task], str]] = None
        self._tasks: List[Task] = self._load()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _load(self) -> List[Task]:
        try:
            data = json.loads(_tasks_path().read_text())
            return [Task.from_dict(item) for item in data]
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return []

    def _save(self):
        _tasks_path().parent.mkdir(parents=True, exist_ok=True)
        tmp = _tasks_path().with_suffix(".tmp")
        tmp.write_text(json.dumps([task.to_dict() for task in self._tasks], indent=2))
        tmp.replace(_tasks_path())

    def set_executor(self, executor: Callable[[str, Task], str]):
        self._executor = executor

    def add(self, prompt: str, interval: int, run_once: bool = False) -> Task:
        with self._lock:
            task = Task(
                id=max((task.id for task in self._tasks), default=0) + 1,
                prompt=prompt,
                interval=interval,
                run_once=run_once,
                next_run_at=time.time() + interval,
            )
            self._tasks.append(task)
            self._save()
            return task

    def cancel(self, task_id: int) -> bool:
        with self._lock:
            for task in self._tasks:
                if task.id == task_id:
                    task.active = False
                    self._save()
                    return True
        return False

    def list_tasks(self) -> List[Task]:
        with self._lock:
            return [Task.from_dict(task.to_dict()) for task in self._tasks]

    def _run_due_tasks(self):
        now = time.time()
        due: List[Task] = []
        with self._lock:
            for task in self._tasks:
                if task.active and task.next_run_at <= now:
                    due.append(task)
        for task in due:
            if not self._executor:
                continue
            try:
                self._executor(task.prompt, task)
                task.run_count += 1
                task.last_run = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                task.last_error = ""
                if task.run_once:
                    task.active = False
                else:
                    task.next_run_at = time.time() + task.interval
            except Exception as exc:
                task.last_error = str(exc)
                task.next_run_at = time.time() + max(task.interval, 60)
            with self._lock:
                self._save()

    def _loop(self):
        while not self._stop.is_set():
            self._run_due_tasks()
            self._stop.wait(1.0)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.0)

