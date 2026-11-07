"""Recuperação de gravações interrompidas (queda de energia, kill -9, travamento)."""
from __future__ import annotations

import time
from pathlib import Path

from . import merge, report, util
from .config import Settings
from .notifier import Notifier
from .session import Session, find_incomplete


def scan(out_dir: str | Path) -> list[Session]:
    return find_incomplete(out_dir)


def recover(session: Session, st: Settings, normalize: bool | None = None,
            notifier: Notifier | None = None) -> dict:
    """Reconstrói metadados a partir dos segmentos em disco, junta e gera o relatório.
    Não apaga nada: segmentos truncados continuam reproduzíveis (MPEG-TS)."""
    if session.lock_path.exists():
        try:
            pid = int(session.lock_path.read_text().strip() or 0)
        except ValueError:
            pid = 0
        if pid and util.pid_alive(pid):
            raise RuntimeError(f"A gravação ainda está em execução (PID {pid}); não é seguro recuperar.")
    util.setup_logging(session.logs_dir / "gravador.log", console=False)
    session.emit("recovery_start", message="Recuperando gravação interrompida")
    events = session.read_events()
    last_t = max((e["t"] for e in events), default=time.time())

    # fecha capturas que ficaram "abertas" e recalcula a duração a partir dos arquivos
    infos = [merge.probe_segment(p) for p in merge.list_segments(session.segments_dir)]
    by_cap: dict[int, list] = {}
    for i in infos:
        by_cap.setdefault(i.cid, []).append(i)
    known = {c["id"]: c for c in session.meta.get("captures", [])}
    for cid, segs in by_cap.items():
        c = known.get(cid) or {"id": cid, "source_id": segs[0].sid, "source": segs[0].sid, "staging": False}
        c["out_seconds"] = round(sum(s.duration for s in segs if s.ok), 2)
        c["segments"] = len(segs)
        c["bytes"] = sum(s.size for s in segs)
        if not c.get("ended"):
            c["end_reason"] = "encerramento inesperado (recuperada)"
        known[cid] = c
        session.upsert_capture(c)
    session.meta["status"] = "recovered"
    session.meta["ended"] = util.now_iso()
    session.meta["stop_reason"] = "encerramento inesperado — recuperada"
    session.save_meta()

    result = merge.finalize(session.root, session.meta.get("name", "gravacao"), st, normalize)
    session.meta["merge"] = result
    session.save_meta()
    session.emit("merge_result", message="; ".join(result.get("messages", [])), ok=result.get("ok"))
    session.emit("recovered_session", message="Gravação recuperada", last_event_t=last_t)
    report.write(session, result)
    if notifier:
        notifier.notify("recovery", "Gravação recuperada", f"{session.meta.get('name')}: {util.fmt_dur(result.get('total_seconds'))} salvos",
                        force=True)
    session.release_lock()
    return result
