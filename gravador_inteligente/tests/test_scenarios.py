"""Cenários de ponta a ponta com transmissões simuladas (FFmpeg real, tempo real).

Cada teste leva de 20 a 90 s. Para rodar em paralelo: pytest -n 4 (pytest-xdist) ou vários processos.
"""
import contextlib
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from helpers import *  # noqa
from gravador import merge, util
from gravador.config import SourceCfg
from gravador.session import Session, find_incomplete
from simserver import HLSServer, SimServer, StaticServer, StreamCfg

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def sim():
    s = SimServer({"a": StreamCfg(), "b": StreamCfg()})
    yield s
    s.close()


@contextlib.contextmanager
def running(rec):
    rec.start()
    try:
        yield rec
    finally:
        if rec.running:
            rec.stop(wait=True)


def duration(path) -> float:
    info = util.ffprobe_json(str(path))
    return float(info["format"]["duration"])


def decodes_cleanly(path) -> bool:
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"], capture_output=True)
    return r.returncode == 0 and not r.stderr.strip()


def switch_events(rec):
    return events(rec, "switch")


# ------------------------------------------------------------------ 1. transmissão normal
def test_01_stream_works_normally(tmp_path, sim):
    rec = make_recorder(tmp_path, {"A": sim.url("a"), "B": sim.url("b")}, fast_settings())
    with running(rec):
        wait_until(lambda: rec.state == "gravando" and rec.recorded_seconds() >= 14, 50, msg="gravar 14 s")
    assert rec.failures == 0 and not switch_events(rec)
    assert len(events(rec, "probe_start")) == 1, "a reserva não deve ser aberta sem necessidade"
    assert rec.merge_result["ok"] and rec.merge_result["mode"] == "único"
    final = rec.session.root / rec.merge_result["outputs"][0]
    assert abs(duration(final) - rec.merge_result["total_seconds"]) < 2
    assert decodes_cleanly(final)
    assert (rec.session.root / "relatorio.md").exists() and (rec.session.root / "logs" / "gravador.log").exists()
    assert len(list(rec.session.segments_dir.glob("*.ts"))) >= 3, "gravação deve estar em segmentos"
    assert not rec.session.lock_path.exists()


# ------------------------------------------------------------------ 2. transmissão que congela
def test_02_frozen_video_switches_to_healthy_backup(tmp_path, sim):
    rec = make_recorder(tmp_path, {"A": sim.url("a"), "B": sim.url("b")}, fast_settings())
    with running(rec):
        wait_until(lambda: rec.state == "gravando" and rec.recorded_seconds() >= 6, 40)
        t_freeze = time.time()
        sim.set_mode("a", "freeze")      # dados continuam chegando, imagem parada
        wait_until(lambda: switch_events(rec), 90, msg="troca por vídeo congelado")
        elapsed = time.time() - t_freeze
        assert elapsed >= 8, f"alarme falso: reagiu em {elapsed:.0f}s (< freeze_seconds)"
        sw = switch_events(rec)[0]
        assert sw["tipo"] == "conteudo" and "congelado" in sw["motivo"]
        assert active_label(rec) == "B"
        wait_until(lambda: rec.state == "gravando" and rec.recorded_seconds() >= 20, 40)
    assert rec.merge_result["ok"]


def test_02b_frozen_everywhere_keeps_recording_and_only_warns(tmp_path, sim):
    rec = make_recorder(tmp_path, {"A": sim.url("a"), "B": sim.url("b")}, fast_settings())
    with running(rec):
        wait_until(lambda: rec.state == "gravando" and rec.recorded_seconds() >= 5, 40)
        sim.set_mode("a", "freeze")
        sim.set_mode("b", "freeze")
        wait_until(lambda: events(rec, "content_problem"), 90, msg="aviso sem alternativa")
        assert not switch_events(rec), "trocar para outra fonte congelada não ajuda"
        before = rec.recorded_seconds()
        time.sleep(5)
        assert rec.recorded_seconds() > before + 3, "a gravação deve continuar"
        assert active_label(rec) == "A"


