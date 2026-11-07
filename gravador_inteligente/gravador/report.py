"""Relatório final da gravação (Markdown + JSON)."""
from __future__ import annotations

import json
from datetime import datetime

from . import util
from .session import Session

ERR_TYPES = {"failure_detected", "all_down", "disk_critical", "disk_low", "error", "probe_fail",
             "capture_failed", "resolve_failed", "content_problem"}


def build(session: Session, merge_result: dict | None = None) -> dict:
    events = session.read_events()
    meta = session.meta
    caps = [c for c in meta.get("captures", []) if not c.get("staging")]
    caps.sort(key=lambda c: c.get("id", 0))

    # lacunas entre capturas consecutivas (último dado → primeiro dado)
    gaps = []
    with_data = [c for c in caps if c.get("first_data_t") and c.get("last_data_t") and c.get("bytes")]
    for a, b in zip(with_data, with_data[1:]):
        if a.get("last_data_t") and b.get("first_data_t"):
            gaps.append({"de": a["source"], "para": b["source"],
                         "inicio": datetime.fromtimestamp(a["last_data_t"]).astimezone().isoformat(timespec="seconds"),
                         "segundos": round(max(0.0, b["first_data_t"] - a["last_data_t"]), 1)})

    failures = [e for e in events if e["type"] == "failure_detected"]
    switches = [e for e in events if e["type"] == "switch"]
    interruptions = []
    for f in failures:
        nxt = next((e for e in events if e["type"] in ("switch", "recovered") and e["t"] >= f["t"]), None)
        secs = None
        if nxt:
            secs = nxt.get("interrupcao_s")
            if secs is None:
                secs = round(nxt["t"] - f["t"], 1)
        interruptions.append({"inicio": f["ts"], "fonte": f.get("source"), "motivo": f.get("message"),
                              "ate": nxt["ts"] if nxt else None, "segundos": secs})
    order, seen = [], set()
    for c in with_data:
        if c["source"] not in seen or (order and order[-1] != c["source"]):
            order.append(c["source"])
            seen.add(c["source"])
    problems = [e for e in events if e["type"] in ERR_TYPES]
    total = sum(c.get("out_seconds", 0) for c in caps)
    if merge_result and merge_result.get("total_seconds"):
        total = max(total, merge_result["total_seconds"]) if not caps else total
    return {
        "nome": meta.get("name"), "inicio": meta.get("started"), "fim": meta.get("ended"),
        "status": meta.get("status"), "duracao_gravada_s": round(total, 1),
        "fontes_em_ordem": order, "falhas": len(failures), "trocas": len(switches),
        "interrupcoes": interruptions, "lacunas_entre_fontes": gaps,
        "capturas": caps, "avisos": [{"ts": e["ts"], "tipo": e["type"], "msg": e.get("message", "")} for e in problems],
        "juncao": merge_result or {},
    }


def render_md(r: dict) -> str:
    L = [f"# Relatório da gravação — {r['nome']}", "",
         f"- **Início:** {r['inicio']}", f"- **Término:** {r['fim'] or '(não finalizada normalmente)'}",
         f"- **Duração gravada (mídia):** {util.fmt_dur(r['duracao_gravada_s'])}",
         f"- **Falhas detectadas:** {r['falhas']}  |  **Trocas de fonte:** {r['trocas']}",
         f"- **Fontes utilizadas (ordem):** {' → '.join(r['fontes_em_ordem']) or '—'}", ""]
    L += ["## Capturas (segmentos criados)", "", "| # | Fonte | Início | Mídia | Segmentos | Fim |", "|---|---|---|---|---|---|"]
    for c in r["capturas"]:
        L.append(f"| {c['id']} | {c['source']} | {c.get('started') or ''} | {util.fmt_dur(c.get('out_seconds'))} | "
                 f"{c.get('segments', 0)} | {c.get('end_reason') or ''} |")
    L += ["", "## Interrupções (falha → recuperação)", ""]
    if r["interrupcoes"]:
        for i in r["interrupcoes"]:
            dur = f"{i['segundos']}s" if i["segundos"] is not None else "sem recuperação"
            L.append(f"- {i['inicio']} — **{i['fonte']}**: {i['motivo']} → {dur}")
    else:
        L.append("- Nenhuma.")
    L += ["", "## Lacunas de tempo entre fontes (estimadas: último dado → primeiro dado)", ""]
    if r["lacunas_entre_fontes"]:
        for g in r["lacunas_entre_fontes"]:
            L.append(f"- {g['inicio']}: {g['de']} → {g['para']}: **{g['segundos']}s** sem gravação")
    else:
        L.append("- Nenhuma.")
    L += ["", "## Erros e avisos", ""]
    L += [f"- {a['ts']} [{a['tipo']}] {a['msg']}" for a in r["avisos"]] or ["- Nenhum."]
    m = r.get("juncao") or {}
    L += ["", "## Junção dos segmentos", ""]
    if m:
        L += [f"- Resultado: **{'sucesso' if m.get('ok') else 'não unificado'}** (modo: {m.get('mode')})",
              f"- Segmentos: {m.get('valid')}/{m.get('segments_total')} válidos; {util.fmt_dur(m.get('total_seconds'))} de vídeo",
              f"- Arquivos gerados: {', '.join(m.get('outputs') or []) or '—'}"]
        L += [f"- Grupo: {g['signature']} — {g['segments']} segmentos, {util.fmt_dur(g['seconds'])}" for g in m.get("groups", [])]
        L += [f"- {x}" for x in m.get("messages", [])]
        L += [f"- Segmento inválido (preservado): {x['file']} ({x['reason']})" for x in m.get("invalid", [])]
    else:
        L.append("- Não executada.")
    return "\n".join(L) + "\n"


def write(session: Session, merge_result: dict | None = None) -> dict:
    r = build(session, merge_result)
    (session.root / "relatorio.md").write_text(render_md(r), encoding="utf-8")
    util.atomic_write_json(session.root / "relatorio.json", r)
    return r
