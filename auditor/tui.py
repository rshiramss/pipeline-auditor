"""Full-loop TUI shell: the entire audit loop in one Rich session.

Orchestration only -- every action calls the same functions the CLI uses.
Flow is render-screen -> keypress+Enter -> next screen (Rich, not Textual:
no mouse, no live widgets, by design).
"""

from __future__ import annotations

import json
from pathlib import Path

from rich.console import Console, Group
from rich.panel import Panel
from rich.progress import Progress
from rich.prompt import IntPrompt, Prompt
from rich.table import Table
from rich.text import Text
from rich import box

from auditor.config import load_rules
from auditor.models import (
    CanonicalPipeline,
    Discrepancy,
    SEVERITY_COLORS,
    Stage,
)
from auditor.normalize import load_pipeline, stage_for_tag
from auditor.queue import load_queue, review_queue
from auditor.run import run_audit

PAGE_SIZE = 10

MENU = """\
[bold][g][/bold] generate synthetic data    [bold][c][/bold] run checks + triage
[bold][i][/bold] inspect mismatches         [bold][v][/bold] review queue
[bold][p][/bold] write report               [bold][q][/bold] quit"""


class TuiState:
    """What the session has loaded so far; refreshed after generate/check."""

    def __init__(self, rules_path: str, data_dir: str, out_dir: str):
        self.rules = load_rules(rules_path)
        self.data_dir = data_dir
        self.out_dir = out_dir
        self.pipeline: CanonicalPipeline | None = None
        self.discrepancies: list[Discrepancy] = []

    def load(self) -> None:
        self.pipeline = load_pipeline(self.data_dir, self.rules.duplicate_threshold)


# ------------------------------------------------------------------ screens

def do_generate(state: TuiState, console: Console) -> None:
    from generate_data import generate

    n = IntPrompt.ask("pool size --n", default=30, console=console)
    seed = IntPrompt.ask("--seed", default=42, console=console)
    with Progress(console=console, transient=True) as progress:
        progress.add_task("generating...", total=None)
        manifest = generate(n, seed, "2026-06-08T09:00:00+00:00",
                            Path(state.data_dir))
    state.load()
    table = Table(title=f"generated n={n} seed={seed}", box=box.SIMPLE)
    table.add_column("planted drift")
    table.add_column("count", justify="right")
    for kind, members in manifest["planted"].items():
        table.add_row(kind, str(len(members)))
    console.print(table)


def do_check(state: TuiState, console: Console) -> None:
    if not Path(state.data_dir, "ashby_export.json").exists():
        console.print("[yellow]no data yet -- generate first[/]")
        return
    import os
    use_llm = bool(os.environ.get("ANTHROPIC_API_KEY"))
    if not use_llm:
        console.print("[dim]ANTHROPIC_API_KEY not set: offline triage[/]")
    with console.status("[bold]checking...[/]") as status:
        def on_progress(d):
            status.update(f"[bold]triaging[/] {d.summary[:60]}")
        items, found = run_audit(state.rules, state.data_dir, state.out_dir,
                                 use_llm=use_llm, on_triage_progress=on_progress)
    state.load()
    state.discrepancies = found
    counts: dict[str, int] = {}
    for d in found:
        counts[d.type.value] = counts.get(d.type.value, 0) + 1
    table = Table(title=f"{len(found)} discrepancies -> queue", box=box.SIMPLE)
    table.add_column("type")
    table.add_column("count", justify="right")
    for kind, count in sorted(counts.items()):
        table.add_row(kind, str(count))
    console.print(table)