def test_02c_black_screen_detected(tmp_path, sim):
    rec = make_recorder(tmp_path, {"A": sim.url("a"), "B": sim.url("b")}, fast_settings())
    with running(rec):
        wait_until(lambda: rec.state == "gravando" and rec.recorded_seconds() >= 5, 40)
        sim.set_mode("a", "black")
        wait_until(lambda: switch_events(rec), 90, msg="troca por tela preta")
        assert "preta" in switch_events(rec)[0]["motivo"] and active_label(rec) == "B"


# ------------------------------------------------------------------ 3. cai e volta (+ retorno à principal)
def test_03_drops_and_comes_back_with_return_and_no_flapping(tmp_path, sim):
    st = fast_settings(return_to_primary=True, cooldown_seconds=5, primary_probe_interval=3, primary_stable_seconds=6)
    rec = make_recorder(tmp_path, {"A": sim.url("a"), "B": sim.url("b")}, st)
    with running(rec):
        wait_until(lambda: rec.state == "gravando" and rec.recorded_seconds() >= 6, 40)
        sim.set_mode("a", "down")
        wait_until(lambda: active_label(rec) == "B" and rec.state == "gravando", 40, msg="failover para B")
        assert rec.failures == 1
        time.sleep(8)
        assert active_label(rec) == "B", "A ainda está fora: deve permanecer em B"
        sim.set_mode("a", "ok")
        wait_until(lambda: active_label(rec) == "A" and rec.state == "gravando", 90, msg="retorno à principal")
        assert events(rec, "primary_back")
        sw = switch_events(rec)
        assert [(x["de"], x["para"]) for x in sw] == [("A", "B"), ("B", "A")]
        assert sw[1]["t"] - sw[0]["t"] >= st.cooldown_seconds
        n = len(sw)
        time.sleep(12)
        assert len(switch_events(rec)) == n, "não deve ficar alternando"
    rep = rec.session.root / "relatorio.json"
    import json
    r = json.loads(rep.read_text())
    assert r["falhas"] == 1 and r["trocas"] == 2 and r["fontes_em_ordem"] == ["A", "B", "A"]
    assert len(r["lacunas_entre_fontes"]) == 2 and all(g["segundos"] > 0 for g in r["lacunas_entre_fontes"])
    assert rec.merge_result["ok"] and rec.merge_result["segments_total"] >= 5


def test_03b_stalled_connection_is_detected_without_error(tmp_path, sim):
    """Conexão aberta, zero dados (o site 'continua aberto'): precisa trocar mesmo assim."""
    rec = make_recorder(tmp_path, {"A": sim.url("a"), "B": sim.url("b")}, fast_settings())
    with running(rec):
        wait_until(lambda: rec.state == "gravando" and rec.recorded_seconds() >= 5, 40)
        sim.set_mode("a", "stall")
        wait_until(lambda: switch_events(rec), 40, msg="troca por travamento")
        assert "sem novos dados" in events(rec, "failure_detected")[0]["message"] or "mídia" in events(rec, "failure_detected")[0]["message"]
        assert active_label(rec) == "B"


def test_03c_short_blip_does_not_cause_switch(tmp_path, sim):
    """Interrupção menor que o limiar de tolerância não pode disparar troca (anti falso alarme)."""
    st = fast_settings(stall_data_seconds=6, stall_time_seconds=8, suspect_grace_seconds=4)
    rec = make_recorder(tmp_path, {"A": sim.url("a"), "B": sim.url("b")}, st)
    with running(rec):
        wait_until(lambda: rec.state == "gravando" and rec.recorded_seconds() >= 5, 40)
        sim.set_mode("a", "stall")
        time.sleep(2.5)
        sim.set_mode("a", "ok")
        time.sleep(8)
        assert not switch_events(rec) and rec.failures == 0 and active_label(rec) == "A"


# ------------------------------------------------------------------ 4. principal indisponível, reserva funcional
def test_04_primary_unavailable_backup_works(tmp_path, sim):
    sim.set_mode("a", "down")
    rec = make_recorder(tmp_path, {"A": sim.url("a"), "B": sim.url("b")}, fast_settings())
    with running(rec):
        wait_until(lambda: rec.state == "gravando" and rec.recorded_seconds() >= 10, 50)
        assert active_label(rec) == "B"
        a = next(s for s in rec.sources if s.cfg.label == "A")
        assert a.status == "down" and a.reason, "a fonte principal deve aparecer como indisponível, com motivo"
        snap = rec.snapshot()
        assert {s["label"]: s["status"] for s in snap["sources"]} == {"A": "down", "B": "recording"}
    assert rec.merge_result["ok"]


