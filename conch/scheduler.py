"""Background task scheduler for Conch.

Runs scheduled prompts (with full MCP tool access) in a background thread.
Tasks persist to disk so they survive chat restarts. The background thread
runs inside the chat process — no separate daemon needed.

Storage: ~/.local/state/conch/tasks.json
"""
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

TASKS_DIR = Path(
    os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state"))
) / "conch"
TASKS_FILE = TASKS_DIR / "tasks.json"
LOG_DIR = TASKS_DIR / "task_logs"


def _parse_interval(spec: str) -> Optional[int]:
    """Parse interval like '10m', '1h', '30s', '2h30m' into seconds."""
    spec = spec.strip().lower()
    total = 0
    for match in re.finditer(r"(\d+)\s*(s|sec|m|min|h|hr|d|day)", spec):
        val = int(match.group(1))
        unit = match.group(2)[0]
        if unit == "s":
            total += val
        elif unit == "m":
            total += val * 60
        elif unit == "h":
            total += val * 3600
        elif unit == "d":
            total += val * 86400
    if total > 0:
        return total
    # Try bare number as minutes
    try:
        return int(spec) * 60
    except ValueError:
        return None


def _format_interval(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        m = seconds // 60
        s = seconds % 60
        return f"{m}m{s}s" if s else f"{m}m"
    h = seconds // 3600
    m = (seconds % 3600) // 60
    return f"{h}h{m}m" if m else f"{h}h"


class Task:
    def __init__(self, task_id: int, prompt: str, interval: int,
                 created_at: str = None, last_run: str = None,
                 run_count: int = 0, active: bool = True,
                 run_once: bool = False):
        self.id = task_id
        self.prompt = prompt
        self.interval = interval
        self.created_at = created_at or time.strftime("%Y-%m-%d %H:%M:%S")
        self.last_run = last_run
        self.run_count = run_count
        self.active = active
        self.run_once = run_once

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "prompt": self.prompt,
            "interval": self.interval,
            "created_at": self.created_at,
            "last_run": self.last_run,
            "run_count": self.run_count,
            "active": self.active,
            "run_once": self.run_once,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Task":
        return cls(**d)

    def is_due(self) -> bool:
        if not self.active:
            return False
        if self.run_once and self.run_count > 0:
            return False
        if not self.last_run:
            return True
        try:
            last = time.mktime(time.strptime(self.last_run, "%Y-%m-%d %H:%M:%S"))
            return (time.time() - last) >= self.interval
        except (ValueError, OverflowError):
            return True


class Scheduler:
    """Manages scheduled tasks with a background execution thread."""

    def __init__(self):
        self._tasks: List[Task] = []
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._executor: Optional[Callable] = None
        self._load()

    # -- persistence ----------------------------------------------------------

    def _load(self):
        if TASKS_FILE.is_file():
            try:
                with open(TASKS_FILE) as f:
                    data = json.load(f)
                self._tasks = [Task.from_dict(d) for d in data]
            except (json.JSONDecodeError, OSError, TypeError):
                self._tasks = []

    def _save(self):
        TASKS_DIR.mkdir(parents=True, exist_ok=True)
        tmp = TASKS_FILE.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump([t.to_dict() for t in self._tasks], f, indent=2)
        tmp.replace(TASKS_FILE)

    # -- task management ------------------------------------------------------

    def add(self, prompt: str, interval: int, run_once: bool = False) -> Task:
        with self._lock:
            next_id = max((t.id for t in self._tasks), default=0) + 1
            task = Task(task_id=next_id, prompt=prompt, interval=interval,
                        run_once=run_once)
            self._tasks.append(task)
            self._save()
            return task

    def cancel(self, task_id: int) -> bool:
        with self._lock:
            for t in self._tasks:
                if t.id == task_id:
                    t.active = False
                    self._save()
                    return True
        return False

    def remove(self, task_id: int) -> bool:
        with self._lock:
            before = len(self._tasks)
            self._tasks = [t for t in self._tasks if t.id != task_id]
            if len(self._tasks) < before:
                self._save()
                return True
        return False

    def list_tasks(self) -> List[Task]:
        with self._lock:
            return list(self._tasks)

    # -- execution ------------------------------------------------------------

    def set_executor(self, fn: Callable[[str], str]):
        """Set the function that executes a prompt (calls _chat_turn)."""
        self._executor = fn

    def _run_task(self, task: Task):
        if not self._executor:
            return
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_file = LOG_DIR / f"task_{task.id}.log"

        try:
            result = self._executor(task.prompt)
            with self._lock:
                task.last_run = time.strftime("%Y-%m-%d %H:%M:%S")
                task.run_count += 1
                if task.run_once:
                    task.active = False
                self._save()

            with open(log_file, "a") as f:
                f.write(f"\n--- {task.last_run} ---\n")
                f.write(f"Prompt: {task.prompt}\n")
                f.write(f"Result: {result[:2000]}\n")

        except Exception as e:
            with open(log_file, "a") as f:
                f.write(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} ERROR ---\n")
                f.write(f"Prompt: {task.prompt}\n")
                f.write(f"Error: {e}\n")

    def _loop(self):
        """Background loop — checks for due tasks every 10 seconds."""
        while not self._stop_event.is_set():
            with self._lock:
                due = [t for t in self._tasks if t.is_due()]
            for task in due:
                self._run_task(task)
            self._stop_event.wait(10)

    def start(self):
        """Start the background scheduler thread."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        """Stop the background scheduler."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5)
