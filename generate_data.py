"""Generate synthetic hiring-pipeline data with drift injected at known rates.

Writes:
  data/ashby_export.json    ATS export (source of truth)
  data/tracking_log.json    Slack/Notion tracking layer (drifts from truth)
  data/planted_drift.json   manifest of exactly what was injected (test oracle)
  data/prior_week.json      stage counts one week before as_of (report diff)

All data is synthetic. Same --n/--seed/--as-of => byte-identical output.

Usage:
  python generate_data.py --n 30 --seed 42
"""

from __future__ import annotations

import argparse
import json
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rapidfuzz import fuzz

from auditor.config import load_rules

DEFAULT_AS_OF = "2026-06-08T09:00:00+00:00"
MIN_POOL = 20

FIRST_NAMES = [
    "Priya", "Wei", "Amara", "Diego", "Yuki", "Lena", "Tomas", "Ifeoma",
    "Ravi", "Sofia", "Marek", "Anika", "Jorge", "Mei", "Elias", "Nadia",
    "Kofi", "Ingrid", "Hassan", "Clara", "Dmitri", "Aisha", "Felix", "Rosa",
    "Arjun", "Hana", "Lucas", "Zainab", "Oscar", "Freya", "Mateo", "Leila",
    "Stefan", "Noor", "Pavel", "Camila", "Owen", "Sana", "Viktor", "Iris",
]
LAST_NAMES = [
    "Sharma", "Chen", "Okafor", "Ramirez", "Tanaka", "Hoffman", "Novak",
    "Eze", "Iyer", "Rossi", "Kowalski", "Gupta", "Mendez", "Lin", "Berg",
    "Haddad", "Mensah", "Larsen", "Farouk", "Dubois", "Volkov", "Diallo",
    "Wagner", "Moreno", "Pillai", "Sato", "Ferreira", "Hussain", "Nilsen",
    "Vargas", "Petrov", "Amini", "Keller", "Osei", "Horak", "Reyes",
    "Brandt", "Qureshi", "Sokolov", "Bianchi",
]
OWNERS = ["erin", "marcus", "dana", "atlas"]
SOURCES = [
    "USACO outreach", "IOI alumni list", "referral", "LinkedIn",
    "HN Who's Hiring", "Codeforces top list", "conference booth",
]
PEDIGREES = [
    "USACO Gold '19, now at a Series B",
    "IOI bronze '18, infra eng at a fintech",
    "Codeforces master, ML platform work",
    "Putnam top-200, ex-quant dev",
    "ICPC regionals finalist, backend at a startup",
    "USACO Platinum '21, new grad",
    "Kaggle GM, data eng at a scale-up",
]
NOTE_SNIPPETS = [
    "strong systems background", "asked about equity split",
    "wants remote-first", "great take-home", "needs visa transfer",
    "HM very positive", "moving fast, has competing offer",
]

# Active pipeline in order; generator only places candidates in these or "hired".
PIPELINE = [
    "applied", "screen_scheduled", "screen_done",
    "onsite_scheduled", "onsite_done", "offer",
]
SCHEDULED_STAGES = {"screen_scheduled", "onsite_scheduled"}
PASSED_STAGES = {"screen_done", "onsite_done", "offer"}
NEXT_EVENT_TYPE = {
    "screen_scheduled": "screen",
    "onsite_scheduled": "onsite",
    "screen_done": "onsite",
    "onsite_done": "debrief",
    "offer": "offer_call",
}

# Informal tags the tracking layer uses for each canonical stage.
TRACKING_TAGS = {
    "applied": ["applied", "in_pipeline"],
    "screen_scheduled": ["intro_call_booked", "screen_scheduled"],
    "screen_done": ["screen_done", "passed_screen"],
    "onsite_scheduled": ["onsite_scheduled", "onsite_booked"],
    "onsite_done": ["onsite_done", "finished_onsite"],
    "offer": ["offer_out", "offer"],
    "hired": ["signed", "hired"],
}


def iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def sample_names(rng: random.Random, count: int, threshold: float) -> list[str]:
    """Unique full names, none fuzzy-close to another (avoids accidental
    duplicate-suspect hits beyond the one we plant deliberately)."""
    names: list[str] = []
    attempts = 0
    while len(names) < count:
        attempts += 1
        if attempts > count * 200:
            raise RuntimeError("name pool exhausted; lower --n")
        candidate = f"{rng.choice(FIRST_NAMES)} {rng.choice(LAST_NAMES)}"
        if candidate in names:
            continue
        if any(fuzz.WRatio(candidate, n) >= threshold for n in names):
            continue
        names.append(candidate)
    return names


def build_history(rng: random.Random, stage: str, as_of: datetime,
                  last_move_days_ago: float) -> list[dict]:
    """Stage transitions ending at `stage`, last one `last_move_days_ago` back."""
    idx = PIPELINE.index(stage) if stage in PIPELINE else len(PIPELINE)
    stages = PIPELINE[: idx + 1] if stage in PIPELINE else PIPELINE + ["hired"]
    ts = as_of - timedelta(days=last_move_days_ago)
    history = []
    for s in reversed(stages):
        history.append({"stage": s, "timestamp": iso(ts)})
        ts -= timedelta(days=rng.uniform(2, 5), hours=rng.uniform(0, 8))
    return list(reversed(history))


def make_candidate(rng: random.Random, cid: str, name: str, role: str,
                   stage: str, as_of: datetime, last_move_days_ago: float,
                   owner: str | None, future_event: bool) -> dict:
    history = build_history(rng, stage, as_of, last_move_days_ago)
    scheduled = []
    if future_event:
        scheduled.append({
            "type": NEXT_EVENT_TYPE.get(stage, "check_in"),
            "timestamp": iso(as_of + timedelta(days=rng.uniform(1, 4))),
        })
    return {
        "id": cid,
        "name": name,
        "role": role,
        "stage": stage,
        "stage_history": history,
        "scheduled_events": scheduled,
        "owner": owner,
        "source": rng.choice(SOURCES),
        "notes": f"{rng.choice(PEDIGREES)}; {rng.choice(NOTE_SNIPPETS)}",
        "last_updated": history[-1]["timestamp"],
    }


def tracking_entries_for(rng: random.Random, candidate: dict, as_of: datetime,
                         tracked_stage: str) -> list[dict]:
    """1-3 log entries ending at tracked_stage (== ATS stage when healthy,
    a later stage when a mismatch is planted)."""
    channel = "#hiring-" + candidate["role"].lower().replace(" ", "-")
    history = candidate["stage_history"]
    entries = []
    earlier = [h for h in history if h["stage"] != tracked_stage]
    for h in rng.sample(earlier, k=min(len(earlier), rng.randint(0, 2))):
        entries.append({
            "candidate_name": candidate["name"],
            "stage_tag": rng.choice(TRACKING_TAGS[h["stage"]]),
            "timestamp": h["timestamp"],
            "channel": channel,
            "note": rng.choice(NOTE_SNIPPETS),
        })
    last_ats = datetime.fromisoformat(history[-1]["timestamp"].replace("Z", "+00:00"))
    final_ts = min(last_ats + timedelta(hours=rng.uniform(1, 36)), as_of)
    entries.append({
        "candidate_name": candidate["name"],
        "stage_tag": rng.choice(TRACKING_TAGS[tracked_stage]),
        "timestamp": iso(final_ts),
        "channel": channel,
        "note": rng.choice(NOTE_SNIPPETS),
    })
    entries.sort(key=lambda e: e["timestamp"])
    return entries


def prior_week_counts(candidates: list[dict], as_of: datetime) -> dict:
    """Stage each candidate was in 7 days before as_of, counted per role."""
    cutoff = as_of - timedelta(days=7)
    counts: dict[str, dict[str, int]] = {}
    for c in candidates:
        stage_then = None
        for h in c["stage_history"]:
            ts = datetime.fromisoformat(h["timestamp"].replace("Z", "+00:00"))
            if ts <= cutoff:
                stage_then = h["stage"]
        if stage_then is None:
            continue  # not yet in pipeline a week ago
        counts.setdefault(c["role"], {}).setdefault(stage_then, 0)
        counts[c["role"]][stage_then] += 1
    return counts