# ------------------------------------------------------------------ 5. todas indisponíveis
def test_05_all_sources_down_then_recover(tmp_path, sim):
    sim.set_mode("a", "down"); sim.set_mode("b", "down")
    rec = make_recorder(tmp_path, {"A": sim.url("a"), "B": sim.url("b")}, fast_settings())
    with running(rec):
        wait_until(lambda: rec.state == "sem_fonte", 40)
        wait_until(lambda: events(rec, "all_down"), 20)
        assert any("TODAS" in t for t in [n["title"] for n in rec.notifier.history])
        assert rec.running, "deve continuar tentando, sem encerrar"
        sim.set_mode("b", "ok")
        wait_until(lambda: rec.state == "gravando" and rec.recorded_seconds() >= 5, 60, msg="recuperar")
        assert active_label(rec) == "B"


def test_05b_all_down_in_the_middle_preserves_what_was_recorded(tmp_path, sim):
    rec = make_recorder(tmp_path, {"A": sim.url("a"), "B": sim.url("b")}, fast_settings())
    with running(rec):
        wait_until(lambda: rec.state == "gravando" and rec.recorded_seconds() >= 8, 40)
        sim.set_mode("a", "down"); sim.set_mode("b", "down")
        wait_until(lambda: rec.state == "sem_fonte", 60)
        saved = sorted(p.name for p in rec.session.segments_dir.glob("*.ts"))
        assert len(saved) >= 2
        sim.set_mode("a", "ok")
        wait_until(lambda: rec.state == "gravando" and rec.active is not None and rec.active.out_time >= 4, 60)
        assert events(rec, "recovered")
        still = {p.name for p in rec.session.segments_dir.glob("*.ts")}
        assert set(saved) <= still, "segmentos anteriores à falha não podem sumir"
    assert rec.merge_result["ok"]


