"""Integration: every planted discrepancy is caught, nothing else is.

This is the demo's 'provably catches everything planted' claim, executable.
"""

import json
from pathlib import Path

import pytest

from auditor.config import load_rules
from auditor.drift import run_all_checks
from auditor.models import DiscrepancyType
from auditor.normalize import load_pipeline
from generate_data import generate


@pytest.fixture(scope="module", params=[(30, 42), (30, 7), (300, 42)])
def generated(request, tmp_path_factory):
    n, seed = request.param
    out = tmp_path_factory.mktemp(f"data_{n}_{seed}")
    manifest = generate(n, seed, "2026-06-08T09:00:00+00:00", out)
    rules = load_rules(Path(__file__).parent.parent / "rules.yaml")
    pipeline = load_pipeline(out, rules.duplicate_threshold)
    return manifest, run_all_checks(pipeline, rules), pipeline


def by_type(found, dtype):
    return [d for d in found if d.type == dtype]


def test_full_recall_and_zero_false_positives(generated):
    manifest, found, _ = generated
    planted = manifest["planted"]

    expectations = {
        DiscrepancyType.STAGE_MISMATCH: {(cid,) for cid in planted["stage_mismatch"]},
        DiscrepancyType.GHOST_IN_ATS: {(cid,) for cid in planted["ghost_in_ats"]},
        DiscrepancyType.GHOST_IN_TRACKING: {(name,) for name in planted["ghost_in_tracking"]},
        DiscrepancyType.STALE: {(cid,) for cid in planted["stale"]},
        DiscrepancyType.SCHEDULING_LIMBO: {(cid,) for cid in planted["scheduling_limbo"]},
        DiscrepancyType.OWNER_GAP: {(cid,) for cid in planted["owner_gap"]},
        DiscrepancyType.DUPLICATE_SUSPECT: {tuple(pair) for pair in planted["duplicate_suspect"]},
    }
    for dtype, expected in expectations.items():
        got = {tuple(d.candidates_involved) for d in by_type(found, dtype)}
        assert got == expected, f"{dtype.value}: expected {expected}, got {got}"


def test_fuzzy_case_resolved_not_ghosted(generated):
    manifest, found, pipeline = generated
    case = manifest["fuzzy_resolution_case"]
    assert case is not None, "generator failed to plant a fuzzy resolution case"
    ghost_names = {d.candidates_involved[0]
                   for d in by_type(found, DiscrepancyType.GHOST_IN_TRACKING)}
    assert case["logged_as"] not in ghost_names
    resolved_ids = {e.candidate_id for e in pipeline.entries
                    if e.entry.candidate_name == case["logged_as"]}
    assert resolved_ids == {case["id"]}


def test_every_discrepancy_carries_evidence(generated):
    _, found, _ = generated
    for d in found:
        assert d.evidence, f"{d.fingerprint} has no evidence"
        assert d.summary
