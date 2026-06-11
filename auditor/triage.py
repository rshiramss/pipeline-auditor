"""Triage: one investigation per discrepancy, human gate after.

Two phases (BUILD_PLAN §2.2):
  1. Investigate -- a create_agent ReACT loop over four read-only tools
     decides for itself what evidence it needs.
  2. Judge -- with_structured_output(TriageJudgment) over the investigation
     transcript emits the validated judgment.

The agent's autonomy lives entirely in investigation; nothing here sends or
writes anything. An offline path (--no-llm) maps severity straight from
rules.yaml so the pipeline runs end-to-end without an API key.
"""

from __future__ import annotations

import os

from pydantic import ValidationError

from auditor.agent_tools import InvestigationBudget, make_tools
from auditor.models import (
    CanonicalPipeline,
    Discrepancy,
    DiscrepancyType,
    FixType,
    ProposedFix,
    Rules,
    Severity,
    TriageJudgment,
    TriageResult,
)

# The four prompt rules are design, verbatim from the SPEC.
SYSTEM_PROMPT = """You are an auditor investigating one discrepancy between a
hiring pipeline's ATS (source of truth) and its Slack/Notion tracking log.

Investigate using the tools, then conclude. Rules:
1. Cite only evidence you actually retrieved.
2. insufficient_evidence is a legal answer; inference is not.
3. Drafts are drafts -- never assume they will be sent.
4. Stop investigating when more evidence would not change the judgment.

Severity meanings: lag = minor bookkeeping drift; attention = someone should
act this week; urgent = candidate experience or data integrity at risk now.
"""

JUDGE_PROMPT = """Based on the investigation transcript below, emit your
judgment for the discrepancy. Cite only refs that appear in the transcript
(e.g. 'c_017.stage', 'tracking_log[4].stage_tag'). The proposed fix must be a
draft: either a Slack message draft (slack_draft) or a suggested ATS field
change (ashby_update_suggestion). It will be reviewed by a human before
anything happens.

Discrepancy: {summary}

Investigation transcript:
{transcript}
"""


def offline_triage(d: Discrepancy, rules: Rules) -> TriageResult:
    """--no-llm path: severity from rules.yaml, templated fix draft."""
    severity = rules.severity_weights.get(d.type, Severity.ATTENTION)
    fix_type = (FixType.ASHBY_UPDATE_SUGGESTION
                if d.type in (DiscrepancyType.STAGE_MISMATCH,
                              DiscrepancyType.OWNER_GAP,
                              DiscrepancyType.DUPLICATE_SUSPECT)
                else FixType.SLACK_DRAFT)
    content = (f"[draft] {d.summary}. Please verify and update the "
               f"{'ATS record' if fix_type == FixType.ASHBY_UPDATE_SUGGESTION else 'thread'}.")
    return TriageResult(
        severity=severity,
        explanation=f"(offline triage) {d.summary}",
        proposed_fix=ProposedFix(type=fix_type, content=content),
        evidence_cited=[e.ref for e in d.evidence],
        investigation_trace=["offline: severity from rules.severity_weights"],
    )


def _transcript_text(messages) -> str:
    """Flatten the agent's message history for the judge."""
    lines = []
    for m in messages:
        kind = type(m).__name__
        content = m.content if isinstance(m.content, str) else str(m.content)
        calls = getattr(m, "tool_calls", None)
        if calls:
            for c in calls:
                lines.append(f"[tool call] {c['name']}({c.get('args')})")
        if content.strip():
            lines.append(f"[{kind}] {content.strip()}")
    return "\n".join(lines)


def investigate(d: Discrepancy, pipeline: CanonicalPipeline, model,
                rules: Rules) -> tuple[str, InvestigationBudget]:
    """Phase 1: the agent pulls whatever evidence it decides it needs."""
    from langchain.agents import create_agent

    budget = InvestigationBudget(max_calls=rules.llm.max_tool_calls)
    agent = create_agent(model, tools=make_tools(pipeline, budget),
                         system_prompt=SYSTEM_PROMPT)
    result = agent.invoke(
        {"messages": [("user",
                       f"Discrepancy ({d.type.value}): {d.summary}\n"
                       f"Candidates involved: {d.candidates_involved}\n"
                       f"Initial evidence refs: {[e.ref for e in d.evidence]}")]},
        config={"recursion_limit": rules.llm.recursion_limit},
    )
    return _transcript_text(result["messages"]), budget


def judge(d: Discrepancy, transcript: str, model) -> TriageJudgment:
    """Phase 2: structured judgment over the transcript."""
    structured = model.with_structured_output(TriageJudgment)
    return TriageJudgment.model_validate(
        structured.invoke(JUDGE_PROMPT.format(summary=d.summary,
                                              transcript=transcript)))


def triage_discrepancy(d: Discrepancy, pipeline: CanonicalPipeline,
                       model, rules: Rules) -> TriageResult:
    """Investigate + judge one discrepancy. Never raises: a persistent
    failure becomes insufficient_evidence, not a crashed batch."""
    transcript, budget = investigate(d, pipeline, model, rules)
    try:
        judgment = judge(d, transcript, model)
    except (ValidationError, ValueError) as first_error:
        retry_transcript = (f"{transcript}\n\n[validator] Your previous "
                            f"judgment failed validation: {first_error}. "
                            f"Emit a corrected judgment.")
        try:
            judgment = judge(d, retry_transcript, model)
        except (ValidationError, ValueError) as second_error:
            return TriageResult(
                severity=Severity.INSUFFICIENT_EVIDENCE,
                explanation=f"Triage output failed validation twice: {second_error}",
                proposed_fix=ProposedFix(type=FixType.SLACK_DRAFT,
                                         content=f"[draft] Manual review needed: {d.summary}"),
                evidence_cited=[e.ref for e in d.evidence],
                investigation_trace=budget.trace,
            )
    return TriageResult(
        severity=judgment.severity,
        explanation=judgment.explanation,
        proposed_fix=judgment.proposed_fix,
        evidence_cited=judgment.evidence_cited,
        investigation_trace=budget.trace,  # actual calls, not model claims
    )


def build_model(rules: Rules):
    """Live Claude model, or None when no API key is configured."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    from langchain.chat_models import init_chat_model
    return init_chat_model(rules.llm.model)


def triage_all(discrepancies: list[Discrepancy], pipeline: CanonicalPipeline,
               rules: Rules, use_llm: bool = True,
               model=None, on_progress=None) -> list[TriageResult]:
    """Triage every discrepancy; falls back to offline mode without a key."""
    if use_llm and model is None:
        model = build_model(rules)
    results = []
    for d in discrepancies:
        if on_progress:
            on_progress(d)
        if use_llm and model is not None:
            results.append(triage_discrepancy(d, pipeline, model, rules))
        else:
            results.append(offline_triage(d, rules))
    return results