# ------------------------------------------------------------------ 6. encerramento inesperado
def _orphans(marker: str) -> list[str]:
    out = subprocess.run(["pgrep", "-af", "ffmpeg"], capture_output=True, text=True).stdout
    return [l for l in out.splitlines() if marker in l]


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="kill -9 / PDEATHSIG testado no Linux")
def test_06_unexpected_kill_then_recover_and_resume(tmp_path, sim):
    out = tmp_path / "rec"
    env = {**os.environ, "GRAVADOR_HOME": str(tmp_path / "home"), "PYTHONPATH": str(ROOT)}
    cmd = [sys.executable, "-m", "gravador", "record", "-n", "Jogo Morto", "-o", str(out), "-s", sim.url("a"),
           "--segment", "3", "--set", "notify_desktop=false", "--set", "validate_media_seconds=2"]
    p = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    seg_dir = out / "Jogo_Morto" / "segments"
    try:
        wait_until(lambda: len(list(seg_dir.glob("*.ts"))) >= 4, 60, msg="4 segmentos")
    finally:
        p.send_signal(signal.SIGKILL)      # morte brusca: sem finalizar nada
        p.wait()
    before = {f.name: f.stat().st_size for f in seg_dir.glob("*.ts")}
    time.sleep(3)
    assert not _orphans(str(seg_dir)), "FFmpeg não pode ficar órfão gravando sem supervisão"
    assert not list((out / "Jogo_Morto").glob("*.mkv")), "nada foi finalizado"

    inc = find_incomplete(out)
    assert len(inc) == 1 and inc[0].meta["status"] == "recording"

    r = subprocess.run([sys.executable, "-m", "gravador", "recover", str(out / "Jogo_Morto")], cwd=ROOT, env=env,
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    root = out / "Jogo_Morto"
    final = next(root.glob("*.mkv"))
    assert duration(final) >= 6
    for name, size in before.items():
        assert (seg_dir / name).exists() and (seg_dir / name).stat().st_size >= size, "segmento preservado"
    assert (root / "relatorio.md").exists() and "recuperada" in (root / "relatorio.md").read_text().lower()
    assert not find_incomplete(out)

    # retomar a gravação interrompida na mesma pasta
    sess = Session.open(root)
    st = fast_settings()
    rec = Recorder(sess, [SourceCfg(url=sim.url("a"), label="A", id="aaaaaa")], st, Notifier(st), max_duration=10)
    rec.start(); rec.join(90)
    assert not rec.running
    names = {f.name for f in seg_dir.glob("*.ts")}
    assert set(before) <= names and any(n.startswith("c002_") or n.startswith("c003_") for n in names), "capturas novas, sem sobrescrever"
    assert len(list(root.glob("*.mkv"))) == 2, "o arquivo anterior não é substituído"


# ------------------------------------------------------------------ 7. falta de espaço em disco
def test_07_low_disk_space_warns_then_stops_safely(tmp_path, sim, monkeypatch):
    real = util.free_bytes
    flag = {"v": None}
    monkeypatch.setattr(util, "free_bytes", lambda p: flag["v"] if flag["v"] is not None else real(p))
    rec = make_recorder(tmp_path, {"A": sim.url("a")}, fast_settings(disk_warn_gb=5, disk_critical_gb=1))
    with running(rec):
        wait_until(lambda: rec.state == "gravando" and rec.recorded_seconds() >= 6, 40)
        flag["v"] = int(3e9)                       # abaixo do aviso
        wait_until(lambda: events(rec, "disk_low"), 10, msg="aviso de pouco espaço")
        assert rec.running
        flag["v"] = int(0.2e9)                     # crítico
        wait_until(lambda: not rec.running, 40, msg="parada segura")
    assert events(rec, "disk_critical") and "disco" in rec.stop_reason
    assert rec.merge_result["ok"], "o que foi gravado precisa ser preservado e unido"
    assert any("Disco" in n["title"] or "disco" in n["title"] for n in rec.notifier.history)
    assert duration(rec.session.root / rec.merge_result["outputs"][0]) >= 6


# ------------------------------------------------------------------ 8. codecs / resoluções diferentes
def test_08_different_codecs_and_resolutions_between_sources(tmp_path):
    sim = SimServer({"a": StreamCfg(size=(320, 240)),
                     "b": StreamCfg(size=(640, 360), fps=25, vcodec="mpeg2video", acodec="mp2")})
    try:
        rec = make_recorder(tmp_path, {"A": sim.url("a"), "B": sim.url("b")}, fast_settings())
        with running(rec):
            wait_until(lambda: rec.state == "gravando" and rec.recorded_seconds() >= 6, 40)
            sim.set_mode("a", "down")
            wait_until(lambda: active_label(rec) == "B" and rec.state == "gravando", 40)
            wait_until(lambda: rec.active.out_time >= 6, 40)
    finally:
        sim.close()
    m = rec.merge_result
    assert len(m["groups"]) == 2 and m["mode"] == "partes", m
    assert len(m["outputs"]) == 2 and (rec.session.root / "ordem_reproducao.m3u").exists()
    assert "h264" in m["groups"][0]["signature"] and "mpeg2video" in m["groups"][1]["signature"]
    seg_before = sorted(p.name for p in rec.session.segments_dir.glob("*.ts"))

    # etapa opcional: normalizar para um formato comum
    res = merge.finalize(rec.session.root, "Teste", rec.st, normalize=True)
    assert res["mode"] == "normalizado" and res["ok"], res
    final = rec.session.root / res["outputs"][0]
    info = util.ffprobe_json(str(final))
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert (v["width"], v["height"]) == (640, 360)
    assert abs(duration(final) - res["total_seconds"]) < max(3, res["total_seconds"] * 0.08)
    assert decodes_cleanly(final)
    assert sorted(p.name for p in rec.session.segments_dir.glob("*.ts")) == seg_before, "originais intactos"


# ------------------------------------------------------------------ HLS / DASH / página
def test_09_hls_master_quality_and_recording(tmp_path):
    hls = HLSServer()
    try:
        from gravador import probe
        best = probe.test_source(SourceCfg(url=hls.url()), fast_settings(quality="best", stall_data_seconds=6))
        worst = probe.test_source(SourceCfg(url=hls.url()), fast_settings(quality="worst"))
        assert best["ok"], best
        assert best["info"]["video"]["height"] == 240 and worst["info"]["video"]["height"] == 120
        st = fast_settings(quality="best", stall_data_seconds=7, stall_time_seconds=9)
        rec = make_recorder(tmp_path, {"HLS": hls.url()}, st)
        with running(rec):
            wait_until(lambda: rec.state == "gravando" and rec.recorded_seconds() >= 10, 60)
        assert rec.failures == 0 and rec.merge_result["ok"]
    finally:
        hls.close()


def test_10_dash_representation_choice(tmp_path):
    d = tmp_path / "dash"; d.mkdir()
    r = subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=320x240:rate=15",
                        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000", "-t", "6",
                        "-filter_complex", "[0:v]split=2[a][b];[b]scale=160:120[c]", "-map", "[a]", "-map", "[c]", "-map", "1:a",
                        "-c:v", "libx264", "-g", "15", "-c:a", "aac", "-f", "dash", "-seg_duration", "2", str(d / "m.mpd")],
                       capture_output=True, text=True)
    if r.returncode != 0:
        pytest.skip("muxer DASH indisponível: " + r.stderr[-120:])
    srv = StaticServer(d)
    try:
        from gravador import sources
        best = sources.resolve_direct(srv.url("m.mpd"), {}, "best")
        worst = sources.resolve_direct(srv.url("m.mpd"), {}, "worst")
        assert "320x240" in best.note and "160x120" in worst.note
        assert best.map_args != worst.map_args
        ok = subprocess.run(["ffmpeg", "-v", "error", "-t", "3", "-i", best.url, *best.map_args, "-c", "copy", "-f", "null", "-"],
                            capture_output=True)
        assert ok.returncode == 0, ok.stderr
    finally:
        srv.close()


