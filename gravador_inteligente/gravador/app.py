"""AppController: estado da aplicação (configuração + gravação atual) usado pela CLI e pela interface web."""
from __future__ import annotations

import os
import threading
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from . import probe, recovery, sources, util
from .config import AppConfig, SourceCfg
from .engine import Recorder
from .notifier import Notifier
from .session import Session, SessionLockedError, find_incomplete


def default_config_path() -> Path:
    return Path(os.environ.get("GRAVADOR_HOME", Path.home() / ".gravador")) / "config.json"


class AppController:
    def __init__(self, config_path: Path | None = None):
        self.config_path = Path(config_path) if config_path else default_config_path()
        self.cfg = AppConfig.load(self.config_path)
        self.recorder: Recorder | None = None
        self.tests: dict[str, dict] = {}
        self._lock = threading.RLock()

    # ------------------------------------------------------------ configuração
    def save(self):
        self.cfg.save(self.config_path)

    def set_basic(self, name: str | None = None, out_dir: str | None = None, settings: dict | None = None):
        if name is not None:
            self.cfg.name = name
        if out_dir:
            self.cfg.out_dir = out_dir
        if settings:
            self.cfg.settings.update(settings)
        self.save()

    def _live(self) -> Recorder | None:
        r = self.recorder
        return r if r and r.running else None

    def add_source(self, url: str, label: str = "") -> SourceCfg:
        url = url.strip()
        if not url:
            raise ValueError("URL vazia")
        cfg = SourceCfg(url=url, label=label.strip())
        with self._lock:
            self.cfg.sources.append(cfg)
            self.save()
            if self._live():
                self.recorder.add_source(SourceCfg(**asdict(cfg)))
        return cfg

    def remove_source(self, sid: str):
        with self._lock:
            self.cfg.sources = [s for s in self.cfg.sources if s.id != sid]
            self.save()
            if self._live():
                self.recorder.remove_source(sid)

    def move_source(self, sid: str, delta: int):
        with self._lock:
            ids = [s.id for s in self.cfg.sources]
            if sid not in ids:
                return
            i = ids.index(sid)
            j = max(0, min(len(ids) - 1, i + delta))
            self.cfg.sources.insert(j, self.cfg.sources.pop(i))
            self.save()
            if self._live():
                self.recorder.set_order([s.id for s in self.cfg.sources])

    def toggle_source(self, sid: str, enabled: bool):
        with self._lock:
            for s in self.cfg.sources:
                if s.id == sid:
                    s.enabled = enabled
            self.save()
            if self._live():
                self.recorder.set_enabled(sid, enabled)

    # ------------------------------------------------------------ teste manual
    def test_source(self, sid: str) -> None:
        src = next((s for s in self.cfg.sources if s.id == sid), None)
        if not src:
            return
        self.tests[sid] = {"running": True}

        def work():
            try:
                res = probe.test_source(src, self.cfg.settings)
            except Exception as e:
                res = {"ok": False, "reason": f"erro no teste: {e}"}
            res["running"] = False
            res["at"] = time.time()
            self.tests[sid] = res
        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------ gravação
    def start_recording(self, schedule_at: str | None = None, max_duration: float | None = None,
                        resume_dir: str | None = None) -> Recorder:
        with self._lock:
            if self._live():
                raise RuntimeError("Já existe uma gravação em andamento.")
            enabled = [s for s in self.cfg.sources if s.enabled]
            if not enabled:
                raise ValueError("Cadastre pelo menos uma fonte habilitada.")
            when = None
            if schedule_at:
                when = datetime.fromisoformat(schedule_at.replace("Z", "")).timestamp() if schedule_at else None
            st = self.cfg.settings
            if resume_dir:
                sess = Session.open(resume_dir)
            else:
                name = self.cfg.name
                sess = Session.create(self.cfg.out_dir, name)
            srcs = [SourceCfg(**asdict(s)) for s in self.cfg.sources]
            sess.meta["sources"] = [asdict(s) for s in srcs]
            sess.meta["settings"] = asdict(st)
            rec = Recorder(sess, srcs, st, Notifier(st), scheduled_at=when, max_duration=max_duration)
            rec.start()
            self.recorder = rec
            return rec

    def stop_recording(self, wait: bool = False):
        r = self.recorder
        if r and r.running:
            r.stop(wait=wait)

    # ------------------------------------------------------------ estado / recuperação
    def incomplete(self) -> list[dict]:
        out = []
        for s in find_incomplete(self.cfg.out_dir):
            out.append({"path": str(s.root), "name": s.meta.get("name"), "started": s.meta.get("started")})
        return out

    def recover(self, path: str, normalize: bool | None = None) -> dict:
        return recovery.recover(Session.open(path), self.cfg.settings, normalize, Notifier(self.cfg.settings))

    def state(self) -> dict:
        r = self.recorder
        snap = r.snapshot() if r else None
        srcs = []
        for s in self.cfg.sources:
            d = asdict(s)
            d["test"] = self.tests.get(s.id)
            srcs.append(d)
        return {"config": {"name": self.cfg.name, "out_dir": self.cfg.out_dir, "sources": srcs,
                           "settings": asdict(self.cfg.settings)},
                "recording": snap, "incomplete": self.incomplete() if not (r and r.running) else []}
