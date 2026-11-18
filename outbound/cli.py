"""Command-line entry point.

Examples (PowerShell, from the project folder, with .venv active):
  python -m outbound.cli --only rocketlane.com --steps 1-2        # scrape + extract one domain
  python -m outbound.cli --steps 1-3                              # qualify everything in data\\domains.txt
  python -m outbound.cli --steps 3 --only spotdraft.com           # re-run qualify on the latest run's data
  python -m outbound.cli                                           # full pipeline, dry-run (nothing sent)
  python -m outbound.cli --only spotdraft.com --send               # really send (needs SEND_ENABLED=true)
  python -m outbound.cli --no-hunter                               # never spend Hunter credits (cache only)
"""

from __future__ import annotations

import argparse
import logging
import sys

from rich.console import Console
from rich.table import Table

from outbound import PROJECT_ROOT
from outbound.config import ConfigError, get_settings
from outbound.domains import parse_domains, read_domains
from outbound.llm import LLMFatalError
from outbound.logging_setup import setup_logging
from outbound.pipeline import STEP_TITLES, RunOptions, StepOutcome, find_latest_run, new_run_id, parse_steps, run_domain
from outbound.report import summary_rows, write_reports
from outbound.storage import FileStore, get_store

console = Console()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m outbound.cli", description="Sprinto outbound pipeline (dry-run by default).")
    p.add_argument("--domains", default=str(PROJECT_ROOT / "data" / "domains.txt"), help="file with one domain per line")
    p.add_argument("--only", action="append", default=[], help="run just this domain (can repeat)")
    p.add_argument("--steps", default="1-7", help="which steps, e.g. 1-2, 3, 1-7 (default 1-7)")
    p.add_argument("--run", help="continue this run id (default: new run, or the latest run when starting after step 1)")
    p.add_argument("--send", action="store_true", help="really send emails (also needs SEND_ENABLED=true in .env)")
    p.add_argument("--no-hunter", action="store_true", help="never call Hunter; use cached contacts only")
    p.add_argument("--fresh", action="store_true", help="ignore the scrape cache and re-download pages")
    p.add_argument("--local", action="store_true", help="save to runs/ files even if DATABASE_URL is set")
    p.add_argument("-v", "--verbose", action="store_true", help="more detailed logs")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(logging.DEBUG if args.verbose else logging.INFO)
    settings = get_settings()
    try:
        steps = parse_steps(args.steps)
        domains = parse_domains("\n".join(args.only)) if args.only else read_domains(args.domains)
        if not domains:
            console.print("[red]No domains to run.[/red]")
            return 1
        if any(n >= 2 for n in steps):
            settings.require("gemini")
        if 4 in steps and not args.no_hunter:
            settings.require("hunter")
    except (ValueError, FileNotFoundError, ConfigError) as exc:
        console.print(f"[red]{exc}[/red]")
        return 1

    store = FileStore() if args.local else get_store(settings)
    options = RunOptions(send=args.send, no_hunter=args.no_hunter, use_cache=not args.fresh, skip_done=bool(args.run))
    if args.send and not settings.send_enabled:
        console.print("[yellow]--send was given but SEND_ENABLED is not true in .env, so this stays a dry run.[/yellow]")

    run_ids: dict[str, str] = {}
    for d in domains:
        if args.run:
            run_ids[d] = args.run
        elif steps[0] > 1:
            found = find_latest_run(store, d, steps[0] - 1)
            if not found:
                console.print(f"[yellow]{d}: no earlier run has step {steps[0] - 1} done; run from step 1 first.[/yellow]")
                continue
            run_ids[d] = found
    default_run = args.run or new_run_id()
    console.print(f"[bold]Run[/bold] {default_run if not run_ids else ', '.join(sorted(set(run_ids.values())))} · "
                  f"steps {steps[0]}–{steps[-1]} · {len(domains)} domain(s) · {'SEND' if args.send else 'dry-run'} · store: {type(store).__name__}")

    def on_step(domain: str, outcome: StepOutcome) -> None:
        colour = {"done": "green", "stopped": "yellow", "error": "red"}[outcome.status]
        console.print(f"  [{colour}]{domain} · {outcome.step} {STEP_TITLES[outcome.step]} · {outcome.status}[/{colour}] {outcome.message[:160]}")

    used_runs = set()
    fatal: str | None = None
    for d in domains:
        if steps[0] > 1 and d not in run_ids:
            continue
        run_id = run_ids.get(d, default_run)
        used_runs.add(run_id)
        console.rule(d)
        try:
            run_domain(run_id, d, steps, settings, store, options, on_step)
        except LLMFatalError as exc:
            fatal = str(exc)
            console.print(f"\n[bold red]Stopping the run: {exc}[/bold red]")
            break

    for run_id in sorted(used_runs):
        write_reports(run_id, store)
        print_summary(run_id, store, [d for d in domains if run_ids.get(d, default_run) == run_id])
        if isinstance(store, FileStore):
            console.print(f"Artifacts: {store.runs_dir / run_id}")
        else:
            console.print(f"Artifacts saved in Neon under run id {run_id}")
    if fatal:
        console.print(f"[bold red]Not finished: {fatal}[/bold red]\nAfter fixing it, re-run the same command and add "
                      f"--run {sorted(used_runs)[-1]} to fill in the same run (scraped pages are cached).")
        return 2
    return 0


def print_summary(run_id: str, store, domains: list[str]) -> None:
    table = Table(title=f"Run {run_id}", show_lines=False)
    for col in ("Domain", "Verdict", "Score", "Top 2 reasons", "PoC", "Email", "Send"):
        table.add_column(col, overflow="fold")
    for r in summary_rows(store.load_run(run_id)):
        if r["domain"] not in domains:
            continue
        table.add_row(r["domain"], str(r["verdict"] or r["status"]), str(r["score"]), (r["top_reasons"] or r["disqualifier"])[:160],
                      f"{r['poc_name']} ({r['poc_title']})" if r["poc_name"] else "", r["email_subject"] + (" ⚠ flagged" if r["email_flagged"] is True else ""),
                      str(r["send_status"]))
    console.print(table)


if __name__ == "__main__":
    sys.exit(main())
