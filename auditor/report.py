"""Monday report: out/report.md (paste into Notion unedited) + out/digest.txt.

Snapshot = role x stage counts from the ATS export. Movement = diff against
data/prior_week.json (embedded by the generator). Hygiene = decisions from
out/queue.json plus a one-line confidence statement.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from auditor.models import (
    CanonicalPipeline,
    Decision,
    QueueItem,
    Severity,
    Stage,
)
from auditor.queue import load_queue

REPORT_STAGES = [s for s in Stage if s not in (Stage.REJECTED, Stage.WITHDRAWN)]


def snapshot_counts(pipeline: CanonicalPipeline) -> dict[str, Counter]:
    counts: dict[str, Counter] = {}
    for c in pipeline.candidates.values():
        counts.setdefault(c.role, Counter())[c.stage.value] += 1
    return counts


def load_prior_week(data_dir: str | Path) -> dict[str, dict[str, int]]:
    path = Path(data_dir) / "prior_week.json"
    return json.loads(path.read_text()) if path.exists() else {}


def hygiene_stats(items: list[QueueItem]) -> dict[str, int]:
    return {
        "found": len(items),
        "resolved": sum(1 for i in items if i.decision != Decision.PENDING),
        "approved": sum(1 for i in items if i.decision == Decision.APPROVED),
        "dismissed": sum(1 for i in items if i.decision == Decision.DISMISSED),
        "pending": sum(1 for i in items if i.decision == Decision.PENDING),
        "urgent_open": sum(1 for i in items if i.decision == Decision.PENDING
                           and i.triage.severity == Severity.URGENT),
    }


def confidence_line(stats: dict[str, int]) -> str:
    if stats["found"] == 0:
        return "High confidence: the ATS and tracking layer agree everywhere we check."
    if stats["urgent_open"] > 0:
        return (f"Low confidence until the {stats['urgent_open']} open urgent "
                f"item(s) are resolved -- counts above may shift.")
    if stats["pending"] > 0:
        return (f"Moderate confidence: {stats['pending']} open discrepancy(ies) "
                f"under review, none urgent.")
    return "High confidence: every detected discrepancy has been human-reviewed."


def _movement(now: dict[str, Counter], prior: dict[str, dict[str, int]]) -> list[str]:
    lines = []
    for role in sorted(set(now) | set(prior)):
        for stage in REPORT_STAGES:
            current = now.get(role, Counter()).get(stage.value, 0)
            previous = prior.get(role, {}).get(stage.value, 0)
            delta = current - previous
            if delta:
                arrow = "+" if delta > 0 else ""
                lines.append(f"- {role} / {stage.value}: {previous} -> {current} ({arrow}{delta})")
    return lines or ["- no stage movement this week"]


def build_report_md(pipeline: CanonicalPipeline, items: list[QueueItem],
                    prior: dict[str, dict[str, int]]) -> str:
    now = snapshot_counts(pipeline)
    stats = hygiene_stats(items)
    week_of = pipeline.as_of.date().isoformat()

    lines = [f"# Pipeline report -- week of {week_of}", ""]

    lines += ["## Snapshot (role x stage)", ""]
    header = "| role | " + " | ".join(s.value for s in REPORT_STAGES) + " | total |"
    lines += [header,
              "|" + "---|" * (len(REPORT_STAGES) + 2)]
    for role in sorted(now):
        row = [str(now[role].get(s.value, 0)) for s in REPORT_STAGES]
        lines.append(f"| {role} | " + " | ".join(row) + f" | {sum(now[role].values())} |")
    lines.append("")

    lines += ["## Week-over-week movement", ""]
    lines += _movement(now, prior)
    lines.append("")

    lines += ["## Data hygiene", ""]
    lines += [
        f"- discrepancies found: **{stats['found']}**",
        f"- resolved (human-reviewed): **{stats['resolved']}** "
        f"({stats['approved']} approved, {stats['dismissed']} dismissed)",
        f"- pending: **{stats['pending']}**",
        "",
        f"> {confidence_line(stats)}",
        "",
    ]
    lines += ["---", "_All counts from the ATS export (source of truth); "
              "discrepancies from the audit queue. Nothing in this report "
              "was auto-corrected._"]
    return "\n".join(lines) + "\n"


def build_digest_txt(pipeline: CanonicalPipeline, items: list[QueueItem]) -> str:
    now = snapshot_counts(pipeline)
    stats = hygiene_stats(items)
    total_active = sum(1 for c in pipeline.candidates.values() if c.is_active)
    offers = sum(counts.get("offer", 0) for counts in now.values())
    week_of = pipeline.as_of.date().isoformat()
    return (
        f":clipboard: *Pipeline digest -- week of {week_of}*\n"
        f"* {total_active} active candidates across {len(now)} roles, {offers} at offer\n"
        f"* hygiene: {stats['found']} discrepancies found, {stats['resolved']} resolved, "
        f"{stats['pending']} pending ({stats['urgent_open']} urgent)\n"
        f"* {confidence_line(stats)}\n"
        f"Full report: out/report.md\n"
    )


def write_reports(pipeline: CanonicalPipeline, data_dir: str | Path,
                  out_dir: str | Path) -> tuple[Path, Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    items = load_queue(out_dir)
    prior = load_prior_week(data_dir)
    report_path = out_dir / "report.md"
    digest_path = out_dir / "digest.txt"
    report_path.write_text(build_report_md(pipeline, items, prior))
    digest_path.write_text(build_digest_txt(pipeline, items))
    return report_path, digest_path