def diff_table(d: Discrepancy, pipeline: CanonicalPipeline) -> Table:
    """Side-by-side ATS vs tracking-log view; disagreeing cells in red."""
    table = Table(box=box.ROUNDED, expand=True, title=d.type.value)
    table.add_column("field", style="bold", no_wrap=True)
    table.add_column("ATS (source of truth)")
    table.add_column("tracking log")

    for cid in d.candidates_involved:
        c = pipeline.candidates.get(cid)
        if c is None:  # ghost_in_tracking: involved party is a raw name
            table.add_row("record", Text("-- absent --", style="bold red"),
                          f"entries logged as '{cid}'")
            continue
        entries = pipeline.entries_for(cid)
        latest = max(entries, key=lambda e: e.entry.timestamp) if entries else None
        tracked_stage = (stage_for_tag(latest.entry.stage_tag)
                         if latest else None)

        stage_disagrees = latest is not None and tracked_stage != c.stage
        stage_style = "bold red" if stage_disagrees else "green"
        table.add_row(f"{c.name} ({cid})", "", "", style="dim")
        table.add_row(
            "stage",
            Text(c.stage.value, style=stage_style),
            Text(f"{latest.entry.stage_tag} ({tracked_stage.value if tracked_stage else '?'})"
                 if latest else "-- no entries --",
                 style=stage_style if latest else "bold red"),
        )
        table.add_row(
            "last seen",
            c.last_updated.isoformat(),
            latest.entry.timestamp.isoformat() if latest else "--",
        )
        owner_style = "bold red" if not c.owner else ""
        table.add_row("owner", Text(str(c.owner), style=owner_style), "")
        future = [e for e in c.scheduled_events if e.timestamp > pipeline.as_of]
        table.add_row("next step",
                      Text(f"{future[0].type} @ {future[0].timestamp.isoformat()}"
                           if future else "none scheduled",
                           style="" if future else "bold red"),
                      "")
    return table


def do_inspect(state: TuiState, console: Console) -> None:
    if state.pipeline is None or not state.discrepancies:
        console.print("[yellow]nothing to inspect -- run checks first[/]")
        return
    found = state.discrepancies
    page = 0
    while True:
        console.clear()
        start = page * PAGE_SIZE
        chunk = found[start:start + PAGE_SIZE]
        listing = Table(box=box.SIMPLE,
                        title=f"discrepancies {start + 1}-{start + len(chunk)} of {len(found)}")
        listing.add_column("#", justify="right")
        listing.add_column("type")
        listing.add_column("summary", overflow="fold")
        for offset, d in enumerate(chunk):
            listing.add_row(str(start + offset + 1), d.type.value, d.summary)
        console.print(listing)
        choices = [str(start + i + 1) for i in range(len(chunk))] + ["n", "p", "b"]
        pick = Prompt.ask("number to inspect, [n]ext page, [p]rev page, [b]ack",
                          choices=choices, default="b", console=console,
                          show_choices=False)
        if pick == "b":
            return
        if pick == "n":
            page = min(page + 1, (len(found) - 1) // PAGE_SIZE)
            continue
        if pick == "p":
            page = max(page - 1, 0)
            continue
        d = found[int(pick) - 1]
        console.clear()
        console.print(Panel(d.summary, title="where the systems disagree",
                            box=box.HEAVY))
        console.print(diff_table(d, state.pipeline))
        Prompt.ask("[enter] to go back", default="", show_default=False,
                   console=console)


def do_report(state: TuiState, console: Console) -> None:
    from auditor.report import write_reports

    if state.pipeline is None:
        console.print("[yellow]no data yet -- generate first[/]")
        return
    report_path, digest_path = write_reports(state.pipeline, state.data_dir,
                                             state.out_dir)
    console.print(f"[green]wrote {report_path} and {digest_path}[/]")
    console.print(Panel(digest_path.read_text(), title="digest preview",
                        box=box.ROUNDED))


def run_tui(rules_path: str = "rules.yaml", data_dir: str = "data",
            out_dir: str = "out") -> None:
    console = Console()
    state = TuiState(rules_path, data_dir, out_dir)
    if Path(data_dir, "ashby_export.json").exists():
        state.load()

    while True:
        console.print()
        console.print(Panel(MENU, title="pipeline auditor", box=box.HEAVY))
        status = Text()
        if state.pipeline:
            pending = sum(1 for i in load_queue(out_dir)
                          if i.decision.value == "pending")
            status.append(f"{len(state.pipeline.candidates)} candidates loaded; "
                          f"{len(state.discrepancies)} discrepancies in session; "
                          f"{pending} queue items pending", style="dim")
            console.print(status)
        choice = Prompt.ask("action", choices=["g", "c", "i", "v", "p", "q"],
                            console=console)
        if choice == "q":
            return
        {"g": do_generate, "c": do_check, "i": do_inspect,
         "v": lambda s, c: review_queue(s.out_dir, c),
         "p": do_report}[choice](state, console)
