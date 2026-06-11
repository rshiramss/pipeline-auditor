"""Read-only investigation tools for the triage agent.

Four lookups over the canonical model, wrapped with a shared call budget.
The budget is OUR deterministic cap (SPEC: hard cap of 8 calls per
discrepancy); the agent graph's recursion_limit is only a backstop. Every
real call is recorded in the budget's trace -- the audit trail reflects what
the agent actually did, not what it claims it did.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from langchain.tools import tool

from auditor.models import CanonicalPipeline

BUDGET_EXHAUSTED_MSG = (
    "Investigation budget exhausted ({max} tool calls). Do not request more "
    "evidence; conclude with your judgment now. If the evidence gathered so "
    "far is not enough, answer insufficient_evidence."
)


@dataclass
class InvestigationBudget:
    max_calls: int = 8
    used: int = 0
    trace: list[str] = field(default_factory=list)

    @property
    def exhausted(self) -> bool:
        return self.used >= self.max_calls

    def spend(self, call_repr: str) -> bool:
        """Record a call attempt. Returns False when over budget."""
        if self.exhausted:
            return False
        self.used += 1
        self.trace.append(call_repr)
        return True


def _dump(payload) -> str:
    return json.dumps(payload, default=str, indent=1)


def make_tools(pipeline: CanonicalPipeline, budget: InvestigationBudget) -> list:
    """Build the four @tool functions, closing over one pipeline + budget."""

    @tool
    def lookup_candidate(candidate_id: str) -> str:
        """Return the full ATS record for a candidate id (e.g. 'c_017')."""
        if not budget.spend(f"lookup_candidate({candidate_id})"):
            return BUDGET_EXHAUSTED_MSG.format(max=budget.max_calls)
        c = pipeline.candidates.get(candidate_id)
        if c is None:
            return f"No ATS record with id {candidate_id!r}."
        return _dump(c.model_dump(mode="json", exclude={"stage_history"}))

    @tool
    def get_stage_history(candidate_id: str) -> str:
        """Return the ordered stage transitions (with timestamps) for a candidate id."""
        if not budget.spend(f"get_stage_history({candidate_id})"):
            return BUDGET_EXHAUSTED_MSG.format(max=budget.max_calls)
        c = pipeline.candidates.get(candidate_id)
        if c is None:
            return f"No ATS record with id {candidate_id!r}."
        return _dump([h.model_dump(mode="json") for h in c.stage_history])

    @tool
    def search_tracking_log(name_or_id: str) -> str:
        """Return tracking-log entries whose resolved candidate id or logged
        name matches the query (candidate id like 'c_017', or a name)."""
        if not budget.spend(f"search_tracking_log({name_or_id})"):
            return BUDGET_EXHAUSTED_MSG.format(max=budget.max_calls)
        query = name_or_id.strip().casefold()
        hits = []
        for i, e in enumerate(pipeline.entries):
            logged = e.entry.candidate_name.casefold()
            if e.candidate_id == name_or_id or query in logged:
                hits.append({"index": i,
                             "resolved_candidate_id": e.candidate_id,
                             "resolution": e.method.value,
                             **e.entry.model_dump(mode="json")})
        if not hits:
            return f"No tracking-log entries match {name_or_id!r}."
        return _dump(hits)

    @tool
    def check_scheduled_events(candidate_id: str) -> str:
        """Return any future-dated next steps for a candidate id, relative to
        the audit reference time."""
        if not budget.spend(f"check_scheduled_events({candidate_id})"):
            return BUDGET_EXHAUSTED_MSG.format(max=budget.max_calls)
        c = pipeline.candidates.get(candidate_id)
        if c is None:
            return f"No ATS record with id {candidate_id!r}."
        future = [ev.model_dump(mode="json") for ev in c.scheduled_events
                  if ev.timestamp > pipeline.as_of]
        return _dump({"as_of": pipeline.as_of.isoformat(),
                      "future_events": future})

    return [lookup_candidate, get_stage_history, search_tracking_log,
            check_scheduled_events]
