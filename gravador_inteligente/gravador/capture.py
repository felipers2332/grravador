"""Uma captura = um processo FFmpeg gravando em segmentos MPEG-TS (cópia sem recodificar)."""
from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

from . import util
from .config import Settings, SourceCfg
from .sources import Resolved

SEG_RE = re.compile(r"^c(\d{3,})_([A-Za-z0-9]+)_(\d{5,})\.ts$")


class Capture:
    def __init__(self, cid: int, source: SourceCfg, resolved: Resolved, out_dir: Path,
                 settings: Settings, staging: bool = False):
        self.cid = cid
        self.source = source
        self.resolved = resolved
        self.out_dir = Path(out_dir)
        self.st = settings
        self.staging = staging
        self.prefix = f"c{cid:03d}_{source.id}_"
        self.proc: subprocess.Popen | None = None
        self.stderr = deque(maxlen=300)
        self.info: dict = {}
        self.started = 0.0
        self.ended: float | None = None
        self.exit_code: int | None = None
        self.end_reason = ""
        # estado de progresso
        self.out_time = 0.0            # segundos de mídia gravados (progress do FFmpeg)
        self.speed: float | None = None
        self.bytes = 0
        self.first_data: float | None = None
        self.last_bytes_change = 0.0   # wall clock da última vez que o disco recebeu dados
        self.last_time_advance = 0.0
        self.content: dict = {}        # preenchido pelo ContentWatcher
        self._last_out_time = 0.0
        self._out_seen = False
        self._lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._stop_requested = False

    # ------------------------------------------------------------ comando
    def build_cmd(self) -> list[str]:
        st, r = self.st, self.resolved
        cmd = [util.find_bin("ffmpeg"), "-hide_banner", "-loglevel", "info", "-nostats",
               "-progress", "pipe:1", "-stats_period", "0.5", "-y"]
        url = r.url
        if url.lower().startswith(("http://", "https://")):
            cmd += ["-rw_timeout", str(int(st.ffmpeg_rw_timeout_s * 1_000_000))]
            h = {k.lower(): v for k, v in r.headers.items()}
            if h.get("user-agent"):
                cmd += ["-user_agent", h["user-agent"]]
            if h.get("referer"):
                cmd += ["-referer", h["referer"]]
        cmd += ["-fflags", "+genpts", "-analyzeduration", str(int(st.analyze_seconds * 1_000_000)), "-i", url]
        cmd += r.map_args + ["-c", "copy", "-dn", "-sn"]
        cmd += ["-f", "segment", "-segment_time", str(st.segment_seconds), "-segment_format", "mpegts",
                "-segment_format_options", "flush_packets=1", "-reset_timestamps", "1",
                "-segment_start_number", "0", str(self.out_dir / (self.prefix + "%05d.ts"))]
        return cmd

    # ------------------------------------------------------------ ciclo de vida
    def start(self) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        cmd = self.build_cmd()
        util.log.info("[c%03d] iniciando FFmpeg para %s", self.cid, self.source.display())
        kw = {}
        if os.name == "nt":
            kw["creationflags"] = 0x00000200  # CREATE_NEW_PROCESS_GROUP
        self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     preexec_fn=util.child_preexec(), **kw)
        self.started = time.time()
        self.last_bytes_change = self.last_time_advance = self.started
        for target in (self._read_progress, self._read_stderr):
            t = threading.Thread(target=target, daemon=True)
            t.start()
            self._threads.append(t)

    @property
    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def _read_progress(self):
        try:
            for raw in self.proc.stdout:
                line = raw.decode("utf-8", "replace").strip()
                if "=" not in line:
                    continue
                k, v = line.split("=", 1)
                with self._lock:
                    if k in ("out_time_us", "out_time_ms"):
                        try:
                            val = int(v) / 1_000_000
                            if val >= 0:
                                self.out_time = val
                        except ValueError:
                            pass
                    elif k == "speed":
                        try:
                            self.speed = float(v.strip().rstrip("x"))
                        except ValueError:
                            self.speed = None
                    elif k == "progress":
                        if self.out_time > self._last_out_time + 0.05:
                            self._last_out_time = self.out_time
                            self.last_time_advance = time.time()
        except Exception:
            pass

    def _read_stderr(self):
        try:
            for raw in self.proc.stderr:
                line = raw.decode("utf-8", "replace").rstrip()
                if not line:
                    continue
                self.stderr.append(line)
                if not self._out_seen:
                    if line.startswith("Output #0"):
                        self._out_seen = True
                    else:
                        self._parse_stream(line)
        except Exception:
            pass

    def _parse_stream(self, line: str):
        if ": Video:" in line and "video" not in self.info:
            m = re.search(r"Video: (\w+)(?: \(([^)]*)\))?", line)
            size = re.search(r"(\d{2,5})x(\d{2,5})", line.split("Video:", 1)[1])
            fps = re.search(r"([\d.]+) fps", line)
            self.info["video"] = {"codec": m.group(1) if m else "?", "profile": (m.group(2) or "") if m else "",
                                  "width": int(size.group(1)) if size else 0,
                                  "height": int(size.group(2)) if size else 0,
                                  "fps": float(fps.group(1)) if fps else 0.0}
        elif ": Audio:" in line and "audio" not in self.info:
            m = re.search(r"Audio: (\w+)", line)
            hz = re.search(r"(\d+) Hz", line)
            self.info["audio"] = {"codec": m.group(1) if m else "?", "rate": int(hz.group(1)) if hz else 0}

    # ------------------------------------------------------------ polling (disco)
    def segment_files(self) -> list[Path]:
        out = []
        try:
            with os.scandir(self.out_dir) as it:
                for e in it:
                    if e.name.startswith(self.prefix) and e.name.endswith(".ts"):
                        out.append(Path(e.path))
        except FileNotFoundError:
            pass
        return sorted(out)

    def poll(self) -> None:
        """Atualiza bytes gravados em disco (sinal mais confiável de 'dados chegando')."""
        total = 0
        try:
            with os.scandir(self.out_dir) as it:
                for e in it:
                    if e.name.startswith(self.prefix):
                        try:
                            total += e.stat().st_size
                        except OSError:
                            pass
        except FileNotFoundError:
            pass
        now = time.time()
        if total > self.bytes:
            self.last_bytes_change = now
            if self.first_data is None:
                self.first_data = now
                # os relógios de "mídia avançando" só passam a valer a partir do primeiro dado
                self.last_time_advance = now
        self.bytes = total

    def failure_reason(self) -> str:
        code = self.exit_code if self.exit_code is not None else (self.proc.poll() if self.proc else None)
        base = f"FFmpeg encerrou (código {code})"
        friendly = util.friendly_error(list(self.stderr))
        if friendly:
            return f"{base}: {friendly}"
        errs = [l for l in self.stderr if re.search(r"error|fail|invalid|refused|timed out|not found|returned", l, re.I)]
        if errs:
            return f"{base}: {errs[-1][:140]}"
        if code == 0:
            return "o servidor encerrou a transmissão (fim do fluxo)"
        return base

    def newest_segment(self) -> Path | None:
        files = self.segment_files()
        for f in reversed(files):
            try:
                if f.stat().st_size > 0:
                    return f
            except OSError:
                continue
        return None

    # ------------------------------------------------------------ parada
    def stop(self, reason: str = "parado", timeout: float = 10.0) -> None:
        if self.proc is None:
            return
        self._stop_requested = True
        if self.proc.poll() is None:
            try:
                self.proc.stdin.write(b"q\n")
                self.proc.stdin.flush()
            except Exception:
                pass
            try:
                self.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=4)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait()
        self._finish(reason)

    def _finish(self, reason: str):
        if self.ended is None:
            self.ended = time.time()
            self.exit_code = self.proc.poll() if self.proc else None
            self.end_reason = reason
            for t in self._threads:
                t.join(timeout=2)
            self.poll()
            # remove apenas arquivos vazios (nunca segmentos com dados)
            for f in self.segment_files():
                try:
                    if f.stat().st_size == 0:
                        f.unlink()
                except OSError:
                    pass

    def reap(self, reason: str = "encerrou sozinho") -> None:
        """Chamar quando o processo já morreu por conta própria."""
        if self.proc is not None and self.proc.poll() is not None:
            self._finish(reason)

    def summary(self) -> dict:
        files = self.segment_files()
        last_change = self.last_bytes_change if self.bytes else None
        iso = lambda t: datetime.fromtimestamp(t).astimezone().isoformat(timespec="seconds") if t else None
        return {"id": self.cid, "source_id": self.source.id, "source": self.source.display(),
                "staging": self.staging, "started": iso(self.started), "first_data": iso(self.first_data),
                "last_data": iso(last_change), "first_data_t": self.first_data, "last_data_t": last_change,
                "ended": iso(self.ended), "out_seconds": round(self.out_time, 2), "bytes": self.bytes,
                "segments": len(files), "exit_code": self.exit_code, "end_reason": self.end_reason,
                "info": self.info, "note": self.resolved.note}
