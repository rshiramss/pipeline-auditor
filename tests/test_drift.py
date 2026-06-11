"""Unit tests for the six drift checks: triggering case, boundary,
non-triggering case for each."""

from datetime import datetime, timedelta, timezone

from auditor.drift import (
    check_duplicate_suspects,
    check_ghosts,
    check_owner_gap,
    check_scheduling_limbo,
    check_stage_mismatch,
    check_stale,
)
from auditor.models import (
    Candidate,
    CanonicalPipeline,
    DiscrepancyType,
    ResolutionMethod,
    ResolvedEntry,
    Rules,
    ScheduledEvent,
    Stage,
    StageEvent,
    TrackingEntry,
)

AS_OF = datetime(2026, 6, 8, 9, 0, tzinfo=timezone.utc)
RULES = Rules(stale_days=7, limbo_days=3, duplicate_threshold=85)


def make_candidate(cid="c_001", name="Priya Sharma", stage=Stage.SCREEN_SCHEDULED,
                   days_idle=2.0, owner="erin", scheduled=()):
    ts = AS_OF - timedelta(days=days_idle)
    return Candidate(
        id=cid, name=name, role="FDE", stage=stage,
        stage_history=[StageEvent(stage=stage, timestamp=ts)],
        scheduled_events=list(scheduled),
        owner=owner, source="referral", last_updated=ts,
    )


def make_entry(name="Priya Sharma", tag="screen_scheduled", days_ago=1.0,
               candidate_id="c_001", method=ResolutionMethod.EXACT):
    entry = TrackingEntry(candidate_name=name, stage_tag=tag,
                          timestamp=AS_OF - timedelta(days=days_ago),
                          channel="#hiring-fde", note="")
    return ResolvedEntry(entry=entry, candidate_id=candidate_id, method=method,
                         score=100.0 if candidate_id else 50.0)


def pipeline_of(candidates, entries):
    return CanonicalPipeline(candidates={c.id: c for c in candidates},
                             entries=entries, as_of=AS_OF)


class TestStageMismatch:
    def test_flags_when_tracking_disagrees(self):
        p = pipeline_of([make_candidate()], [make_entry(tag="onsite_done")])
        found = check_stage_mismatch(p, RULES)
        assert [d.type for d in found] == [DiscrepancyType.STAGE_MISMATCH]
        assert found[0].candidates_involved == ["c_001"]

    def test_quiet_when_stages_agree(self):
        p = pipeline_of([make_candidate()], [make_entry(tag="intro_call_booked")])
        assert check_stage_mismatch(p, RULES) == []

    def test_uses_latest_entry_only(self):
        entries = [make_entry(tag="applied", days_ago=5.0),
                   make_entry(tag="screen_scheduled", days_ago=0.5)]
        p = pipeline_of([make_candidate()], entries)
        assert check_stage_mismatch(p, RULES) == []

    def test_unknown_tag_is_not_guessed_at(self):
        p = pipeline_of([make_candidate()], [make_entry(tag="vibing")])
        assert check_stage_mismatch(p, RULES) == []


class TestGhosts:
    def test_ats_candidate_with_no_tracking_is_ghost(self):
        p = pipeline_of([make_candidate()], [])
        found = check_ghosts(p, RULES)
        assert [d.type for d in found] == [DiscrepancyType.GHOST_IN_ATS]

    def test_terminal_candidate_is_not_a_ghost(self):
        p = pipeline_of([make_candidate(stage=Stage.HIRED)], [])
        assert check_ghosts(p, RULES) == []

    def test_unresolved_tracking_name_is_ghost(self):
        p = pipeline_of([make_candidate()],
                        [make_entry(), make_entry(name="Totally Unknown",
                                                  candidate_id=None,
                                                  method=ResolutionMethod.UNRESOLVED)])
        found = check_ghosts(p, RULES)
        assert [d.type for d in found] == [DiscrepancyType.GHOST_IN_TRACKING]
        assert found[0].candidates_involved == ["Totally Unknown"]

    def test_same_unknown_name_reported_once(self):
        ghosts = [make_entry(name="Totally Unknown", candidate_id=None,
                             method=ResolutionMethod.UNRESOLVED) for _ in range(3)]
        p = pipeline_of([make_candidate()], [make_entry()] + ghosts)
        assert len(check_ghosts(p, RULES)) == 1


