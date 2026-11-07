"""Pasta da gravação: eventos (append-only), metadados atômicos, trava com PID, detecção de sessões incompletas."""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from . import util


class SessionLockedError(RuntimeError):
    pass


class Session:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.segments_dir = self.root / "segments"
        self.probe_dir = self.root / "probe"
        self.logs_dir = self.root / "logs"
        self.events_path = self.root / "events.jsonl"
        self.meta_path = self.root / "session.json"
        self.lock_path = self.root / "session.lock"
        self.meta: dict = {}
        self._ev_lock = threading.Lock()
        self._meta_lock = threading.Lock()

    # ---------- criação / abertura ----------
    @classmethod
    def create(cls, base_dir: str | Path, name: str, meta_extra: dict | None = None) -> "Session":
        base = Path(base_dir)
        base.mkdir(parents=True, exist_ok=True)
        folder = util.slug(name)
        root = base / folder
        n = 2
        while root.exists() and any(root.iterdir()):
            root = base / f"{folder}_{n}"
            n += 1
        s = cls(root)
        s._mkdirs()
        s.meta = {"name": name, "status": "recording", "started": util.now_iso(), "ended": None,
                  "pid": os.getpid(), "captures": [], "next_capture_id": 1, "version": 1}
        s.meta.update(meta_extra or {})
        s.save_meta()
        return s

    @classmethod
    def open(cls, root: str | Path) -> "Session":
        s = cls(Path(root))
        if not s.meta_path.exists():
            raise FileNotFoundError(f"{root} não é uma pasta de gravação (falta session.json)")
        s.meta = json.loads(s.meta_path.read_text(encoding="utf-8"))
        s._mkdirs()
        return s

    def _mkdirs(self):
        for d in (self.root, self.segments_dir, self.logs_dir):
            d.mkdir(parents=True, exist_ok=True)

    # ---------- trava ----------
    def acquire_lock(self) -> None:
        if self.lock_path.exists():
            try:
                pid = int(self.lock_path.read_text().strip() or 0)
            except ValueError:
                pid = 0
            if pid and pid != os.getpid() and util.pid_alive(pid):
                raise SessionLockedError(f"Gravação já está em execução (PID {pid}).")
        self.lock_path.write_text(str(os.getpid()))
        self.meta["pid"] = os.getpid()

    def release_lock(self) -> None:
        try:
            self.lock_path.unlink()
        except FileNotFoundError:
            pass

    def is_incomplete(self) -> bool:
        """Sessão que ficou como 'recording' mas cujo processo já não existe."""
        if self.meta.get("status") != "recording":
            return False
        pid = self.meta.get("pid")
        if self.lock_path.exists():
            try:
                pid = int(self.lock_path.read_text().strip() or pid or 0)
            except ValueError:
                pass
        return not (pid and util.pid_alive(int(pid)))

    # ---------- metadados ----------
    def save_meta(self) -> None:
        with self._meta_lock:
            util.atomic_write_json(self.meta_path, self.meta)

    def next_capture_id(self) -> int:
        with self._meta_lock:
            cid = int(self.meta.get("next_capture_id", 1))
            self.meta["next_capture_id"] = cid + 1
            return cid

    def upsert_capture(self, summary: dict) -> None:
        with self._meta_lock:
            caps = self.meta.setdefault("captures", [])
            for i, c in enumerate(caps):
                if c.get("id") == summary["id"]:
                    caps[i] = summary
                    break
            else:
                caps.append(summary)

    # ---------- eventos ----------
    def emit(self, type_: str, **kw) -> dict:
        ev = {"ts": util.now_iso(), "t": round(time.time(), 3), "type": type_}
        ev.update(kw)
        line = json.dumps(ev, ensure_ascii=False, default=str)
        with self._ev_lock:
            with open(self.events_path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
                os.fsync(f.fileno())
        return ev

    def read_events(self) -> list[dict]:
        out = []
        if not self.events_path.exists():
            return out
        with open(self.events_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass  # linha final truncada por queda
        return out


def find_incomplete(base_dir: str | Path) -> list[Session]:
    base = Path(base_dir)
    found = []
    if not base.exists():
        return found
    for child in sorted(base.iterdir()):
        if (child / "session.json").exists():
            try:
                s = Session.open(child)
                if s.is_incomplete():
                    found.append(s)
            except Exception:
                continue
    return found
