"""Motor de gravação: supervisiona a captura ativa, detecta falhas, troca de fonte e retorna à principal."""
from __future__ import annotations

import shutil
import threading
import time
from pathlib import Path

from . import merge, probe, report, sources, util
from .capture import Capture
from .config import Settings, SourceCfg
from .monitor import ContentWatcher, HealthMonitor, Verdict, analyze_frames, grab_keyframes
from .notifier import Notifier
from .session import Session

log = util.log


class SourceState:
    def __init__(self, cfg: SourceCfg):
        self.cfg = cfg
        self.status = "idle"        # idle | testing | ok | warn | down
        self.reason = ""
        self.last_check: float | None = None
        self.failures = 0
        self.info: dict = {}

    def mark(self, status: str, reason: str = ""):
        self.status, self.reason, self.last_check = status, reason, time.time()


class Recorder:
    def __init__(self, session: Session, source_cfgs: list[SourceCfg], settings: Settings,
                 notifier: Notifier | None = None, profile_dir: Path | None = None,
                 scheduled_at: float | None = None, max_duration: float | None = None):
        self.session = session
        self.st = settings
        self.notifier = notifier or Notifier(settings)
        self.profile_dir = profile_dir
        self.scheduled_at = scheduled_at
        self.max_duration = max_duration
        self.sources: list[SourceState] = [SourceState(c) for c in source_cfgs]
        self._src_lock = threading.RLock()
        self.listeners: list = []
        self.state = "parada"
        self.active: Capture | None = None
        self.active_src: SourceState | None = None
        self.monitor = HealthMonitor(settings)
        self.watcher: ContentWatcher | None = None
        self.verdict = Verdict()
        self.failures = 0
        self.switches: list[dict] = []
        self.last_switch_t = 0.0
        self.t_start = 0.0
        self._stop_evt = threading.Event()
        self._thread: threading.Thread | None = None
        self._bad_since: float | None = None
        self._ok_since: float | None = None
        self._same_retries_left = settings.same_source_retries
        self._ctx: dict | None = None            # contexto de troca em andamento
        self._all_down_notified = False
        self._warned = False
        self._next_content_attempt = 0.0
        self._ret_probe: tuple | None = None     # (SourceState, Capture, HealthMonitor, t_stable)
        self._ret_starting = False
        self._ret_ready: tuple | None = None
        self._return_attempts = 0
        self._next_return_probe = 0.0
        self._last_persist = 0.0
        self._disk_warned_at = 0.0
        self._bytes_cache = (0.0, 0)
        self.stop_reason = ""
        self.merge_result: dict | None = None

    # ------------------------------------------------------------ API pública
    def start(self) -> None:
        self.session.acquire_lock()
        self.session.meta["status"] = "recording"
        self.session.meta["ended"] = None
        self.session.save_meta()
        util.setup_logging(self.session.logs_dir / "gravador.log", console=False)
        self.t_start = time.time()
        self._thread = threading.Thread(target=self._run, name="recorder", daemon=True)
        self._thread.start()

    def stop(self, wait: bool = True, reason: str = "parada pelo usuário") -> None:
        self.stop_reason = self.stop_reason or reason
        self._stop_evt.set()
        if wait and self._thread:
            self._thread.join(timeout=300)

    def join(self, timeout: float | None = None) -> None:
        if self._thread:
            self._thread.join(timeout)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def add_source(self, cfg: SourceCfg) -> None:
        with self._src_lock:
            self.sources.append(SourceState(cfg))
        self._emit("source_added", f"Fonte adicionada: {cfg.display()}")

    def remove_source(self, sid: str) -> None:
        with self._src_lock:
            self.sources = [s for s in self.sources if s.cfg.id != sid]
        self._emit("source_removed", f"Fonte removida: {sid}")

    def set_order(self, ids: list[str]) -> None:
        with self._src_lock:
            by = {s.cfg.id: s for s in self.sources}
            new = [by[i] for i in ids if i in by] + [s for s in self.sources if s.cfg.id not in ids]
            self.sources = new

    def set_enabled(self, sid: str, enabled: bool) -> None:
        with self._src_lock:
            for s in self.sources:
                if s.cfg.id == sid:
                    s.cfg.enabled = enabled

    def _ordered(self, failed: SourceState | None = None) -> list[SourceState]:
        with self._src_lock:
            lst = [s for s in self.sources if s.cfg.enabled]
        if failed in lst:                       # a que acabou de falhar é a última tentativa
            lst.remove(failed)
            lst.append(failed)
        return lst

    # ------------------------------------------------------------ eventos
    def _emit(self, type_: str, message: str = "", source: str | None = None, **extra) -> dict:
        ev = self.session.emit(type_, message=message, source=source, **extra)
        log.info("[%s] %s%s", type_, f"({source}) " if source else "", message)
        for cb in list(self.listeners):
            try:
                cb(ev)
            except Exception:
                pass
        return ev

    def _wait(self, secs: float) -> bool:
        """Espera interrompível. True se foi pedido para parar."""
        return self._stop_evt.wait(secs)

    # ------------------------------------------------------------ laço principal
    def _run(self):
        try:
            self._emit("session_start", f"Sessão '{self.session.meta.get('name')}' iniciada",
                       sources=[s.cfg.display() for s in self.sources])
            if self.scheduled_at and self.scheduled_at > time.time():
                self.state = "agendada"
                self._emit("scheduled", "Gravação agendada para " + time.strftime("%d/%m %H:%M:%S", time.localtime(self.scheduled_at)))
                while time.time() < self.scheduled_at:
                    if self._wait(1):
                        return
            self.t_start = time.time()
            while not self._stop_evt.is_set():
                if self.active is None:
                    self.state = "iniciando" if self._ctx is None else "trocando"
                    if not self._acquire():
                        continue
                self._tick()
                if self.max_duration and time.time() - self.t_start >= self.max_duration:
                    self.stop_reason = self.stop_reason or "duração máxima atingida"
                    break
                self._wait(self.st.poll_interval)
        except Exception as e:  # nunca morrer em silêncio
            log.exception("erro fatal no laço principal")
            self._emit("error", f"Erro interno: {e!r}")
            self.stop_reason = self.stop_reason or f"erro interno: {e!r}"
        finally:
            self._shutdown()

    # ------------------------------------------------------------ início / validação de capturas
    def _start_validated(self, st: SourceState, hot: bool, require_motion: bool = False) -> Capture | None:
        st.mark("testing")
        self._emit("probe_start", "Testando fonte", source=st.cfg.display())
        try:
            resolved = sources.resolve(st.cfg, self.st.quality, self.profile_dir)
        except sources.SourceError as e:
            st.mark("down", str(e))
            st.failures += 1
            self._emit("resolve_failed", str(e), source=st.cfg.display())
            return None
        except Exception as e:
            st.mark("down", f"erro ao resolver: {e}")
            self._emit("resolve_failed", f"erro ao resolver: {e}", source=st.cfg.display())
            return None
        cid = self.session.next_capture_id()
        out_dir = self.session.segments_dir if hot else self.session.probe_dir
        cap = Capture(cid, st.cfg, resolved, out_dir, self.st, staging=not hot)
        try:
            cap.start()
        except Exception as e:
            st.mark("down", f"não foi possível iniciar o FFmpeg: {e}")
            self._emit("probe_fail", str(e), source=st.cfg.display())
            return None
        ok, why = probe.validate_capture(cap, self.st, self._stop_evt, require_motion)
        if ok:
            st.info = cap.info
            return cap
        cap.stop("falha na validação")
        self.session.upsert_capture(cap.summary())
        if not hot:
            self._purge_probe(cap)
        st.mark("down", why)
        st.failures += 1
        self._emit("probe_fail", f"não validada: {why}", source=st.cfg.display())
        return None

    def _purge_probe(self, cap: Capture) -> None:
        for f in cap.segment_files():
            try:
                f.unlink()
            except OSError:
                pass

    def _promote(self, cap: Capture, st: SourceState) -> None:
        ctx, self._ctx = self._ctx, None
        if ctx is not None and ctx.get("failed") is None:
            ctx = None  # primeira aquisição (mesmo após tentativas falhas): é o início, não uma troca
        self.active, self.active_src = cap, st
        self.monitor = HealthMonitor(self.st)
        self._bad_since = self._ok_since = None
        self._same_retries_left = self.st.same_source_retries
        self._warned = False
        if self.watcher:
            self.watcher.stop()
        self.watcher = None
        if self.st.freeze_detect:
            self.watcher = ContentWatcher(cap, self.st)
            self.watcher.start()
        st.mark("ok")
        now = time.time()
        self.state = "gravando"
        self.session.upsert_capture(cap.summary())
        info = cap.info
        desc = ""
        if info.get("video"):
            v = info["video"]
            desc = f"{v['codec']} {v['width']}x{v['height']} {v['fps']:g}fps"
            if info.get("audio"):
                desc += f" + áudio {info['audio']['codec']}"
        if ctx is None:
            self._emit("capture_start", f"Gravando via {st.cfg.display()} ({desc})", source=st.cfg.display())
            self.notifier.notify("start", "Gravação iniciada", f"{self.session.meta.get('name')} — fonte: {st.cfg.display()}",
                                 force=True)
            return
        failed = ctx.get("failed")
        # interrupção real: do último dado recebido da fonte anterior ao primeiro dado da nova
        gap = max(0.0, (cap.first_data or now) - ctx.get("t_lost", ctx["t"]))
        if failed is st:
            self._emit("recovered", f"{st.cfg.display()} voltou a funcionar após {gap:.0f}s", source=st.cfg.display())
            self.notifier.notify("recovered", "Fonte recuperada", f"{st.cfg.display()} voltou ({ctx['reason']})", force=True)
        else:
            rec = {"t": now, "ts": util.now_iso(), "de": failed.cfg.display() if failed else None,
                   "para": st.cfg.display(), "motivo": ctx["reason"], "tipo": ctx.get("kind", "falha"),
                   "interrupcao_s": round(gap, 1)}
            self.switches.append(rec)
            self.last_switch_t = now
            self._emit("switch", f"{rec['de']} → {rec['para']} ({ctx['reason']}); interrupção ≈ {gap:.0f}s",
                       source=rec["para"], de=rec["de"], para=rec["para"], motivo=ctx["reason"],
                       tipo=ctx.get("kind", "falha"), interrupcao_s=rec["interrupcao_s"])
            if ctx.get("kind") == "retorno":
                self.notifier.notify("primary_back", "Retorno à transmissão principal",
                                     f"{rec['de']} → {rec['para']}", force=True)
            else:
                self.notifier.notify("switch", "Troca automática de transmissão",
                                     f"Falhou: {rec['de']} ({ctx['reason']}). Em uso: {rec['para']}.", force=True)
        self._all_down_notified = False
        self._return_attempts = 0
        self._next_return_probe = now + self.st.primary_probe_interval

    def _acquire(self) -> bool:
        """Tenta as fontes em ordem de prioridade até uma validar. True se conseguiu."""
        cands = self._ordered(failed=self._ctx.get("failed") if self._ctx else None)
        if not cands:
            self._emit("all_down", "Nenhuma fonte habilitada cadastrada")
            self._wait(self.st.all_down_retry_seconds)
            return False
        for st in cands:
            if self._stop_evt.is_set():
                return False
            if self._ctx is not None and self.active is None:
                self.state = "trocando"
            cap = self._start_validated(st, hot=True, require_motion=bool(self._ctx and self._ctx.get("kind") == "conteudo"))
            if cap:
                self._promote(cap, st)
                return True
        # nenhuma funcionou
        self.state = "sem_fonte"
        if self._ctx is None:
            self._ctx = {"failed": None, "reason": "nenhuma fonte disponível ao iniciar", "t": time.time()}
        if not self._all_down_notified:
            self._all_down_notified = True
            self._emit("all_down", "Todas as fontes estão indisponíveis; tentando novamente periodicamente")
            self.notifier.notify("all_down", "TODAS as fontes indisponíveis",
                                 "Última falha: " + (self._ctx.get("reason") or "?"), force=True)
        self._wait(self.st.all_down_retry_seconds)
        return False

    # ------------------------------------------------------------ supervisão
    def _tick(self):
        now = time.time()
        cap = self.active
        self._check_disk()
        if self._stop_evt.is_set():
            return
        v = self.monitor.evaluate(cap, now)
        self.verdict = v
        st = self.active_src
        st.status = {"ok": "ok", "warn": "warn", "bad": "down"}[v.level] if v.kind != "starting" else "warn"
        st.reason = "; ".join(v.reasons)
        st.last_check = now
        if now - self._last_persist > 10:
            self._last_persist = now
            self.session.upsert_capture(cap.summary())
            self.session.save_meta()
        if v.level == "bad":
            self._ok_since = None
            self._handle_bad(v, now)
            return
        self._ok_since = self._ok_since or now
        if now - self._ok_since >= 3:
            if self._bad_since is not None:
                self._emit("transient_ok", "Problema temporário passou; seguindo na mesma fonte", source=st.cfg.display())
            self._bad_since = None
        if v.level == "warn" and v.kind != "starting" and not self._warned:
            self._warned = True
            self._emit("health_warn", "Instabilidade: " + "; ".join(v.reasons), source=st.cfg.display())
        elif v.level == "ok":
            self._warned = False
        self._maybe_return(now)

    def _handle_bad(self, v: Verdict, now: float):
        st = self.active_src
        if self._bad_since is None:
            self._bad_since = now
            kind_msg = "Problema de conteúdo" if v.kind == "content" else "Transmissão com problemas"
            self._emit("health_bad", "; ".join(v.reasons), source=st.cfg.display(), kind=v.kind)
            self.notifier.notify("problem", f"{kind_msg}: {st.cfg.display()}", "; ".join(v.reasons), key="problem:" + st.cfg.id)
        grace = self.st.suspect_grace_seconds
        waited = now - self._bad_since

        if v.kind == "content":
            if self.st.freeze_action == "warn" or waited < grace:
                return
            if now - self.last_switch_t < self.st.cooldown_seconds or now < self._next_content_attempt:
                return
            self._next_content_attempt = now + max(60.0, self.st.cooldown_seconds)
            self._content_switch("; ".join(v.reasons))
            return

        if v.dead and self._same_retries_left > 0 and waited >= min(1.5, grace):
            self._same_retries_left -= 1
            self._emit("retry_same", "Tentando religar a mesma fonte antes de trocar", source=st.cfg.display())
            old = self.active
            old.stop("encerrou; religando")
            self.session.upsert_capture(old.summary())
            new = self._start_validated(st, hot=True)
            if new:
                keep = self._same_retries_left
                self._ctx = {"failed": st, "reason": "; ".join(v.reasons), "t": self._bad_since, "t_lost": old.last_bytes_change, "kind": "religado"}
                self._promote(new, st)
                self._same_retries_left = keep
                self._emit("transient_ok", "Mesma fonte religada com sucesso", source=st.cfg.display())
            else:
                self.active = None
                self._begin_failover(st, "; ".join(v.reasons), "falha")
            return
        if waited >= grace:
            self._begin_failover(st, "; ".join(v.reasons), "falha")

    def _begin_failover(self, failed: SourceState, reason: str, kind: str):
        cap = self.active
        self.failures += 1
        failed.failures += 1
        failed.mark("down", reason)
        self._emit("failure_detected", reason, source=failed.cfg.display())
        if cap is not None:
            cap.stop("falha: " + reason)
            self.session.upsert_capture(cap.summary())
        if self.watcher:
            self.watcher.stop()
            self.watcher = None
        self.active, self.active_src = None, None
        self._bad_since = None
        t_lost = cap.last_bytes_change if cap is not None and cap.bytes else time.time()
        self._ctx = {"failed": failed, "reason": reason, "t": time.time(), "t_lost": t_lost, "kind": kind}
        self.state = "trocando"
        self.session.save_meta()

    def _content_switch(self, reason: str):
        """Congelado/preto: só troca se houver fonte alternativa com movimento (mantém gravando enquanto testa)."""
        cur = self.active_src
        others = [s for s in self._ordered() if s is not cur]
        for st in others:
            if self._stop_evt.is_set():
                return
            cap = self._start_validated(st, hot=False, require_motion=True)
            if cap:
                cap.stop("sonda de teste")
                self._purge_probe(cap)
                self._emit("content_switch", f"Alternativa saudável: {st.cfg.display()}", source=cur.cfg.display())
                self._begin_failover(cur, reason, "conteudo")
                return
        self._emit("content_problem", f"{reason} — nenhuma alternativa com movimento; mantendo a fonte atual",
                   source=cur.cfg.display())

    # ------------------------------------------------------------ retorno à principal
    def _maybe_return(self, now: float):
        st = self.st
        if not st.return_to_primary or self.active is None:
            return
        if self._ret_probe:
            self._advance_return_probe(now)
            return
        if self._ret_ready:
            target, resolved = self._ret_ready
            self._ret_ready = None
            self._launch_return_probe(target, resolved)
            return
        if self._ret_starting:
            return
        order = self._ordered()
        if not order or order[0] is self.active_src:
            self._return_attempts = 0
            return
        if self._return_attempts >= st.max_return_attempts:
            return
        if now - self.last_switch_t < st.cooldown_seconds or now < self._next_return_probe:
            return
        target = order[0]
        self._ret_starting = True
        threading.Thread(target=self._resolve_for_return, args=(target,), daemon=True).start()

    def _resolve_for_return(self, target: SourceState):
        """Thread auxiliar: só resolve a URL (pode demorar em páginas). O FFmpeg é iniciado pela thread
        do gravador, porque no Linux o PDEATHSIG vale para a *thread* que criou o processo."""
        try:
            target.mark("testing")
            try:
                resolved = sources.resolve(target.cfg, self.st.quality, self.profile_dir)
            except Exception as e:
                target.mark("down", str(e))
                self._return_failed(f"não resolveu: {e}")
                self._ret_starting = False
                return
            self._ret_ready = (target, resolved)
        except Exception:
            self._ret_starting = False
            raise

    def _launch_return_probe(self, target: SourceState, resolved):
        try:
            cid = self.session.next_capture_id()
            cap = Capture(cid, target.cfg, resolved, self.session.probe_dir, self.st, staging=True)
            cap.start()
            self._emit("return_attempt", f"Testando retorno à fonte principal (tentativa {self._return_attempts + 1}"
                       f"/{self.st.max_return_attempts})", source=target.cfg.display())
            self._ret_probe = (target, cap, HealthMonitor(self.st), None, False)
        except Exception as e:
            target.mark("down", str(e))
            self._return_failed(str(e))
        finally:
            self._ret_starting = False

    def _return_failed(self, why: str):
        self._return_attempts += 1
        self._next_return_probe = time.time() + self.st.primary_probe_interval

    def _advance_return_probe(self, now: float):
        target, cap, mon, t_ok, announced = self._ret_probe
        v = mon.evaluate(cap, now)
        if v.level == "bad":
            cap.stop("sonda reprovada")
            self._purge_probe(cap)
            self._ret_probe = None
            target.mark("down", "; ".join(v.reasons))
            self._emit("return_fail", "Principal ainda instável: " + "; ".join(v.reasons), source=target.cfg.display())
            self._return_failed("; ".join(v.reasons))
            return
        if v.level == "ok":
            if t_ok is None:
                t_ok = now
            if not announced:
                announced = True
                target.mark("ok", "em teste de estabilidade")
                self._emit("primary_back", "Fonte principal voltou; verificando estabilidade", source=target.cfg.display())
            if now - t_ok >= self.st.primary_stable_seconds:
                ok, why = probe.check_motion(cap, self.st)
                cap.stop("sonda concluída")
                self._purge_probe(cap)
                self._ret_probe = None
                if not ok:
                    target.mark("down", why)
                    self._return_failed(why)
                    return
                self._do_return(target)
                return
        else:
            t_ok = None  # instabilidade reinicia a contagem de estabilidade
        self._ret_probe = (target, cap, mon, t_ok, announced)

    def _do_return(self, target: SourceState):
        cur, cap = self.active_src, self.active
        cap.stop("retornando à principal")
        self.session.upsert_capture(cap.summary())
        if self.watcher:
            self.watcher.stop()
            self.watcher = None
        self.active, self.active_src = None, None
        self._ctx = {"failed": cur, "reason": "retorno à fonte principal estável", "t": time.time(),
                     "t_lost": cap.last_bytes_change, "kind": "retorno"}
        self.state = "trocando"
        new = self._start_validated(target, hot=True)
        if new:
            self._promote(new, target)
        # se falhar, o laço principal chama _acquire() (que tenta todas, incluindo a anterior)

    # ------------------------------------------------------------ disco
    def _check_disk(self):
        free = util.free_bytes(self.session.root) / 1e9
        now = time.time()
        if free < self.st.disk_critical_gb:
            self._emit("disk_critical", f"Espaço livre crítico: {free:.2f} GB. Finalizando para preservar a gravação.")
            self.notifier.notify("disk", "Disco quase cheio — gravação encerrada",
                                 f"{free:.2f} GB livres; segmentos preservados", force=True)
            self.stop_reason = "espaço em disco insuficiente"
            self._stop_evt.set()
        elif free < self.st.disk_warn_gb and now - self._disk_warned_at > 600:
            self._disk_warned_at = now
            self._emit("disk_low", f"Pouco espaço em disco: {free:.1f} GB livres")
            self.notifier.notify("disk", "Pouco espaço em disco", f"{free:.1f} GB livres", force=True)

    # ------------------------------------------------------------ encerramento
    def _shutdown(self):
        self.state = "finalizando"
        try:
            if self._ret_probe:
                self._ret_probe[1].stop("encerrando")
                self._purge_probe(self._ret_probe[1])
                self._ret_probe = None
            if self.watcher:
                self.watcher.stop()
            if self.active:
                self.active.stop("gravação encerrada")
                self.session.upsert_capture(self.active.summary())
            shutil.rmtree(self.session.probe_dir, ignore_errors=True)
            self.session.meta["ended"] = util.now_iso()
            self.session.meta["status"] = "finished"
            self.session.meta["stop_reason"] = self.stop_reason or "concluída"
            self.session.save_meta()
            if self.st.auto_merge:
                self._emit("merge_start", "Unindo segmentos…")
                try:
                    self.merge_result = merge.finalize(self.session.root, self.session.meta.get("name", "gravacao"), self.st)
                except Exception as e:
                    log.exception("falha na junção")
                    self.merge_result = {"ok": False, "messages": [f"erro na junção: {e!r}"], "mode": "erro"}
                self._emit("merge_result", "; ".join(self.merge_result.get("messages", [])), ok=self.merge_result.get("ok"))
                self.session.meta["merge"] = self.merge_result
                self.session.save_meta()
            report.write(self.session, self.merge_result)
            self._emit("session_end", f"Gravação finalizada ({self.stop_reason or 'concluída'})")
            self.notifier.notify("end", "Gravação finalizada",
                                 f"{self.session.meta.get('name')} — {util.fmt_dur(self.recorded_seconds())} gravados",
                                 force=True)
        except Exception:
            log.exception("erro ao finalizar")
        finally:
            self.session.release_lock()
            self.state = "finalizada"

    # ------------------------------------------------------------ dados para a interface
    def recorded_seconds(self) -> float:
        total = 0.0
        act = self.active.cid if self.active else None
        for c in self.session.meta.get("captures", []):
            if not c.get("staging") and c.get("id") != act:
                total += c.get("out_seconds", 0)
        if self.active:
            total += self.active.out_time
        return total

    def snapshot(self) -> dict:
        now = time.time()
        if now - self._bytes_cache[0] > 4:
            self._bytes_cache = (now, util.dir_size(self.session.segments_dir))
        with self._src_lock:
            srcs = list(self.sources)
        out_sources = []
        for s in srcs:
            status = s.status
            if s is self.active_src and self.state == "gravando":
                status = "recording" if s.status == "ok" else s.status
            out_sources.append({"id": s.cfg.id, "label": s.cfg.display(), "url": s.cfg.url, "enabled": s.cfg.enabled,
                                "status": status, "reason": s.reason, "failures": s.failures,
                                "last_check_s": round(now - s.last_check, 1) if s.last_check else None,
                                "active": s is self.active_src, "info": s.info})
        return {"name": self.session.meta.get("name"), "state": self.state, "running": self.running,
                "active_source": self.active_src.cfg.display() if self.active_src else None,
                "sources": out_sources, "recorded_seconds": round(self.recorded_seconds(), 1),
                "elapsed": round(now - self.t_start, 1) if self.t_start else 0,
                "bytes": self._bytes_cache[1], "failures": self.failures, "switches": self.switches[-20:],
                "disk_free_gb": round(util.free_bytes(self.session.root) / 1e9, 1),
                "verdict": {"level": self.verdict.level, "reasons": self.verdict.reasons},
                "notifications": [n["text"] for n in list(self.notifier.history)[-12:]],
                "session_dir": str(self.session.root), "stop_reason": self.stop_reason,
                "merge": self.merge_result,
                "scheduled_at": self.scheduled_at}
