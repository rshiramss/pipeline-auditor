"""The six deterministic drift checks. No AI in this layer.

Each check is a pure function: (pipeline, rules) -> list[Discrepancy].
Thresholds come from rules.yaml; the reference time is pipeline.as_of.
"""

from __future__ import annotations

from datetime import timedelta
from itertools import combinations

from rapidfuzz import fuzz

from auditor.models import (
    Candidate,
    CanonicalPipeline,
    Discrepancy,
    DiscrepancyType,
    EvidenceRef,
    PASSED_STAGES,
    ResolvedEntry,
    Rules,
)
from auditor.normalize import stage_for_tag


def _ats_evidence(c: Candidate, field: str, value: str) -> EvidenceRef:
    return EvidenceRef(source="ashby", ref=f"{c.id}.{field}", field=field,
                       value=value, timestamp=c.last_updated)


def _entry_evidence(pipeline: CanonicalPipeline, e: ResolvedEntry) -> EvidenceRef:
    index = pipeline.entries.index(e)
    return EvidenceRef(source="tracking_log", ref=f"tracking_log[{index}].stage_tag",
                       field="stage_tag", value=e.entry.stage_tag,
                       timestamp=e.entry.timestamp)


def _latest_entry(entries: list[ResolvedEntry]) -> ResolvedEntry | None:
    return max(entries, key=lambda e: e.entry.timestamp) if entries else None


def check_stage_mismatch(pipeline: CanonicalPipeline, rules: Rules) -> list[Discrepancy]:
    """Tracking layer's latest stage != ATS stage for the same candidate."""
    found = []
    for cid, c in pipeline.candidates.items():
        latest = _latest_entry(pipeline.entries_for(cid))
        if latest is None:
            continue  # ghost check's territory
        tracked = stage_for_tag(latest.entry.stage_tag)
        if tracked is None or tracked == c.stage:
            continue
        found.append(Discrepancy(
            type=DiscrepancyType.STAGE_MISMATCH,
            candidates_involved=[cid],
            summary=(f"{c.name}: ATS says '{c.stage.value}', tracking log says "
                     f"'{latest.entry.stage_tag}' ({tracked.value})"),
            evidence=[_ats_evidence(c, "stage", c.stage.value),
                      _entry_evidence(pipeline, latest)],
            detected_at=pipeline.as_of,
        ))
    return found


def check_ghosts(pipeline: CanonicalPipeline, rules: Rules) -> list[Discrepancy]:
    """Candidates present in one source and absent from the other."""
    found = []
    for cid, c in pipeline.candidates.items():
        if c.is_active and not pipeline.entries_for(cid):
            found.append(Discrepancy(
                type=DiscrepancyType.GHOST_IN_ATS,
                candidates_involved=[cid],
                summary=f"{c.name} is active in the ATS but never appears in the tracking log",
                evidence=[_ats_evidence(c, "stage", c.stage.value)],
                detected_at=pipeline.as_of,
            ))
    seen_names = set()
    for e in pipeline.unresolved_entries:
        name = e.entry.candidate_name
        if name in seen_names:
            continue
        seen_names.add(name)
        found.append(Discrepancy(
            type=DiscrepancyType.GHOST_IN_TRACKING,
            candidates_involved=[name],
            summary=f"'{name}' appears in the tracking log but matches no ATS record "
                    f"(best name score {e.score:.0f})",
            evidence=[_entry_evidence(pipeline, e)],
            detected_at=pipeline.as_of,
        ))
    return found


def check_stale(pipeline: CanonicalPipeline, rules: Rules) -> list[Discrepancy]:
    """Active in ATS with no movement for more than stale_days."""
    cutoff = pipeline.as_of - timedelta(days=rules.stale_days)
    found = []
    for cid, c in pipeline.candidates.items():
        if c.is_active and c.last_updated < cutoff:
            idle_days = (pipeline.as_of - c.last_updated).days
            found.append(Discrepancy(
                type=DiscrepancyType.STALE,
                candidates_involved=[cid],
                summary=f"{c.name} marked active but untouched for {idle_days} days "
                        f"(threshold {rules.stale_days})",
                evidence=[_ats_evidence(c, "last_updated", c.last_updated.isoformat())],
                detected_at=pipeline.as_of,
            ))
    return found


def check_scheduling_limbo(pipeline: CanonicalPipeline, rules: Rules) -> list[Discrepancy]:
    """Passed a stage more than limbo_days ago with no future-dated next step."""
    found = []
    for cid, c in pipeline.candidates.items():
        if c.stage not in PASSED_STAGES:
            continue
        if any(ev.timestamp > pipeline.as_of for ev in c.scheduled_events):
            continue
        idle = pipeline.as_of - c.last_updated
        if idle <= timedelta(days=rules.limbo_days):
            continue
        found.append(Discrepancy(
            type=DiscrepancyType.SCHEDULING_LIMBO,
            candidates_involved=[cid],
            summary=f"{c.name} passed '{c.stage.value}' {idle.days} days ago; "
                    f"nothing scheduled next",
            evidence=[_ats_evidence(c, "stage", c.stage.value),
                      _ats_evidence(c, "scheduled_events",
                                    str(len(c.scheduled_events)) + " (none future)")],
            detected_at=pipeline.as_of,
        ))
    return found


def check_owner_gap(pipeline: CanonicalPipeline, rules: Rules) -> list[Discrepancy]:
    """Active candidate with nobody assigned."""
    found = []
    for cid, c in pipeline.candidates.items():
        if c.is_active and not c.owner:
            found.append(Discrepancy(
                type=DiscrepancyType.OWNER_GAP,
                candidates_involved=[cid],
                summary=f"{c.name} ({c.stage.value}) has no owner assigned",
                evidence=[_ats_evidence(c, "owner", repr(c.owner))],
                detected_at=pipeline.as_of,
            ))
    return found


def check_duplicate_suspects(pipeline: CanonicalPipeline, rules: Rules) -> list[Discrepancy]:
    """Two ATS records with near-identical names (WRatio >= threshold)."""
    found = []
    for (id_a, a), (id_b, b) in combinations(sorted(pipeline.candidates.items()), 2):
        score = fuzz.WRatio(a.name, b.name)
        if score < rules.duplicate_threshold:
            continue
        found.append(Discrepancy(
            type=DiscrepancyType.DUPLICATE_SUSPECT,
            candidates_involved=[id_a, id_b],
            summary=f"'{a.name}' ({id_a}) and '{b.name}' ({id_b}) look like the "
                    f"same person (name similarity {score:.0f})",
            evidence=[_ats_evidence(a, "name", a.name),
                      _ats_evidence(b, "name", b.name)],
            detected_at=pipeline.as_of,
        ))
    return found


ALL_CHECKS = [
    check_stage_mismatch,
    check_ghosts,
    check_stale,
    check_scheduling_limbo,
    check_owner_gap,
    check_duplicate_suspects,
]


def run_all_checks(pipeline: CanonicalPipeline, rules: Rules) -> list[Discrepancy]:
    found: list[Discrepancy] = []
    for check in ALL_CHECKS:
        found.extend(check(pipeline, rules))
    return found
