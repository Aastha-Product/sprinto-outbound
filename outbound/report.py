"""Builds summary.csv and decision_report.md for a run from its saved artifacts.

The report answers, per domain: why qualified / rejected (score table + evidence links),
why this person (vs the alternatives), and why this email (hook, pain point, sources).
"""

from __future__ import annotations

import csv
import io

from outbound.pipeline import STEP_FILES
from outbound.storage import Store

SUMMARY_COLUMNS = ["domain", "last_step", "status", "verdict", "score", "disqualifier", "top_reasons", "poc_name", "poc_title",
                   "email_subject", "email_flagged", "send_status", "llm_calls", "hunter_credits"]


def _last(arts: dict) -> tuple[str, dict]:
    for name in reversed(list(STEP_FILES.values())):
        if name in arts:
            return name, arts[name]
    return "", {}


def summary_rows(run: dict[str, dict[str, dict]]) -> list[dict]:
    rows = []
    for domain, arts in run.items():
        last_name, last = _last(arts)
        q = (arts.get("03_qualify") or {}).get("output") or {}
        poc = ((arts.get("04_poc") or {}).get("output") or {})
        chosen = poc.get("chosen") or {}
        draft = (arts.get("06_email") or {}).get("output") or {}
        send = (arts.get("07_send") or {}).get("output") or {}
        rows.append({
            "domain": domain,
            "last_step": last_name,
            "status": last.get("status", ""),
            "verdict": q.get("verdict", ""),
            "score": q.get("score", ""),
            "disqualifier": q.get("disqualifier") or "",
            "top_reasons": " | ".join(q.get("top_reasons", [])),
            "poc_name": " ".join(p for p in (chosen.get("first_name"), chosen.get("last_name")) if p),
            "poc_title": chosen.get("position") or "",
            "email_subject": (draft.get("draft") or {}).get("subject", ""),
            "email_flagged": draft.get("flagged", ""),
            "send_status": send.get("status", ""),
            "llm_calls": last.get("llm_calls_used", ""),
            "hunter_credits": poc.get("hunter_credits_used", ""),
        })
    return rows


def summary_csv(run: dict[str, dict[str, dict]]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=SUMMARY_COLUMNS)
    writer.writeheader()
    writer.writerows(summary_rows(run))
    return buf.getvalue()


def _link(url: str) -> str:
    return f"[{url}]({url})" if url.startswith("http") else f"`{url}`"


def _evidence_lines(evidence: list[dict], limit: int = 6) -> list[str]:
    return [f"  - {e['claim']}: \"{e['quote']}\" ({_link(e['source_url'])})" for e in evidence[:limit]]


def domain_section(domain: str, arts: dict) -> str:
    out = [f"## {domain}"]
    scrape = (arts.get("01_scrape") or {})
    out.append(f"**Scrape:** {scrape.get('reasoning', 'not run')}")
    q_art = arts.get("03_qualify")
    if not q_art:
        out.append(f"**Stopped before qualification:** {_last(arts)[1].get('reasoning', '')}")
        return "\n\n".join(out)
    q = q_art.get("output") or {}
    if not q:
        out.append(f"**Qualification error:** {q_art.get('reasoning')}")
        return "\n\n".join(out)

    out.append(f"### Why {q['verdict']} (score {q['score']}/100)\n{q.get('reasoning', '')}")
    if q.get("score_breakdown"):
        table = ["| Rule | Row | Points | Max | Reason |", "|---|---|---|---|---|"]
        for ln in q["score_breakdown"]:
            if ln["points"] or ln["reason"] not in ("unknown", "no"):
                table.append(f"| {ln['rule_id']} | {ln['rubric_row']} | {ln['points']} | {ln['max_points']} | {ln['reason'].replace('|', '/')} |")
        out.append("\n".join(table))
    if q.get("evidence"):
        out.append("**Evidence:**\n" + "\n".join(_evidence_lines(q["evidence"], 10)))
    if q.get("iterations"):
        its = [f"{i['n']}. `{i['action']['action']}` {i['action']['argument']}: {i['action']['why']} → {i['learned'][:200]} (score {i['score_before']} → {i['score_after']})"
               for i in q["iterations"]]
        out.append("**Agent loop:**\n" + "\n".join(its))

    poc_art = arts.get("04_poc")
    if poc_art and poc_art.get("output"):
        out.append(f"### Why this person\n{poc_art['output'].get('reasoning', '')}")
    pain_art = arts.get("05_pain")
    if pain_art and pain_art.get("output"):
        lines = []
        for p in pain_art["output"]:
            lines.append(f"- **{p['title']}**: {p['why_this_person']}")
            lines += _evidence_lines(p.get("evidence", []), 2)
        out.append("### Pain points\n" + "\n".join(lines))
    email_art = arts.get("06_email")
    if email_art and email_art.get("output"):
        d = email_art["output"]
        out.append(f"### Why this email\n{email_art.get('reasoning', '')}\n\n**Subject:** {d['draft']['subject']}\n\n```\n{d['full_body']}\n```")
    send_art = arts.get("07_send")
    if send_art and send_art.get("output"):
        s = send_art["output"]
        out.append(f"### Send\n`{s['status']}`: {s['reason']}")
    return "\n\n".join(out)


def decision_report(run_id: str, run: dict[str, dict[str, dict]]) -> str:
    rows = summary_rows(run)
    head = [f"# Decision report: run {run_id}", "", "| Domain | Verdict | Score | Top reasons | PoC | Send |", "|---|---|---|---|---|---|"]
    for r in rows:
        head.append(f"| {r['domain']} | {r['verdict'] or r['status']} | {r['score']} | {r['top_reasons'].replace('|', '/') or r['disqualifier']} | {r['poc_name']} {('(' + r['poc_title'] + ')') if r['poc_title'] else ''} | {r['send_status']} |")
    sections = [domain_section(d, arts) for d, arts in run.items()]
    return "\n".join(head) + "\n\n" + "\n\n---\n\n".join(sections) + "\n"


def write_reports(run_id: str, store: Store) -> tuple[str, str]:
    """Build both reports from the store and save them as run files. Returns (csv, markdown)."""
    run = store.load_run(run_id)
    csv_text, md = summary_csv(run), decision_report(run_id, run)
    store.save_run_file(run_id, "summary.csv", csv_text)
    store.save_run_file(run_id, "decision_report.md", md)
    return csv_text, md
