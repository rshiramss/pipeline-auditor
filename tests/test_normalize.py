"""Identity-resolution ladder: exact -> normalized -> fuzzy -> flagged."""

from datetime import datetime, timezone

from auditor.models import Candidate, ResolutionMethod, Stage, StageEvent, TrackingEntry
from auditor.normalize import resolve_entry, stage_for_tag

TS = datetime(2026, 6, 8, tzinfo=timezone.utc)
THRESHOLD = 85


def candidate(cid: str, name: str) -> Candidate:
    return Candidate(id=cid, name=name, role="FDE", stage=Stage.APPLIED,
                     stage_history=[StageEvent(stage=Stage.APPLIED, timestamp=TS)],
                     owner="erin", last_updated=TS)


def entry(name: str) -> TrackingEntry:
    return TrackingEntry(candidate_name=name, stage_tag="applied", timestamp=TS)


CANDIDATES = {
    "c_001": candidate("c_001", "Jonathan Smith"),
    "c_002": candidate("c_002", "Priya Sharma"),
}


def test_exact_match():
    r = resolve_entry(entry("Priya Sharma"), CANDIDATES, THRESHOLD)
    assert (r.candidate_id, r.method) == ("c_002", ResolutionMethod.EXACT)


def test_normalized_match_handles_case_and_spacing():
    r = resolve_entry(entry("  priya  SHARMA "), CANDIDATES, THRESHOLD)
    assert (r.candidate_id, r.method) == ("c_002", ResolutionMethod.NORMALIZED)


def test_fuzzy_match_resolves_nickname():
    r = resolve_entry(entry("Jon Smith"), CANDIDATES, THRESHOLD)
    assert (r.candidate_id, r.method) == ("c_001", ResolutionMethod.FUZZY)
    assert r.score >= THRESHOLD


def test_unknown_name_is_flagged_never_merged():
    r = resolve_entry(entry("Totally Unknown"), CANDIDATES, THRESHOLD)
    assert r.candidate_id is None
    assert r.method == ResolutionMethod.UNRESOLVED


def test_stage_tag_mapping():
    assert stage_for_tag("intro_call_booked") == Stage.SCREEN_SCHEDULED
    assert stage_for_tag("OFFER_OUT") == Stage.OFFER
    assert stage_for_tag("some new vibe") is None
