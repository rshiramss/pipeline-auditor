"""One audit pass: load -> normalize -> detect -> triage -> queue.

Shared by the CLI (`audit.py run`) and the TUI shell so both stay
orchestration over the same functions.
"""

from __future__ import annotations

from pathlib import Path

from auditor.drift import run_all_checks
from auditor.models import Discrepancy, QueueItem, Rules
from auditor.normalize import load_pipeline
from auditor.queue import build_queue_items, load_queue, merge_queue, save_queue
from auditor.triage import triage_all


def run_audit(rules: Rules, data_dir: str | Path = "data",
              out_dir: str | Path = "out", use_llm: bool = True,
              on_triage_progress=None) -> tuple[list[QueueItem], list[Discrepancy]]:
    """Returns (queue items written, discrepancies found)."""
    pipeline = load_pipeline(data_dir, rules.duplicate_threshold)
    discrepancies = run_all_checks(pipeline, rules)
    triages = triage_all(discrepancies, pipeline, rules, use_llm=use_llm,
                         on_progress=on_triage_progress)
    items = merge_queue(build_queue_items(discrepancies, triages,
                                          pipeline=pipeline, rules=rules),
                        load_queue(out_dir))
    save_queue(items, out_dir)
    return items, discrepancies