def _page_env(tmp_path, script: str):
    hls = HLSServer()
    (hls.dir / "jogo.html").write_text(f"<html><body><video id=v></video><script>{script}</script></body></html>")
    return hls


def test_11_page_player_url_is_discovered_from_network(tmp_path):
    from gravador import sources
    hls = _page_env(tmp_path, "fetch('/master.m3u8?sess=1').catch(()=>{});")
    try:
        try:
            r = sources.resolve_page(SourceCfg(url=hls.url("jogo.html")), "best", tmp_path / "prof", timeout=25)
        except sources.SourceError as e:
            if "navegador" in str(e):
                pytest.skip(str(e))
            raise
        assert r.url.endswith("high/index.m3u8") and "Referer" in r.headers
        assert not any(k.lower() in ("cookie", "authorization") for k in r.headers), "credenciais não são repassadas"
        st = fast_settings(stall_data_seconds=7, stall_time_seconds=9)
        rec = make_recorder(tmp_path / "out", {"PAGINA": hls.url("jogo.html")}, st)
        rec.profile_dir = tmp_path / "prof"
        with running(rec):
            wait_until(lambda: rec.state == "gravando" and rec.recorded_seconds() >= 8, 90)
        assert rec.merge_result["ok"]
    finally:
        hls.close()


def test_12_page_with_drm_is_refused_with_clear_reason(tmp_path):
    from gravador import sources, probe
    js = ("navigator.requestMediaKeySystemAccess('com.widevine.alpha',[{initDataTypes:['cenc'],"
          "videoCapabilities:[{contentType:'video/mp4;codecs=\"avc1.42E01E\"'}]}]).catch(()=>{});"
          "fetch('/master.m3u8').catch(()=>{});")
    hls = _page_env(tmp_path, js)
    try:
        try:
            with pytest.raises(sources.ProtectedSourceError) as e:
                sources.resolve_page(SourceCfg(url=hls.url("jogo.html")), "best", tmp_path / "prof", timeout=25)
        except sources.SourceError as e2:
            if "navegador" in str(e2):
                pytest.skip(str(e2))
            raise
        assert "DRM" in str(e.value) and "não tenta contornar" in str(e.value)
    finally:
        hls.close()
