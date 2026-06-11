"""The human gate: review queue persistence + Rich approval TUI.

Decisions persist to out/queue.json immediately (quit-safe). Approved drafts
are written to out/sent_drafts/ -- visibly drafted, never transmitted.
Re-runs merge by discrepancy fingerprint so dismissed items stay dismissed.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console, Group
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Prompt
from rich.table import Table
from rich.text import Text

from auditor.models import (
    Decision,
    Discrepancy,
    QueueItem,
    SEVERITY_COLORS,
    Severity,
    TriageResult,
)
from auditor.theme import GREY_LINE, PANEL_BOX, make_console, two_tone

QUEUE_FILE = "queue.json"
DRAFTS_DIR = "sent_drafts"


# ---------------------------------------------------------------- persistence

def resolve_channel(d: Discrepancy, pipeline, rules) -> str:
    """Slack channel for a discrepancy: the candidate's role channel; for
    tracking-only ghosts, the channel the entries were logged in."""
    for party in d.candidates_involved:
        candidate = pipeline.candidates.get(party)
        if candidate is not None:
            slug = candidate.role.lower().replace(" ", "-")
            return rules.slack.channel_template.format(role=slug)
        for e in pipeline.entries:
            if e.entry.candidate_name == party and e.entry.channel:
                return e.entry.channel
    return rules.slack.default_channel


def build_queue_items(discrepancies: list[Discrepancy],
                      triages: list[TriageResult],
                      pipeline=None, rules=None) -> list[QueueItem]:
    return [QueueItem(fingerprint=d.fingerprint, discrepancy=d, triage=t,
                      slack_channel=(resolve_channel(d, pipeline, rules)
                                     if pipeline is not None and rules is not None
                                     else None))
            for d, t in zip(discrepancies, triages)]


def load_queue(out_dir: str | Path) -> list[QueueItem]:
    path = Path(out_dir) / QUEUE_FILE
    if not path.exists():
        return []
    return [QueueItem.model_validate(item)
            for item in json.loads(path.read_text())]


def save_queue(items: list[QueueItem], out_dir: str | Path) -> None:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = [item.model_dump(mode="json") for item in items]
    (out_dir / QUEUE_FILE).write_text(json.dumps(payload, indent=2) + "\n")


def merge_queue(new_items: list[QueueItem],
                existing: list[QueueItem]) -> list[QueueItem]:
    """Fresh detections win on content; prior human decisions win on state."""
    decisions = {e.fingerprint: e for e in existing
                 if e.decision != Decision.PENDING}
    merged = []
    for item in new_items:
        prior = decisions.get(item.fingerprint)
        if prior is not None:
            item = item.model_copy(update={"decision": prior.decision,
                                           "decided_at": prior.decided_at})
        merged.append(item)
    return merged


def write_draft(item: QueueItem, out_dir: str | Path) -> Path:
    drafts = Path(out_dir) / DRAFTS_DIR
    drafts.mkdir(parents=True, exist_ok=True)
    path = drafts / f"{item.fingerprint}.txt"
    fix = item.triage.proposed_fix
    path.write_text(
        f"DRAFT ({fix.type.value}) -- approved by a human, NOT transmitted\n"
        f"discrepancy: {item.discrepancy.summary}\n"
        f"---\n{fix.content}\n")
    return path


# ----------------------------------------------------------------- rendering

def severity_badge(severity: Severity) -> Text:
    return Text(f" {severity.value.upper()} ",
                style=f"bold {SEVERITY_COLORS[severity]} reverse")


def queue_header(items: list[QueueItem], position: int) -> Text:
    pending = [i for i in items if i.decision == Decision.PENDING]
    counts = {s: sum(1 for i in pending if i.triage.severity == s)
              for s in Severity}
    header = two_tone("Review queue.", f"item {position} of {len(items)}")
    header.append("   ")
    for sev in (Severity.URGENT, Severity.ATTENTION, Severity.LAG,
                Severity.INSUFFICIENT_EVIDENCE):
        if counts[sev]:
            header.append(f"{sev.value} {counts[sev]}   ",
                          style=SEVERITY_COLORS[sev])
    return header


def evidence_table(item: QueueItem) -> Table:
    table = Table(box=None, expand=True, pad_edge=False,
                  header_style="stat.label")
    table.add_column("source", style="grey58", no_wrap=True)
    table.add_column("ref", style="bright_white")
    table.add_column("value", style="bright_white")
    table.add_column("timestamp", style="grey58")
    for ev in item.discrepancy.evidence:
        table.add_row(ev.source, ev.ref, ev.value,
                      ev.timestamp.isoformat() if ev.timestamp else "-")
    return table


def render_item(items: list[QueueItem], index: int) -> Group:
    item = items[index]
    d, t = item.discrepancy, item.triage
    summary = Text()
    summary.append(f"{d.type.value}  ", style="bold bright_white")
    summary.append_text(severity_badge(t.severity))
    summary.append(f"\ncandidates: {', '.join(d.candidates_involved)}\n",
                   style="grey58")
    summary.append(t.explanation)
    trace = Text("investigation: " + (" -> ".join(t.investigation_trace) or "(none)"),
                 style="caption")
    return Group(
        queue_header(items, index + 1),
        Panel(summary, title="discrepancy", title_align="left",
              box=PANEL_BOX, border_style=GREY_LINE),
        Panel(evidence_table(item), title="evidence", title_align="left",
              box=PANEL_BOX, border_style=GREY_LINE),
        trace,
        Panel(Markdown(t.proposed_fix.content),
              title=f"drafted fix ({t.proposed_fix.type.value})",
              title_align="left", box=PANEL_BOX,
              border_style=SEVERITY_COLORS[t.severity]),
    )


def _post_approved(item: QueueItem, console: Console) -> None:
    """Send the just-approved draft to Slack. Failure never blocks the
    decision -- the draft file above is always the local audit trail."""
    from auditor import slack_push

    if not slack_push.is_configured():
        console.print(Text("Slack not configured -- draft saved locally only",
                           style="caption"))
        return
    from auditor.config import load_rules
    try:
        fallback = load_rules().slack.default_channel
        channel = slack_push.post_draft(item, fallback_channel=fallback)
        console.print(f"[ok]posted to {channel}[/]")
    except Exception as error:  # noqa: BLE001 -- network/API errors vary
        console.print(f"[#e5484d]Slack post failed:[/] {error}")


# -------------------------------------------------------------- review loop

def review_queue(out_dir: str | Path, console: Console | None = None) -> None:
    """One screen per pending item: [a]pprove / [d]ismiss / [s]kip / [q]uit."""
    console = console or make_console()
    items = load_queue(out_dir)
    if not items:
        console.print("[yellow]queue is empty -- run `python audit.py run` first[/]")
        return
    pending_indexes = [i for i, item in enumerate(items)
                       if item.decision == Decision.PENDING]
    if not pending_indexes:
        console.print("[green]nothing pending -- queue fully reviewed[/]")
        return

    for index in pending_indexes:
        console.clear()
        console.print(render_item(items, index))
        choice = Prompt.ask("[a]pprove  [d]ismiss  [s]kip  [q]uit",
                            choices=["a", "d", "s", "q"], default="s",
                            console=console)
        if choice == "q":
            break
        if choice == "s":
            continue  # skipped items stay pending for the next session
        decision = Decision.APPROVED if choice == "a" else Decision.DISMISSED
        items[index] = items[index].model_copy(update={
            "decision": decision,
            "decided_at": datetime.now(timezone.utc)})
        if decision == Decision.APPROVED:
            path = write_draft(items[index], out_dir)
            console.print(f"[ok]draft written to {path}[/]")
            _post_approved(items[index], console)
        save_queue(items, out_dir)  # persist every decision; quitting is safe

    done = sum(1 for i in items if i.decision != Decision.PENDING)
    console.print(f"\n[bold]{done}/{len(items)} decided[/] -- decisions saved "
                  f"to {Path(out_dir) / QUEUE_FILE}")
