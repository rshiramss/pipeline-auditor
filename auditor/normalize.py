"""Normalize the two sources into one canonical model.

Identity resolution ladder (in order): exact name -> casefolded/stripped name
-> rapidfuzz WRatio >= duplicate_threshold. Anything below the threshold is
left unresolved and flagged -- ambiguity is never silently merged.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from rapidfuzz import fuzz

from auditor.models import (
    Candidate,
    CanonicalPipeline,
    ResolutionMethod,
    ResolvedEntry,
    Stage,
    TrackingEntry,
)

# Informal tracking-layer tags -> canonical stages. The tracking log is
# free-text-ish by design; unknown tags resolve to None and surface in
# evidence rather than being guessed at.
TAG_TO_STAGE: dict[str, Stage] = {
    "applied": Stage.APPLIED,
    "in_pipeline": Stage.APPLIED,
    "intro_call_booked": Stage.SCREEN_SCHEDULED,
    "screen_scheduled": Stage.SCREEN_SCHEDULED,
    "screen_done": Stage.SCREEN_DONE,
    "passed_screen": Stage.SCREEN_DONE,
    "onsite_scheduled": Stage.ONSITE_SCHEDULED,
    "onsite_booked": Stage.ONSITE_SCHEDULED,
    "onsite_done": Stage.ONSITE_DONE,
    "finished_onsite": Stage.ONSITE_DONE,
    "offer_out": Stage.OFFER,
    "offer": Stage.OFFER,
    "signed": Stage.HIRED,
    "hired": Stage.HIRED,
    "rejected": Stage.REJECTED,
    "withdrew": Stage.WITHDRAWN,
    "withdrawn": Stage.WITHDRAWN,
}


def stage_for_tag(tag: str) -> Stage | None:
    return TAG_TO_STAGE.get(tag.strip().lower())


def _normalize_name(name: str) -> str:
    return " ".join(name.casefold().split())


def resolve_entry(entry: TrackingEntry, candidates: dict[str, Candidate],
                  threshold: float) -> ResolvedEntry:
    """Attach a tracking entry to an ATS candidate, or flag it unresolved."""
    by_exact = {c.name: cid for cid, c in candidates.items()}
    if entry.candidate_name in by_exact:
        return ResolvedEntry(entry=entry, candidate_id=by_exact[entry.candidate_name],
                             method=ResolutionMethod.EXACT, score=100.0)

    wanted = _normalize_name(entry.candidate_name)
    for cid, c in candidates.items():
        if _normalize_name(c.name) == wanted:
            return ResolvedEntry(entry=entry, candidate_id=cid,
                                 method=ResolutionMethod.NORMALIZED, score=100.0)

    best_cid, best_score = None, 0.0
    for cid, c in candidates.items():
        score = fuzz.WRatio(entry.candidate_name, c.name)
        if score > best_score:
            best_cid, best_score = cid, score
    if best_cid is not None and best_score >= threshold:
        return ResolvedEntry(entry=entry, candidate_id=best_cid,
                             method=ResolutionMethod.FUZZY, score=best_score)

    return ResolvedEntry(entry=entry, candidate_id=None,
                         method=ResolutionMethod.UNRESOLVED, score=best_score)


def normalize(ashby_raw: list[dict], tracking_raw: list[dict],
              threshold: float, as_of: datetime) -> CanonicalPipeline:
    candidates = {c["id"]: Candidate.model_validate(c) for c in ashby_raw}
    entries = [
        resolve_entry(TrackingEntry.model_validate(e), candidates, threshold)
        for e in tracking_raw
    ]
    return CanonicalPipeline(candidates=candidates, entries=entries, as_of=as_of)


def load_pipeline(data_dir: str | Path, threshold: float,
                  as_of: datetime | None = None) -> CanonicalPipeline:
    """Load both source files and normalize.

    as_of defaults to the value recorded by the generator (keeps stale/limbo
    math correct regardless of when the audit actually runs), falling back to
    the current time for non-generated data.
    """
    data_dir = Path(data_dir)
    ashby = json.loads((data_dir / "ashby_export.json").read_text())
    tracking = json.loads((data_dir / "tracking_log.json").read_text())
    if as_of is None:
        manifest_path = data_dir / "planted_drift.json"
        if manifest_path.exists():
            recorded = json.loads(manifest_path.read_text())["as_of"]
            as_of = datetime.fromisoformat(recorded.replace("Z", "+00:00"))
        else:
            from datetime import timezone
            as_of = datetime.now(timezone.utc)
    return normalize(ashby, tracking, threshold, as_of)
