"""Typed models for the pipeline auditor.

Every layer of the pipeline (normalize -> drift -> triage -> queue -> report)
exchanges these pydantic models. LLM output is validated against
TriageJudgment, so a malformed agent response fails loudly here rather than
leaking downstream.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from hashlib import sha256

from pydantic import BaseModel, Field


class Stage(str, Enum):
    """Canonical ATS stages, in pipeline order."""

    APPLIED = "applied"
    SCREEN_SCHEDULED = "screen_scheduled"
    SCREEN_DONE = "screen_done"
    ONSITE_SCHEDULED = "onsite_scheduled"
    ONSITE_DONE = "onsite_done"
    OFFER = "offer"
    HIRED = "hired"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"


STAGE_ORDER: dict[Stage, int] = {
    Stage.APPLIED: 0,
    Stage.SCREEN_SCHEDULED: 1,
    Stage.SCREEN_DONE: 2,
    Stage.ONSITE_SCHEDULED: 3,
    Stage.ONSITE_DONE: 4,
    Stage.OFFER: 5,
    Stage.HIRED: 6,
    Stage.REJECTED: 7,
    Stage.WITHDRAWN: 8,
}

TERMINAL_STAGES = {Stage.HIRED, Stage.REJECTED, Stage.WITHDRAWN}

# Stages where the candidate has passed a milestone and the next step must
# be scheduled (feeds the scheduling-limbo check).
PASSED_STAGES = {Stage.SCREEN_DONE, Stage.ONSITE_DONE, Stage.OFFER}


class StageEvent(BaseModel):
    stage: Stage
    timestamp: datetime


class ScheduledEvent(BaseModel):
    """A future-dated next step attached to an ATS record."""

    type: str
    timestamp: datetime


class Candidate(BaseModel):
    """One record from the ATS export (source of truth)."""

    id: str
    name: str
    role: str
    stage: Stage
    stage_history: list[StageEvent] = Field(default_factory=list)
    scheduled_events: list[ScheduledEvent] = Field(default_factory=list)
    owner: str | None = None
    source: str = ""
    notes: str = ""
    last_updated: datetime

    @property
    def is_active(self) -> bool:
        return self.stage not in TERMINAL_STAGES


class TrackingEntry(BaseModel):
    """One record from the Slack/Notion tracking log (drifts from truth)."""

    candidate_name: str
    stage_tag: str
    timestamp: datetime
    channel: str = ""
    note: str = ""


class ResolutionMethod(str, Enum):
    EXACT = "exact"
    NORMALIZED = "normalized"
    FUZZY = "fuzzy"
    UNRESOLVED = "unresolved"


class ResolvedEntry(BaseModel):
    """A tracking entry after identity resolution.

    candidate_id is None when no ATS record matched confidently enough --
    flagged, never silently merged.
    """

    entry: TrackingEntry
    candidate_id: str | None = None
    method: ResolutionMethod = ResolutionMethod.UNRESOLVED
    score: float = 0.0


class CanonicalPipeline(BaseModel):
    """Both sources, normalized. The only data surface drift and triage see."""

    candidates: dict[str, Candidate]
    entries: list[ResolvedEntry]
    as_of: datetime

    def entries_for(self, candidate_id: str) -> list[ResolvedEntry]:
        return [e for e in self.entries if e.candidate_id == candidate_id]

    @property
    def unresolved_entries(self) -> list[ResolvedEntry]:
        return [e for e in self.entries if e.candidate_id is None]


class DiscrepancyType(str, Enum):
    STAGE_MISMATCH = "stage_mismatch"
    GHOST_IN_ATS = "ghost_in_ats"            # in ATS, absent from tracking
    GHOST_IN_TRACKING = "ghost_in_tracking"  # in tracking, absent from ATS
    STALE = "stale"
    SCHEDULING_LIMBO = "scheduling_limbo"
    OWNER_GAP = "owner_gap"
    DUPLICATE_SUSPECT = "duplicate_suspect"


class EvidenceRef(BaseModel):
    """A pointer at a specific field in a specific source record."""

    source: str  # "ashby" | "tracking_log"
    ref: str     # e.g. "c_017.stage" or "tracking_log[42].stage_tag"
    field: str
    value: str
    timestamp: datetime | None = None


class Discrepancy(BaseModel):
    type: DiscrepancyType
    candidates_involved: list[str]  # ATS ids, or raw names for tracking ghosts
    summary: str
    evidence: list[EvidenceRef]
    detected_at: datetime

    @property
    def fingerprint(self) -> str:
        """Stable identity across runs: type + involved parties.

        Lets a re-run preserve prior human decisions instead of
        resurrecting dismissed items.
        """
        key = f"{self.type.value}:{':'.join(sorted(self.candidates_involved))}"
        return f"{self.type.value}-{sha256(key.encode()).hexdigest()[:8]}"


class Severity(str, Enum):
    LAG = "lag"
    ATTENTION = "attention"
    URGENT = "urgent"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


SEVERITY_COLORS: dict[Severity, str] = {
    Severity.URGENT: "red",
    Severity.ATTENTION: "yellow",
    Severity.LAG: "dim",
    Severity.INSUFFICIENT_EVIDENCE: "cyan",
}


class FixType(str, Enum):
    SLACK_DRAFT = "slack_draft"
    ASHBY_UPDATE_SUGGESTION = "ashby_update_suggestion"


class ProposedFix(BaseModel):
    type: FixType
    content: str


class TriageJudgment(BaseModel):
    """The structured judgment the LLM must emit (phase 2 of triage)."""

    severity: Severity
    explanation: str = Field(description="One line explaining the discrepancy")
    proposed_fix: ProposedFix
    evidence_cited: list[str] = Field(
        description="Refs actually retrieved during investigation, e.g. 'c_017.stage'"
    )


class TriageResult(BaseModel):
    """Judgment plus the audit trail of how it was reached."""

    severity: Severity
    explanation: str
    proposed_fix: ProposedFix
    evidence_cited: list[str]
    investigation_trace: list[str] = Field(default_factory=list)


class Decision(str, Enum):
    PENDING = "pending"  # skip leaves an item here for the next session
    APPROVED = "approved"
    DISMISSED = "dismissed"


class QueueItem(BaseModel):
    fingerprint: str
    discrepancy: Discrepancy
    triage: TriageResult
    decision: Decision = Decision.PENDING
    decided_at: datetime | None = None


class LlmRules(BaseModel):
    model: str = "claude-sonnet-4-6"
    max_tool_calls: int = 8
    recursion_limit: int = 20


class Rules(BaseModel):
    """rules.yaml, validated at startup."""

    stale_days: int = 7
    limbo_days: int = 3
    duplicate_threshold: float = 90
    roles_in_scope: list[str] = Field(default_factory=list)
    severity_weights: dict[DiscrepancyType, Severity] = Field(default_factory=dict)
    llm: LlmRules = Field(default_factory=LlmRules)
