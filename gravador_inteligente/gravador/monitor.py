"""Monitoramento de saúde: combina processo, bytes em disco, tempo de mídia, velocidade,
taxa de dados e análise de conteúdo (congelado / tela preta).
"""
from __future__ import annotations

import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field

from . import util
from .capture import Capture
from .config import Settings

FW, FH = 32, 18
FRAME_BYTES = FW * FH
MOTION_THRESHOLD = 0.6   # diferença média (0-255) entre quadros-chave para considerar "movimento"
BLACK_MEAN = 14.0
BLACK_MAX = 48


def grab_keyframes(path, window_s: float, timeout: float = 40) -> list[bytes]:
    """Decodifica SÓ os quadros-chave dos últimos `window_s` s do arquivo, em 32x18 cinza (barato)."""
    cmd = [util.find_bin("ffmpeg"), "-hide_banner", "-loglevel", "error", "-nostdin",
           "-skip_frame", "nokey", "-sseof", f"-{window_s:.0f}", "-i", str(path), "-map", "0:v:0", "-an",
           "-vf", f"scale={FW}:{FH},format=gray", "-vsync", "0", "-f", "rawvideo", "-"]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL).stdout
    except Exception:
        return []
    return [out[i:i + FRAME_BYTES] for i in range(0, len(out) - FRAME_BYTES + 1, FRAME_BYTES)]


def analyze_frames(frames: list[bytes]) -> dict:
    """Retorna {'n', 'motion': bool|None, 'black': bool|None}. None = indeterminado (poucos quadros)."""
    n = len(frames)
    res = {"n": n, "motion": None, "black": None}
    if n >= 1:
        means = [sum(f) / len(f) for f in frames]
        res["black"] = (n >= 2) and all(m < BLACK_MEAN and max(f) < BLACK_MAX for m, f in zip(means, frames))
    if n >= 2:
        best = 0.0
        for a, b in zip(frames, frames[1:]):
            d = sum(abs(x - y) for x, y in zip(a, b)) / FRAME_BYTES
            best = max(best, d)
        res["motion"] = best >= MOTION_THRESHOLD
        res["max_diff"] = round(best, 2)
    return res


def recent_frames(cap: Capture, window_s: float) -> list[bytes]:
    """Quadros-chave recentes: usa os 2 últimos segmentos (o mais novo pode ter acabado de abrir)."""
    files = [f for f in cap.segment_files() if f.exists() and f.stat().st_size > 0][-2:]
    frames: list[bytes] = []
    for f in files:
        frames += grab_keyframes(f, window_s)
    return frames


class ContentWatcher(threading.Thread):
    """Thread leve por captura: a cada N s analisa o final do segmento mais recente."""

    def __init__(self, cap: Capture, st: Settings):
        super().__init__(daemon=True)
        self.cap, self.st = cap, st
        self._halt = threading.Event()
        now = time.time()
        cap.content = {"last_motion": now, "last_nonblack": now, "checked_at": None, "analysis": None}

    def stop(self):
        self._halt.set()

    def run(self):
        interval = max(5.0, self.st.content_check_interval)
        window = interval + 3
        while not self._halt.wait(interval):
            if not self.cap.alive:
                return
            if not self.cap.newest_segment():
                continue
            res = analyze_frames(recent_frames(self.cap, window))
            now = time.time()
            c = self.cap.content
            # indeterminado conta como "ok" (nunca gera falso alarme por falta de quadros)
            if res["motion"] is None or res["motion"]:
                c["last_motion"] = now
            if res["black"] is None or not res["black"]:
                c["last_nonblack"] = now
            c["checked_at"] = now
            c["analysis"] = res


@dataclass
class Verdict:
    level: str = "ok"                 # ok | warn | bad
    reasons: list = field(default_factory=list)
    kind: str = ""                    # hard | content | soft | starting
    dead: bool = False