class TestStale:
    def test_flags_past_threshold(self):
        p = pipeline_of([make_candidate(days_idle=8.5)], [make_entry()])
        assert [d.type for d in check_stale(p, RULES)] == [DiscrepancyType.STALE]

    def test_boundary_exactly_stale_days_is_not_stale(self):
        p = pipeline_of([make_candidate(days_idle=7.0)], [make_entry()])
        assert check_stale(p, RULES) == []

    def test_terminal_stage_never_stale(self):
        p = pipeline_of([make_candidate(stage=Stage.REJECTED, days_idle=30)], [])
        assert check_stale(p, RULES) == []


class TestSchedulingLimbo:
    def test_passed_stage_nothing_scheduled(self):
        p = pipeline_of([make_candidate(stage=Stage.ONSITE_DONE, days_idle=4)], [])
        found = check_scheduling_limbo(p, RULES)
        assert [d.type for d in found] == [DiscrepancyType.SCHEDULING_LIMBO]

    def test_future_event_clears_limbo(self):
        ev = ScheduledEvent(type="debrief", timestamp=AS_OF + timedelta(days=1))
        p = pipeline_of([make_candidate(stage=Stage.ONSITE_DONE, days_idle=4,
                                        scheduled=[ev])], [])
        assert check_scheduling_limbo(p, RULES) == []

    def test_past_event_does_not_clear_limbo(self):
        ev = ScheduledEvent(type="debrief", timestamp=AS_OF - timedelta(days=1))
        p = pipeline_of([make_candidate(stage=Stage.ONSITE_DONE, days_idle=4,
                                        scheduled=[ev])], [])
        assert len(check_scheduling_limbo(p, RULES)) == 1

    def test_recent_pass_is_not_limbo(self):
        p = pipeline_of([make_candidate(stage=Stage.SCREEN_DONE, days_idle=2)], [])
        assert check_scheduling_limbo(p, RULES) == []

    def test_non_passed_stage_is_not_limbo(self):
        p = pipeline_of([make_candidate(stage=Stage.APPLIED, days_idle=5)], [])
        assert check_scheduling_limbo(p, RULES) == []


class TestOwnerGap:
    def test_active_without_owner(self):
        p = pipeline_of([make_candidate(owner=None)], [])
        assert [d.type for d in check_owner_gap(p, RULES)] == [DiscrepancyType.OWNER_GAP]

    def test_owned_candidate_passes(self):
        p = pipeline_of([make_candidate(owner="erin")], [])
        assert check_owner_gap(p, RULES) == []

    def test_terminal_unowned_is_fine(self):
        p = pipeline_of([make_candidate(stage=Stage.WITHDRAWN, owner=None)], [])
        assert check_owner_gap(p, RULES) == []


class TestDuplicateSuspects:
    def test_near_identical_names_flagged_as_pair(self):
        a = make_candidate(cid="c_001", name="Jon Smith")
        b = make_candidate(cid="c_002", name="Jonathan Smith")
        found = check_duplicate_suspects(pipeline_of([a, b], []), RULES)
        assert [d.type for d in found] == [DiscrepancyType.DUPLICATE_SUSPECT]
        assert found[0].candidates_involved == ["c_001", "c_002"]

    def test_distinct_names_pass(self):
        a = make_candidate(cid="c_001", name="Priya Sharma")
        b = make_candidate(cid="c_002", name="Diego Ramirez")
        assert check_duplicate_suspects(pipeline_of([a, b], []), RULES) == []
