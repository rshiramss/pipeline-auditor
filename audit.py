"""Pipeline auditor entry point.

  python audit.py run [--no-llm]        detect drift, triage, fill the queue
  python audit.py review                approve/dismiss drafted fixes (TUI)
  python audit.py report [--push-notion]  write report.md + digest.txt
  python audit.py tui                   run the whole loop in one TUI session
"""

from __future__ import annotations

import argparse
import os
import sys

from rich.console import Console
from rich.table import Table
from rich import box

from auditor.config import load_dotenv, load_rules
from auditor.models import SEVERITY_COLORS

load_dotenv()
console = Console()


def cmd_run(args: argparse.Namespace) -> None:
    from auditor.run import run_audit
    from auditor.triage import build_model

    rules = load_rules(args.rules)
    use_llm = not args.no_llm
    if use_llm and build_model(rules) is None:
        console.print("[yellow]ANTHROPIC_API_KEY not set -- falling back to "
                      "offline triage (severity from rules.yaml)[/]")
        use_llm = False

    with console.status("[bold]auditing...[/]") as status:
        def on_progress(d):
            status.update(f"[bold]triaging[/] {d.type.value}: {d.summary[:60]}")
        items, discrepancies = run_audit(rules, args.data, args.out,
                                         use_llm=use_llm,
                                         on_triage_progress=on_progress)

    table = Table(title=f"{len(discrepancies)} discrepancies", box=box.SIMPLE)
    table.add_column("severity")
    table.add_column("type")
    table.add_column("summary", overflow="fold")
    for item in items:
        sev = item.triage.severity
        table.add_row(f"[{SEVERITY_COLORS[sev]}]{sev.value}[/]",
                      item.discrepancy.type.value, item.discrepancy.summary)
    console.print(table)
    console.print(f"queue written to {args.out}/queue.json -- "
                  f"next: [bold]python audit.py review[/]")


def cmd_review(args: argparse.Namespace) -> None:
    from auditor.queue import review_queue
    review_queue(args.out, console)


def cmd_report(args: argparse.Namespace) -> None:
    from auditor.normalize import load_pipeline
    from auditor.report import write_reports

    rules = load_rules(args.rules)
    pipeline = load_pipeline(args.data, rules.duplicate_threshold)
    report_path, digest_path = write_reports(pipeline, args.data, args.out)
    console.print(f"[green]wrote {report_path} and {digest_path}[/]")
    if args.push_notion:
        from auditor.notion_push import push_report
        url = push_report(pipeline, args.out)
        console.print(f"[green]pushed to Notion:[/] {url}")


def cmd_tui(args: argparse.Namespace) -> None:
    from auditor.tui import run_tui
    run_tui(rules_path=args.rules, data_dir=args.data, out_dir=args.out)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="audit",
                                     description="hiring-pipeline drift auditor")
    parser.add_argument("--rules", default="rules.yaml")
    parser.add_argument("--data", default="data")
    parser.add_argument("--out", default="out")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="detect drift and triage into the queue")
    p_run.add_argument("--no-llm", action="store_true",
                       help="offline triage: severity from rules.yaml, no API calls")
    p_run.set_defaults(func=cmd_run)

    p_review = sub.add_parser("review", help="approve/dismiss drafted fixes")
    p_review.set_defaults(func=cmd_review)

    p_report = sub.add_parser("report", help="write report.md + digest.txt")
    p_report.add_argument("--push-notion", action="store_true",
                          help="also publish the report to Notion (NOTION_TOKEN)")
    p_report.set_defaults(func=cmd_report)

    p_tui = sub.add_parser("tui", help="run the full loop in one TUI session")
    p_tui.set_defaults(func=cmd_tui)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
