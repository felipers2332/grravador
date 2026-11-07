"""Transmissões simuladas (reais para o FFmpeg) para testar o gravador sem depender de internet.

SimServer: MPEG-TS ao vivo via HTTP, em tempo real, com modos controláveis a qualquer momento:
  ok      imagem em movimento + áudio
  down    HTTP 503 e conexões abertas são fechadas (transmissão caiu)
  stall   conexão aberta, mas nenhum dado é enviado (travou)
  freeze  dados continuam chegando, mas o quadro fica parado (vídeo congelado)
  black   dados continuam chegando, tela preta
HLSServer: HLS ao vivo gerado pelo FFmpeg e servido por HTTP.
"""
from __future__ import annotations

import http.server
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass
class StreamCfg:
    size: tuple = (320, 240)
    fps: int = 15
    vcodec: str = "libx264"
    acodec: str = "aac"
    mode: str = "ok"
    start_mode_delay: float = 0.0


class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *a):
        pass

    def do_GET(self):
        srv: SimServer = self.server.sim
        name = self.path.strip("/").split(".")[0].split("?")[0]
        cfg = srv.streams.get(name)
        if not cfg or cfg.mode == "down":
            self.send_response(503)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "video/mp2t")
        self.end_headers()
        w, h = cfg.size
        venc = ["-c:v", cfg.vcodec]
        if cfg.vcodec == "libx264":
            venc += ["-preset", "ultrafast", "-tune", "zerolatency", "-g", str(cfg.fps), "-sc_threshold", "0",
                     "-bf", "0", "-pix_fmt", "yuv420p"]
        else:
            venc += ["-b:v", "800k", "-g", str(cfg.fps), "-pix_fmt", "yuv420p"]
        cmd = ["ffmpeg", "-loglevel", "error", "-f", "rawvideo", "-pix_fmt", "gray", "-s", f"{w}x{h}",
               "-r", str(cfg.fps), "-i", "-", "-re", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
               *venc, "-c:a", cfg.acodec, "-f", "mpegts", "-muxdelay", "0", "-flush_packets", "1", "-"]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        srv.connections += 1
        stop = threading.Event()

        def feeder():
            n = 0
            last = bytes([128]) * (w * h)
            t0 = time.monotonic()
            try:
                while not stop.is_set():
                    mode = cfg.mode
                    if mode == "down":
                        break
                    due = t0 + n / cfg.fps
                    delay = due - time.monotonic()
                    if delay > 0:
                        time.sleep(delay)
                    if mode == "stall":
                        n += 1
                        continue
                    if mode == "black":
                        frame = bytes(w * h)
                    elif mode == "freeze":
                        frame = last
                    else:
                        frame = bytes([40 + (n * 53) % 200]) * (w * h)
                    last = frame
                    proc.stdin.write(frame)
                    proc.stdin.flush()
                    n += 1
            except Exception:
                pass
            finally:
                stop.set()

        threading.Thread(target=feeder, daemon=True).start()
        try:
            while not stop.is_set():
                if cfg.mode == "down":
                    break
                if cfg.mode == "stall":
                    time.sleep(0.2)
                    continue
                chunk = proc.stdout.read1(4096) if hasattr(proc.stdout, "read1") else proc.stdout.read(4096)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except Exception:
            pass
        finally:
            stop.set()
            srv.connections -= 1
            try:
                proc.kill()
            except Exception:
                pass


class SimServer:
    def __init__(self, streams: dict[str, StreamCfg]):
        self.streams = streams
        self.connections = 0
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.daemon_threads = True
        self.httpd.sim = self
        self.port = self.httpd.server_address[1]
        self._t = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._t.start()

    def url(self, name: str) -> str:
        return f"http://127.0.0.1:{self.port}/{name}.ts"

    def set_mode(self, name: str, mode: str) -> None:
        self.streams[name].mode = mode

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class HLSServer:
    """HLS ao vivo (2 qualidades + master) gerado pelo FFmpeg e servido por HTTP."""

    def __init__(self):
        self.dir = Path(tempfile.mkdtemp(prefix="sim_hls_"))
        self.procs = []
        for name, (w, h, br) in {"low": (160, 120, "150k"), "high": (320, 240, "400k")}.items():
            d = self.dir / name
            d.mkdir()
            self.procs.append(subprocess.Popen(
                ["ffmpeg", "-loglevel", "error", "-re", "-f", "lavfi", "-i", f"testsrc2=size={w}x{h}:rate=15",
                 "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-c:v", "libx264", "-preset", "ultrafast",
                 "-b:v", br, "-g", "15", "-sc_threshold", "0", "-pix_fmt", "yuv420p", "-c:a", "aac",
                 "-f", "hls", "-hls_time", "2", "-hls_list_size", "6", "-hls_flags", "delete_segments+omit_endlist",
                 str(d / "index.m3u8")], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        (self.dir / "master.m3u8").write_text(
            "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=200000,RESOLUTION=160x120\nlow/index.m3u8\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=500000,RESOLUTION=320x240\nhigh/index.m3u8\n")
        handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(*a, directory=str(self.dir), **k)
        handler.log_message = lambda *a: None
        class Q(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *a): pass
        d = str(self.dir)
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), lambda *a, **k: Q(*a, directory=d, **k))
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        deadline = time.time() + 15
        while time.time() < deadline and not ((self.dir / "high/index.m3u8").exists() and (self.dir / "low/index.m3u8").exists()):
            time.sleep(0.3)
        time.sleep(3)  # alguns segmentos

    def url(self, path: str = "master.m3u8") -> str:
        return f"http://127.0.0.1:{self.port}/{path}"

    def close(self):
        for p in self.procs:
            p.kill()
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.dir, ignore_errors=True)


class StaticServer:
    """Serve uma pasta por HTTP (para páginas de teste, DASH, etc.)."""

    def __init__(self, directory):
        class Q(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *a):
                pass
        d = str(directory)
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), lambda *a, **k: Q(*a, directory=d, **k))
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def url(self, path: str = "") -> str:
        return f"http://127.0.0.1:{self.port}/{path}"

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()