class HealthMonitor:
    """Avalia uma captura. Mantém histórico de taxa de dados e temporizadores de buffering/queda."""

    def __init__(self, st: Settings):
        self.st = st
        self.hist: deque = deque()
        self.baseline = 0.0
        self.low_speed_since: float | None = None
        self.low_rate_since: float | None = None
        self.last_check = time.time()

    def _rate(self, now: float, bytes_: int) -> float | None:
        self.hist.append((now, bytes_))
        while self.hist and now - self.hist[0][0] > 12:
            self.hist.popleft()
        if len(self.hist) >= 2 and now - self.hist[0][0] >= 5:
            t0, b0 = self.hist[0]
            return (bytes_ - b0) / (now - t0)
        return None

    def evaluate(self, cap: Capture, now: float | None = None) -> Verdict:
        st = self.st
        now = now or time.time()
        self.last_check = now
        v = Verdict()
        cap.poll()

        if not cap.alive:
            cap.reap()
            v.level, v.kind, v.dead = "bad", "hard", True
            v.reasons.append(cap.failure_reason())
            return v

        age = now - cap.started
        no_data_yet = cap.first_data is None
        if no_data_yet:
            if age > st.startup_timeout_s:
                v.level, v.kind = "bad", "hard"
                v.reasons.append(f"nenhum dado recebido em {age:.0f}s após iniciar")
            else:
                v.level, v.kind = "warn", "starting"
                v.reasons.append("iniciando…")
            return v

        stall = now - cap.last_bytes_change
        if stall > st.stall_data_seconds:
            v.level, v.kind = "bad", "hard"
            v.reasons.append(f"sem novos dados de vídeo há {stall:.0f}s")
        elif stall > st.stall_data_seconds / 2:
            v.level = "warn"
            v.reasons.append(f"dados atrasados ({stall:.0f}s)")

        tstall = now - cap.last_time_advance
        if tstall > st.stall_time_seconds and v.level != "bad":
            v.level, v.kind = "bad", "hard"
            v.reasons.append(f"mídia não avança há {tstall:.0f}s")

        # buffering (velocidade abaixo do tempo real) — só vira vermelho se prolongado
        if cap.speed is not None and cap.speed < st.buffering_speed and age > 10:
            self.low_speed_since = self.low_speed_since or now
            dur = now - self.low_speed_since
            if dur > st.buffering_bad_seconds:
                v.level, v.kind = "bad", v.kind or "soft"
                v.reasons.append(f"buffering prolongado ({dur:.0f}s a {cap.speed:.2f}x)")
            elif dur > st.buffering_warn_seconds and v.level == "ok":
                v.level = "warn"
                v.reasons.append(f"buffering ({cap.speed:.2f}x)")
        else:
            self.low_speed_since = None

        # taxa de dados vs. linha de base
        rate = self._rate(now, cap.bytes)
        if rate is not None:
            if self.baseline == 0:
                self.baseline = rate
            if self.baseline > 20_000 and rate < self.baseline * st.bitrate_drop_ratio:
                self.low_rate_since = self.low_rate_since or now
                dur = now - self.low_rate_since
                if dur > st.bitrate_drop_seconds:
                    v.level, v.kind = "bad", v.kind or "soft"
                    v.reasons.append(f"queda forte na taxa de dados ({rate / 1000:.0f} kB/s vs. {self.baseline / 1000:.0f})")
                elif v.level == "ok":
                    v.level = "warn"
                    v.reasons.append("taxa de dados caiu")
            else:
                self.low_rate_since = None
                if v.level == "ok":
                    self.baseline = 0.9 * self.baseline + 0.1 * rate

        # conteúdo
        c = cap.content
        if c and st.freeze_detect:
            frozen = now - c.get("last_motion", now)
            black = now - c.get("last_nonblack", now)
            if black > st.black_seconds:
                v.level, v.kind = "bad", v.kind or "content"
                v.reasons.append(f"tela preta há {black:.0f}s")
            elif frozen > st.freeze_seconds:
                v.level, v.kind = "bad", v.kind or "content"
                v.reasons.append(f"vídeo congelado há {frozen:.0f}s")
        return v
