# pipeline-auditor

An agent that keeps a hiring pipeline's systems honest. It detects where the
source of truth (an ATS-style export) and the tracking layer (a Slack/Notion-style
log) disagree, drafts the fixes, and writes the weekly report. **A human
approves every change. It proposes; the human disposes.**

All data is synthetic — real candidate information shouldn't leave its system
of record.

## Run it (3 commands)

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python generate_data.py --n 30 --seed 42
.venv/bin/python audit.py run && .venv/bin/python audit.py review && .venv/bin/python audit.py report
```

Or run the whole loop inside one terminal UI:

```bash
.venv/bin/python audit.py tui
```

Without `ANTHROPIC_API_KEY` set, triage falls back to offline mode (severity
mapped from `rules.yaml`) — the full loop still runs. With the key set, an
investigation agent examines each discrepancy before judging it.

## What it does

```
generate/load data → normalize → detect drift → agent triage → human review → weekly report
     (script)         (script)     (script)        (LLM)         (human)        (script)
```

1. **Ingest** two sources: `data/ashby_export.json` (source of truth) and
   `data/tracking_log.json` (messier: names not ids, informal stage tags).
2. **Normalize** into one canonical model. Identity resolution runs
   exact → casefolded → fuzzy (`rapidfuzz`); anything below the threshold is
   flagged, never silently merged.
3. **Detect** six deterministic drift checks (`auditor/drift.py`). No AI in
   this layer: stage mismatch, ghosts (both directions), stale, scheduling
   limbo, owner gap, duplicate-suspect names.
4. **Triage**: one investigation per discrepancy. The agent gets four
   read-only tools (`lookup_candidate`, `get_stage_history`,
   `search_tracking_log`, `check_scheduled_events`) and decides for itself
   what evidence it needs — hard-capped at 8 tool calls. A structured
   judgment (severity, one-line explanation, drafted fix, cited evidence) is
   validated by pydantic. `insufficient_evidence` is a legal answer.
5. **Review**: `audit.py review` renders one screen per item — discrepancy,
   evidence table, the agent's actual investigation trace, drafted fix.
   `[a]pprove / [d]ismiss / [s]kip / [q]uit`. Approved drafts land in
   `out/sent_drafts/` — visibly drafted, **never transmitted**.
6. **Report**: `out/report.md` (role × stage snapshot, week-over-week
   movement, data-hygiene section) + `out/digest.txt` for Slack.

## Design decisions

- **Auditor, not syncer.** The source of record is never auto-edited.
  Nothing auto-sends. The only writable surface is `out/`.
- **Deterministic where possible, agent where judgment is needed.** The
  agent's autonomy lives in *investigation*, never in *action*.
- **Every agent conclusion cites its evidence**, and the investigation trace
  in the queue comes from the tool wrapper's own log — what the agent
  actually did, not what it claims it did.
- **Ambiguity is flagged, never silently merged.** Unresolvable names become
  ghost discrepancies, not guesses.
- **Disagreement between systems is signal, not noise.**
- **Built for today's scale, designed for the stated one** (8–10 hires/month).
  `--n 300` runs the same loop unchanged.
- **Legible.** Plain JSON in, three files out, thresholds in `rules.yaml`.

### Decisions that override the original SPEC (recorded on purpose)

1. **LangChain for the agent loop** (SPEC chose the raw `anthropic` SDK).
   `create_agent` runs the investigation; `with_structured_output(TriageJudgment)`
   emits the validated judgment. The 8-call cap stays ours: a shared counter
   in the tool wrappers, with the graph's `recursion_limit` as backstop.
2. **Everything is committed scope** (SPEC had a cut order): full-loop TUI,
   Notion push, all six checks, fuzzy matching are all in.
3. **`duplicate_threshold: 85`** (SPEC example said 90): measured
   `WRatio("Jon Smith", "Jonathan Smith") = 85.5`. The generator filters
   sampled names at the same threshold, so clean data can't collide with it.

## Provably catches what was planted

`generate_data.py` writes `data/planted_drift.json` — a manifest of exactly
which drift was injected. The test suite asserts full recall **and zero false
positives** against it at `--n 30` and `--n 300`:

```bash
.venv/bin/python -m pytest tests/ -q
```

## Notion push (optional path, output-only)

```bash
export NOTION_TOKEN=secret_...        # internal integration token
export NOTION_DATABASE_ID=...         # a scratch database shared with it
.venv/bin/python audit.py report --push-notion
```

One page per report: snapshot as a table block, hygiene as callouts. It
writes the report and nothing else — it never reads or syncs. Markdown
remains the default path; everything works without Notion.

## Repo map

```
audit.py                 entry point: run / review / report / tui
generate_data.py         synthetic data + planted-drift manifest (test oracle)
rules.yaml               thresholds, severity weights, roles in scope
auditor/
  models.py              pydantic models, the shared vocabulary
  normalize.py           two sources → canonical model; identity ladder
  drift.py               the six checks, pure functions
  agent_tools.py         four read-only tools + the call budget
  triage.py              investigate (create_agent) → judge (structured output)
  queue.py               approval queue: persistence + review TUI
  tui.py                 full-loop TUI shell
  report.py              report.md + digest.txt
  notion_push.py         output-only Notion publisher
tests/                   unit + recall-against-manifest integration tests
out/                     queue.json, sent_drafts/, report.md, digest.txt
```
