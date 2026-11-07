"""Validação de fontes: uma fonte só é 'funcional' se o vídeo realmente avança e há dados chegando."""
from __future__ import annotations

import shutil
import tempfile
import threading
import time
from pathlib import Path

from . import sources, util
from .capture import Capture
from .config import Settings, SourceCfg
from .monitor import analyze_frames, recent_frames


def check_motion(cap: Capture, st: Settings) -> tuple[bool, str]:
    """True se a imagem não está congelada nem preta (indeterminado conta como OK)."""
    if not cap.newest_segment():
        return True, ""
    res = analyze_frames(recent_frames(cap, max(6, st.validate_media_seconds + 4)))
    if res["black"]:
        return False, "tela preta"
    if res["motion"] is False:
        return False, "imagem estática (sem movimento)"
    return True, ""


def validate_capture(cap: Capture, st: Settings, stop_evt: threading.Event | None = None,
                     require_motion: bool = False) -> tuple[bool, str]:
    """Aguarda a captura provar que funciona: processo vivo, vídeo (e áudio), mídia avançando, bytes crescendo."""
    deadline = time.time() + st.validate_timeout_s
    need_t = st.validate_media_seconds + (4 if require_motion else 0)
    while time.time() < deadline:
        if stop_evt is not None and stop_evt.is_set():
            return False, "cancelado"
        cap.poll()
        if not cap.alive:
            cap.reap("falhou na validação")
            return False, cap.failure_reason()
        has_v = "video" in cap.info
        has_a = "audio" in cap.info
        if cap.out_time >= need_t and cap.bytes > 0 and has_v and (has_a or not st.require_audio):
            if time.time() - cap.last_bytes_change > 3:
                time.sleep(0.5)
                continue
            if require_motion:
                ok, why = check_motion(cap, st)
                if not ok:
                    return False, why
            return True, "ok"
        time.sleep(0.4)
    cap.poll()
    if cap.bytes == 0:
        return False, util.friendly_error(list(cap.stderr)) or "nenhum dado recebido"
    if "video" not in cap.info:
        return False, "sem trilha de vídeo"
    if st.require_audio and "audio" not in cap.info:
        return False, "sem trilha de áudio (desative 'require_audio' se for esperado)"
    return False, f"mídia insuficiente após {st.validate_timeout_s:.0f}s ({cap.out_time:.1f}s)"


def test_source(source: SourceCfg, st: Settings, profile_dir: Path | None = None,
                stop_evt: threading.Event | None = None) -> dict:
    """Teste manual de um link (usado pelo botão 'Testar' e pelo comando `test`). Não deixa arquivos."""
    t0 = time.time()
    out = {"ok": False, "reason": "", "info": {}, "note": "", "seconds": 0.0, "bitrate_kbps": None}
    try:
        resolved = sources.resolve(source, st.quality, profile_dir)
    except sources.ProtectedSourceError as e:
        out["reason"] = f"Protegida por DRM — {e}"
        out["protected"] = True
        return out
    except sources.SourceError as e:
        out["reason"] = str(e)
        return out
    except Exception as e:
        out["reason"] = f"erro ao resolver a fonte: {e}"
        return out
    tmp = Path(tempfile.mkdtemp(prefix="gravador_test_"))
    cap = Capture(0, source, resolved, tmp, st, staging=True)
    try:
        cap.start()
        ok, why = validate_capture(cap, st, stop_evt, require_motion=st.motion_check)
        out.update(ok=ok, reason="" if ok else why, info=cap.info, note=resolved.note)
        if cap.out_time > 0:
            out["bitrate_kbps"] = round(cap.bytes * 8 / cap.out_time / 1000)
    except FileNotFoundError as e:
        out["reason"] = str(e)
    finally:
        cap.stop("teste concluído", timeout=4)
        shutil.rmtree(tmp, ignore_errors=True)
    out["seconds"] = round(time.time() - t0, 1)
    return out
