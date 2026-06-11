"""Triage loop tests with a scripted fake chat model -- no API calls.

Covers: happy path (investigate -> judge), budget exhaustion, validation
retry, second-failure fallback to insufficient_evidence, offline mode.
"""

from datetime import datetime, timedelta, timezone
from itertools import cycle

import pytest
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from auditor.agent_tools import BUDGET_EXHAUSTED_MSG, InvestigationBudget, make_tools
from auditor.models import (
    Candidate,
    CanonicalPipeline,
    Discrepancy,
    DiscrepancyType,
    EvidenceRef,
    FixType,
    ProposedFix,
    Rules,
    Severity,
    Stage,
    StageEvent,
    TriageJudgment,
)
from auditor.triage import offline_triage, triage_discrepancy

AS_OF = datetime(2026, 6, 8, 9, 0, tzinfo=timezone.utc)
RULES = Rules()


def make_pipeline() -> CanonicalPipeline:
    ts = AS_OF - timedelta(days=2)
    c = Candidate(id="c_001", name="Priya Sharma", role="FDE",
                  stage=Stage.SCREEN_SCHEDULED,
                  stage_history=[StageEvent(stage=Stage.SCREEN_SCHEDULED, timestamp=ts)],
                  owner="erin", last_updated=ts)
    return CanonicalPipeline(candidates={"c_001": c}, entries=[], as_of=AS_OF)


def make_discrepancy() -> Discrepancy:
    return Discrepancy(
        type=DiscrepancyType.STAGE_MISMATCH,
        candidates_involved=["c_001"],
        summary="Priya Sharma: ATS says screen_scheduled, tracking says onsite_done",
        evidence=[EvidenceRef(source="ashby", ref="c_001.stage", field="stage",
                              value="screen_scheduled")],
        detected_at=AS_OF,
    )


GOOD_JUDGMENT = TriageJudgment(
    severity=Severity.ATTENTION,
    explanation="Tracking is ahead of the ATS by two stages.",
    proposed_fix=ProposedFix(type=FixType.ASHBY_UPDATE_SUGGESTION,
                             content="[draft] Advance c_001 to onsite_done."),
    evidence_cited=["c_001.stage"],
)


class ScriptedModel(GenericFakeChatModel):
    """Fake chat model: scripted investigation turns + scripted judge outputs."""

    judge_outputs: list = []

    def __init__(self, messages, judge_outputs):
        super().__init__(messages=iter(messages))
        # pydantic model -- set after init via object.__setattr__
        object.__setattr__(self, "judge_outputs", list(judge_outputs))

    def bind_tools(self, tools, **kwargs):
        return self

    def with_structured_output(self, schema, **kwargs):
        outputs = self.judge_outputs

        def next_output(_input):
            result = outputs.pop(0)
            if isinstance(result, Exception):
                raise result
            return result

        return RunnableLambda(next_output)


def investigation_script():
    """One tool call, then a concluding message."""
    return [
        AIMessage(content="", tool_calls=[{
            "name": "lookup_candidate", "args": {"candidate_id": "c_001"},
            "id": "call_1", "type": "tool_call"}]),
        AIMessage(content="Evidence gathered; ATS genuinely lags tracking."),
    ]


class TestTriageHappyPath:
    def test_returns_validated_result_with_real_trace(self):
        model = ScriptedModel(investigation_script(), [GOOD_JUDGMENT])
        result = triage_discrepancy(make_discrepancy(), make_pipeline(), model, RULES)
        assert result.severity == Severity.ATTENTION
        assert result.proposed_fix.type == FixType.ASHBY_UPDATE_SUGGESTION
        # trace comes from the budget wrapper, not the model's claims
        assert result.investigation_trace == ["lookup_candidate(c_001)"]

    def test_dict_judgment_is_validated_into_model(self):
        model = ScriptedModel(investigation_script(),
                              [GOOD_JUDGMENT.model_dump(mode="json")])
        result = triage_discrepancy(make_discrepancy(), make_pipeline(), model, RULES)
        assert result.severity == Severity.ATTENTION


class TestValidationRetry:
    def test_first_failure_retries_and_succeeds(self):
        model = ScriptedModel(investigation_script(),
                              [ValueError("bad json"), GOOD_JUDGMENT])
        result = triage_discrepancy(make_discrepancy(), make_pipeline(), model, RULES)
        assert result.severity == Severity.ATTENTION

    def test_second_failure_falls_back_to_insufficient_evidence(self):
        model = ScriptedModel(investigation_script(),
                              [ValueError("bad"), ValueError("still bad")])
        result = triage_discrepancy(make_discrepancy(), make_pipeline(), model, RULES)
        assert result.severity == Severity.INSUFFICIENT_EVIDENCE
        assert "validation" in result.explanation.lower()
        # the batch never crashes; evidence falls back to the detector's refs
        assert result.evidence_cited == ["c_001.stage"]


class TestBudget:
    def test_tools_refuse_past_cap_and_trace_stops(self):
        pipeline = make_pipeline()
        budget = InvestigationBudget(max_calls=2)
        lookup = make_tools(pipeline, budget)[0]
        first = lookup.invoke({"candidate_id": "c_001"})
        second = lookup.invoke({"candidate_id": "c_001"})
        third = lookup.invoke({"candidate_id": "c_001"})
        assert "Priya" in first and "Priya" in second
        assert third == BUDGET_EXHAUSTED_MSG.format(max=2)
        assert len(budget.trace) == 2

    def test_unknown_id_spends_budget_but_returns_message(self):
        budget = InvestigationBudget(max_calls=8)
        lookup = make_tools(make_pipeline(), budget)[0]
        assert "No ATS record" in lookup.invoke({"candidate_id": "c_999"})
        assert budget.used == 1


class TestApiFailureFallback:
    def test_api_error_downgrades_batch_to_offline_instead_of_crashing(self):
        class DeadModel:
            def bind_tools(self, tools, **kwargs):
                return self

            def invoke(self, *args, **kwargs):
                raise RuntimeError("401 invalid_api_key")

        from auditor.triage import triage_all

        discrepancies = [make_discrepancy(), make_discrepancy()]
        results = triage_all(discrepancies, make_pipeline(), RULES,
                             use_llm=True, model=DeadModel())
        assert len(results) == 2
        assert "LLM unavailable" in results[0].explanation
        # second item never touches the dead API: plain offline triage
        assert results[1].explanation.startswith("(offline triage)")
        assert all(r.severity == Severity.ATTENTION for r in results)


class TestOfflineTriage:
    def test_severity_comes_from_rules(self):
        rules = Rules(severity_weights={DiscrepancyType.STAGE_MISMATCH: Severity.URGENT})
        result = offline_triage(make_discrepancy(), rules)
        assert result.severity == Severity.URGENT
        assert result.evidence_cited == ["c_001.stage"]

    def test_unmapped_type_defaults_to_attention(self):
        result = offline_triage(make_discrepancy(), Rules())
        assert result.severity == Severity.ATTENTION

    def test_draft_is_visibly_a_draft(self):
        result = offline_triage(make_discrepancy(), Rules())
        assert result.proposed_fix.content.startswith("[draft]")
