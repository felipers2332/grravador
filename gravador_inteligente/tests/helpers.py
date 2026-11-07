"""Ferramentas compartilhadas pelos testes."""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from gravador.config import Settings, SourceCfg  # noqa: E402
from gravador.engine import Recorder  # noqa: E402
from gravador.notifier import Notifier  # noqa: E402
from gravador.session import Session  # noqa: E402
from simserver import SimServer, StreamCfg  # noqa: E402


def fast_settings(**kw) -> Settings:
    """Tempos curtos para os testes (os padrões reais são mais conservadores)."""
    st = Settings(segment_seconds=4, poll_interval=0.5, stall_data_seconds=3, stall_time_seconds=4,
                  suspect_grace_seconds=2, startup_timeout_s=10, validate_media_seconds=2.5, validate_timeout_s=15,
                  content_check_interval=5, freeze_seconds=8, black_seconds=8, cooldown_seconds=5,
                  all_down_retry_seconds=2, primary_probe_interval=4, primary_stable_seconds=6,
                  return_to_primary=False, notify_desktop=False, motion_check=True)
    st.update(kw)
    return st


def wait_until(cond, timeout: float, step: float = 0.3, msg: str = ""):
    t0 = time.time()
    while time.time() - t0 < timeout:
        v = cond()
        if v:
            return v
        time.sleep(step)
    raise AssertionError(f"timeout ({timeout}s) esperando: {msg}")


def make_recorder(tmp_path, urls: dict[str, str], st: Settings, name="Teste", **kw) -> Recorder:
    sess = Session.create(tmp_path, name)
    srcs = [SourceCfg(url=u, label=lbl, id=lbl.lower()[:6]) for lbl, u in urls.items()]
    return Recorder(sess, srcs, st, Notifier(st), **kw)


def events(rec: Recorder, type_: str | None = None) -> list[dict]:
    ev = rec.session.read_events()
    return [e for e in ev if type_ is None or e["type"] == type_]


def active_label(rec: Recorder):
    s = rec.active_src
    return s.cfg.label if s else None