def generate(n: int, seed: int, as_of_str: str, out_dir: Path) -> dict:
    if n < MIN_POOL:
        raise SystemExit(f"--n must be >= {MIN_POOL} to fit all planted drift")
    rng = random.Random(seed)
    as_of = datetime.fromisoformat(as_of_str)
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)
    rules = load_rules()
    roles = rules.roles_in_scope or ["FDE"]
    threshold = rules.duplicate_threshold

    # +2 names reserved for tracking-only ghosts
    names = sample_names(rng, n + 2, threshold)
    ghost_tracking_names = names[n:]
    names = names[:n]

    # Partition indices per planted drift type (disjoint by construction).
    n_mismatch = max(2, round(n * 0.15))
    n_stale = 3 if n >= 30 else 2
    cursor = 0

    def take(k: int) -> list[int]:
        nonlocal cursor
        idxs = list(range(cursor, cursor + k))
        cursor += k
        return idxs

    mismatch_idx = take(n_mismatch)
    ghost_ats_idx = take(2)
    stale_idx = take(n_stale)
    limbo_idx = take(2)
    owner_gap_idx = take(1)
    dup_idx = take(2)  # the planted near-duplicate pair
    healthy_idx = list(range(cursor, n))

    # The planted fuzzy pair replaces two sampled names.
    names[dup_idx[0]] = "Jon Smith"
    names[dup_idx[1]] = "Jonathan Smith"

    candidates: list[dict] = []
    tracking: list[dict] = []
    manifest: dict = {k: [] for k in (
        "stage_mismatch", "ghost_in_ats", "ghost_in_tracking", "stale",
        "scheduling_limbo", "owner_gap", "duplicate_suspect")}

    def healthy_stage_plan(i: int) -> tuple[str, float, bool]:
        """(stage, days since last move, has future event) for a clean record."""
        stage = rng.choice(PIPELINE)
        recent = rng.uniform(0.5, rules.stale_days - 1.5)
        if stage in SCHEDULED_STAGES:
            return stage, recent, True
        if stage in PASSED_STAGES:
            # keep clean of limbo: either fresh or has a future step
            if rng.random() < 0.5:
                return stage, rng.uniform(0.5, rules.limbo_days - 0.5), False
            return stage, recent, True
        return stage, recent, False

    for i in range(n):
        cid = f"c_{i + 1:03d}"
        role = rng.choice(roles)
        owner = rng.choice(OWNERS)

        if i in mismatch_idx:
            # ATS lags: tracking already shows the next stage.
            stage = rng.choice(PIPELINE[:-1])
            ahead = PIPELINE[PIPELINE.index(stage) + 1]
            c = make_candidate(rng, cid, names[i], role, stage, as_of,
                               rng.uniform(1, rules.stale_days - 2), owner,
                               future_event=stage in SCHEDULED_STAGES or stage in PASSED_STAGES)
            tracking.extend(tracking_entries_for(rng, c, as_of, ahead))
            manifest["stage_mismatch"].append(cid)
        elif i in ghost_ats_idx:
            # Active in ATS, zero tracking entries.
            stage, days, future = healthy_stage_plan(i)
            c = make_candidate(rng, cid, names[i], role, stage, as_of, days, owner, future)
            manifest["ghost_in_ats"].append(cid)
        elif i in stale_idx:
            # No ATS movement past stale_days; tracking agrees on stage.
            stage = rng.choice(["applied", "screen_scheduled"])
            c = make_candidate(rng, cid, names[i], role, stage, as_of,
                               rng.uniform(rules.stale_days + 1, rules.stale_days + 10),
                               owner, future_event=False)
            tracking.extend(tracking_entries_for(rng, c, as_of, stage))
            manifest["stale"].append(cid)
        elif i in limbo_idx:
            # Passed a stage, nothing scheduled, past limbo_days (but not stale).
            stage = rng.choice(["screen_done", "onsite_done"])
            c = make_candidate(rng, cid, names[i], role, stage, as_of,
                               rng.uniform(rules.limbo_days + 0.5, rules.stale_days - 1),
                               owner, future_event=False)
            tracking.extend(tracking_entries_for(rng, c, as_of, stage))
            manifest["scheduling_limbo"].append(cid)
        elif i in owner_gap_idx:
            stage, days, future = healthy_stage_plan(i)
            c = make_candidate(rng, cid, names[i], role, stage, as_of, days, None, future)
            tracking.extend(tracking_entries_for(rng, c, as_of, stage))
            manifest["owner_gap"].append(cid)
        elif i in dup_idx:
            # Both duplicate records are tracked under their own names, so the
            # only planted signal here is the near-identical ATS names.
            stage, days, future = healthy_stage_plan(i)
            c = make_candidate(rng, cid, names[i], role, stage, as_of, days, owner, future)
            tracking.extend(tracking_entries_for(rng, c, as_of, stage))
        else:
            stage, days, future = healthy_stage_plan(i)
            c = make_candidate(rng, cid, names[i], role, stage, as_of, days, owner, future)
            tracking.extend(tracking_entries_for(rng, c, as_of, stage))
        candidates.append(c)

    manifest["duplicate_suspect"].append(
        [f"c_{dup_idx[0] + 1:03d}", f"c_{dup_idx[1] + 1:03d}"])

    # One fuzzy identity-resolution case: a healthy candidate whose tracking
    # entries use a shortened first name ("Stefan Keller" logged as "Stef
    # Keller"). Must resolve via rapidfuzz, uniquely, above the threshold.
    fuzzy_case = None
    for i in healthy_idx:
        full = candidates[i]["name"]
        first, last = full.split(" ", 1)
        if len(first) < 6:
            continue
        variant = f"{first[:4]} {last}"
        scores = sorted((fuzz.WRatio(variant, c["name"]) for c in candidates),
                        reverse=True)
        if fuzz.WRatio(variant, full) >= threshold and scores[1] < threshold:
            for e in tracking:
                if e["candidate_name"] == full:
                    e["candidate_name"] = variant
            fuzzy_case = {"id": candidates[i]["id"], "ats_name": full,
                          "logged_as": variant}
            break
    manifest_extra = {"fuzzy_resolution_case": fuzzy_case}

    # Ghosts in tracking: entries whose names match no ATS record.
    for gname in ghost_tracking_names:
        role = rng.choice(roles)
        tracking.append({
            "candidate_name": gname,
            "stage_tag": rng.choice(TRACKING_TAGS[rng.choice(PIPELINE)]),
            "timestamp": iso(as_of - timedelta(days=rng.uniform(0.5, 5))),
            "channel": "#hiring-" + role.lower().replace(" ", "-"),
            "note": rng.choice(NOTE_SNIPPETS),
        })
        manifest["ghost_in_tracking"].append(gname)

    tracking.sort(key=lambda e: e["timestamp"])

    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "ashby_export.json").write_text(
        json.dumps(candidates, indent=2) + "\n")
    (out_dir / "tracking_log.json").write_text(
        json.dumps(tracking, indent=2) + "\n")
    full_manifest = {
        "n": n, "seed": seed, "as_of": iso(as_of),
        "healthy_count": len(healthy_idx),
        "planted": manifest,
        **manifest_extra,
    }
    (out_dir / "planted_drift.json").write_text(
        json.dumps(full_manifest, indent=2) + "\n")
    (out_dir / "prior_week.json").write_text(
        json.dumps(prior_week_counts(candidates, as_of), indent=2) + "\n")
    return full_manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--n", type=int, default=30, help="candidate pool size")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--as-of", default=DEFAULT_AS_OF,
                        help="reference 'now' for all timestamps (ISO 8601)")
    parser.add_argument("--out", default="data", help="output directory")
    args = parser.parse_args()
    manifest = generate(args.n, args.seed, args.as_of, Path(args.out))
    planted = {k: len(v) for k, v in manifest["planted"].items()}
    print(f"wrote {args.out}/: n={args.n} seed={args.seed} planted={planted}")


if __name__ == "__main__":
    main()
