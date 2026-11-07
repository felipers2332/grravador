"""Testes rápidos (sem transmissão ao vivo)."""
import json
import os
import threading
import http.server

import pytest

from helpers import *  # noqa
from gravador import merge, sources, util
from gravador.config import AppConfig, Settings, SourceCfg
from gravador.monitor import FRAME_BYTES, HealthMonitor, analyze_frames
from gravador.notifier import Notifier
from gravador.session import Session, find_incomplete

MASTER = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=800000,RESOLUTION=640x360
360/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=3000000,RESOLUTION=1280x720
720/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=6000000,RESOLUTION=1920x1080
1080/index.m3u8
"""


def test_classify_links():
    assert sources.classify("https://x.com/live/index.m3u8?token=abc") == "direct"
    assert sources.classify("https://x.com/a/manifest.mpd") == "direct"
    assert sources.classify("rtmp://x/live/y") == "direct"
    assert sources.classify("https://liga.com/jogo/123") == "page"


@pytest.mark.parametrize("q,expected", [("best", "1080"), ("worst", "360"), ("720", "720"), ("900", "720"), ("200", "360")])
def test_hls_quality_selection(q, expected):
    url, _ = sources.pick_hls_variant(MASTER, "https://x.com/m/master.m3u8", q)
    assert url == f"https://x.com/m/{expected}/index.m3u8"


def test_drm_is_refused_not_bypassed():
    fairplay = '#EXTM3U\n#EXT-X-KEY:METHOD=SAMPLE-AES,URI="skd://k",KEYFORMAT="com.apple.streamingkeydelivery"\n#EXTINF:4,\na.ts'
    widevine = '#EXTM3U\n#EXT-X-KEY:METHOD=SAMPLE-AES-CTR,KEYFORMAT="urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed",URI="data:x"\n#EXTINF:4,\na.ts'
    dash = '<MPD><Period><AdaptationSet><ContentProtection schemeIdUri="urn:uuid:edef8ba9-79d6-4ace-a3c8-27dcd51d21ed"/></AdaptationSet></Period></MPD>'
    for text in (fairplay, widevine, dash):
        with pytest.raises(sources.ProtectedSourceError):
            sources.check_protection(text)
    # AES-128 comum com chave aberta NÃO é DRM
    sources.check_protection('#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="key.bin"\n#EXTINF:4,\na.ts')
    sources.check_protection('#EXTM3U\n#EXTINF:4,\na.ts')


def test_only_safe_headers_forwarded():
    h = sources._clean_headers({"Cookie": "s=1", "Authorization": "Bearer x", "Referer": "https://a", "User-Agent": "u"})
    assert set(k.lower() for k in h) == {"referer", "user-agent"}


def test_auth_wall_gives_clear_message():
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(403); self.end_headers()
        def log_message(self, *a): pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with pytest.raises(sources.SourceError) as e:
            sources.resolve_direct(f"http://127.0.0.1:{srv.server_address[1]}/live.m3u8", {}, "best")
        assert "403" in str(e.value) and "não contorna" in str(e.value)
    finally:
        srv.shutdown()


def frame(v):
    return bytes([v]) * FRAME_BYTES


def test_content_analysis_freeze_black_motion():
    assert analyze_frames([frame(100)] * 5)["motion"] is False
    assert analyze_frames([frame(100), frame(180), frame(60)])["motion"] is True
    assert analyze_frames([frame(0)] * 4)["black"] is True
    assert analyze_frames([frame(0), frame(120)])["black"] is False
    # poucos quadros = indeterminado, nunca alarme
    assert analyze_frames([frame(100)])["motion"] is None
    assert analyze_frames([])["motion"] is None


class FakeCap:
    """Captura falsa para testar a lógica de decisão do monitor sem FFmpeg."""
    def __init__(self, now):
        self.alive = True; self.started = now - 60; self.first_data = now - 59
        self.last_bytes_change = now; self.last_time_advance = now; self.bytes = 1000
        self.speed = 1.0; self.content = {}; self.out_time = 10
    def poll(self): pass
    def reap(self, *a): pass
    def failure_reason(self): return "morreu"


def test_monitor_debounce_levels():
    st = Settings(stall_data_seconds=6, stall_time_seconds=8)
    now = 1000.0
    cap, mon = FakeCap(now), HealthMonitor(st)
    assert mon.evaluate(cap, now).level == "ok"
    cap.last_bytes_change = now - 4       # atraso pequeno: amarelo, não vermelho
    assert mon.evaluate(cap, now).level == "warn"
    cap.last_bytes_change = now - 7       # dados parados: vermelho
    v = mon.evaluate(cap, now)
    assert v.level == "bad" and v.kind == "hard"
    cap.last_bytes_change = now; cap.last_time_advance = now - 9   # bytes ok, mídia parada
    assert mon.evaluate(cap, now).level == "bad"
    cap.last_time_advance = now
    cap.alive = False
    assert mon.evaluate(cap, now).dead


def test_monitor_freeze_and_black_are_content_failures():
    st = Settings(freeze_seconds=30, black_seconds=30)
    now = 1000.0
    cap, mon = FakeCap(now), HealthMonitor(st)
    cap.content = {"last_motion": now - 40, "last_nonblack": now}
    v = mon.evaluate(cap, now)
    assert v.level == "bad" and v.kind == "content" and "congelado" in v.reasons[0]
    cap.content = {"last_motion": now, "last_nonblack": now - 40}
    assert "preta" in mon.evaluate(cap, now).reasons[0]


def test_monitor_prolonged_buffering_needs_time_to_turn_red():
    st = Settings(buffering_warn_seconds=15, buffering_bad_seconds=60)
    cap, mon, t = FakeCap(1000.0), HealthMonitor(Settings(buffering_warn_seconds=15, buffering_bad_seconds=60)), 1000.0
    cap.speed = 0.4
    levels = []
    for dt in (0, 10, 20, 70):
        cap.last_bytes_change = cap.last_time_advance = t + dt
        levels.append(mon.evaluate(cap, t + dt).level)
    assert levels == ["ok", "ok", "warn", "bad"]


def test_friendly_errors():
    assert "403" in util.friendly_error(["Server returned 403 Forbidden (access denied)"])
    assert "DNS" in util.friendly_error(["Failed to resolve hostname x"])
    assert util.friendly_error(["tudo bem"]) is None


def test_session_incomplete_detection(tmp_path):
    s = Session.create(tmp_path, "Jogo 1")
    s.meta["pid"] = 999999          # processo inexistente
    s.save_meta()
    assert [x.root for x in find_incomplete(tmp_path)] == [s.root]
    s.meta["pid"] = os.getpid()
    s.save_meta()
    assert find_incomplete(tmp_path) == []
    s.meta.update(status="finished"); s.save_meta()
    assert find_incomplete(tmp_path) == []


def test_session_never_reuses_folder(tmp_path):
    a = Session.create(tmp_path, "Jogo")
    (a.segments_dir / "c001_x_00000.ts").write_bytes(b"x")
    b = Session.create(tmp_path, "Jogo")
    assert a.root != b.root


def test_events_survive_truncated_last_line(tmp_path):
    s = Session.create(tmp_path, "J")
    s.emit("a"); s.emit("b")
    with open(s.events_path, "a") as f:
        f.write('{"type": "trunc')      # queda no meio da escrita
    assert [e["type"] for e in s.read_events()] == ["a", "b"]


def test_settings_roundtrip_and_coercion(tmp_path):
    cfg = AppConfig(name="X", out_dir=str(tmp_path))
    cfg.sources.append(SourceCfg(url="http://a/b.m3u8", label="P"))
    cfg.settings.update({"segment_seconds": "30", "return_to_primary": "false", "notify_webhooks": "http://a, http://b"})
    cfg.save(tmp_path / "c.json")
    back = AppConfig.load(tmp_path / "c.json")
    assert back.settings.segment_seconds == 30 and back.settings.return_to_primary is False
    assert back.settings.notify_webhooks == ["http://a", "http://b"] and back.sources[0].label == "P"
    assert "password" not in json.dumps(json.loads((tmp_path / "c.json").read_text())).lower()


def test_notifier_throttle_webhook_and_command(tmp_path):
    got = []
    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            got.append(json.loads(self.rfile.read(int(self.headers["Content-Length"])))); self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
        def log_message(self, *a): pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    out = tmp_path / "cmd.txt"
    st = Settings(notify_desktop=False, notify_webhooks=[f"http://127.0.0.1:{srv.server_address[1]}/"],
                  notify_command=f'echo "$GRAVADOR_MSG" >> {out}', notify_min_interval_s=60)
    n = Notifier(st)
    n.notify("problem", "Falha em A", "sem dados", key="k")
    n.notify("problem", "Falha em A", "sem dados", key="k")      # suprimida (anti-spam)
    n.notify("switch", "Troca", "A → B", force=True)
    wait_until(lambda: len(got) == 2 and out.exists() and len(out.read_text().splitlines()) == 2, 8, msg="entrega")
    srv.shutdown()
    assert len(n.history) == 3 and "Falha em A" in got[0]["text"]
    assert "Falha" in out.read_text()


def test_group_compatible_and_signature_text():
    mk = lambda sig: merge.SegInfo("p", 1, "a", 0, 1, 1.0, sig, True)
    s1, s2 = ("h264", "High", 1280, 720, 30, "aac", "48000", 2), ("mpeg2video", "Main", 640, 360, 25, "mp2", "48000", 2)
    groups = merge.group_compatible([mk(s1), mk(s1), mk(s2), mk(s1)])
    assert [len(g) for g in groups] == [2, 1, 1]
    assert "1280x720" in merge.sig_text(s1)
