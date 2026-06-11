"""Slack push: channel stamping at queue build, gated posting on approve."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from auditor.models import (
    Candidate,
    CanonicalPipeline,
    Discrepancy,
    DiscrepancyType,
    EvidenceRef,
    FixType,
    ProposedFix,
    QueueItem,
    ResolutionMethod,
    ResolvedEntry,
    Rules,
    Severity,
    Stage,
    StageEvent,
    TrackingEntry,
    TriageResult,
)
from auditor.queue import build_queue_items, resolve_channel
from auditor.slack_push import post_draft

AS_OF = datetime(2026, 6, 8, 9, 0, tzinfo=timezone.utc)
RULES = Rules()


def make_pipeline() -> CanonicalPipeline:
    ts = AS_OF - timedelta(days=2)
    c = Candidate(id="c_001", name="Priya Sharma", role="Growth Strategist",
                  stage=Stage.SCREEN_SCHEDULED,
                  stage_history=[StageEvent(stage=Stage.SCREEN_SCHEDULED, timestamp=ts)],
                  owner="erin", last_updated=ts)
    ghost_entry = ResolvedEntry(
        entry=TrackingEntry(candidate_name="Totally Unknown", stage_tag="applied",
                            timestamp=ts, channel="#hiring-fde"),
        candidate_id=None, method=ResolutionMethod.UNRESOLVED)
    return CanonicalPipeline(candidates={"c_001": c}, entries=[ghost_entry],
                             as_of=AS_OF)


def make_discrepancy(parties: list[str]) -> Discrepancy:
    return Discrepancy(type=DiscrepancyType.STAGE_MISMATCH,
                       candidates_involved=parties, summary="test summary",
                       evidence=[EvidenceRef(source="ashby", ref="c_001.stage",
                                             field="stage", value="x")],
                       detected_at=AS_OF)


def make_triage() -> TriageResult:
    return TriageResult(severity=Severity.ATTENTION, explanation="x",
                        proposed_fix=ProposedFix(type=FixType.SLACK_DRAFT,
                                                 content="[draft] hello"),
                        evidence_cited=["c_001.stage"])


class TestChannelStamping:
    def test_role_channel_from_candidate(self):
        channel = resolve_channel(make_discrepancy(["c_001"]), make_pipeline(), RULES)
        assert channel == "#hiring-growth-strategist"

    def test_tracking_ghost_uses_logged_channel(self):
        channel = resolve_channel(make_discrepancy(["Totally Unknown"]),
                                  make_pipeline(), RULES)
        assert channel == "#hiring-fde"

    def test_unknown_party_falls_back_to_default(self):
        channel = resolve_channel(make_discrepancy(["nobody"]), make_pipeline(), RULES)
        assert channel == "#hiring-ops"

    def test_build_queue_items_stamps_channel(self):
        items = build_queue_items([make_discrepancy(["c_001"])], [make_triage()],
                                  pipeline=make_pipeline(), rules=RULES)
        assert items[0].slack_channel == "#hiring-growth-strategist"

    def test_build_without_pipeline_leaves_channel_unset(self):
        items = build_queue_items([make_discrepancy(["c_001"])], [make_triage()])
        assert items[0].slack_channel is None


class TestPostDraft:
    def make_item(self) -> QueueItem:
        return QueueItem(fingerprint="fp-1", discrepancy=make_discrepancy(["c_001"]),
                         triage=make_triage(), slack_channel="#hiring-fde")

    def test_posts_draft_to_stamped_channel(self, monkeypatch):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        fake_client = MagicMock()
        with patch("slack_sdk.WebClient", return_value=fake_client) as ctor:
            channel = post_draft(self.make_item())
        ctor.assert_called_once_with(token="xoxb-test")
        kwargs = fake_client.chat_postMessage.call_args.kwargs
        assert kwargs["channel"] == "#hiring-fde"
        assert "[draft] hello" in kwargs["text"]
        assert "approved via pipeline-auditor" in kwargs["text"]
        assert channel == "#hiring-fde"

    def test_missing_role_channel_falls_back_with_note(self, monkeypatch):
        from slack_sdk.errors import SlackApiError

        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        fake_client = MagicMock()
        not_found = SlackApiError("nope", response={"error": "channel_not_found"})
        fake_client.chat_postMessage.side_effect = [not_found, {"ok": True}]
        with patch("slack_sdk.WebClient", return_value=fake_client):
            channel = post_draft(self.make_item(), fallback_channel="#hiring-ops")
        assert channel == "#hiring-ops"
        retry_kwargs = fake_client.chat_postMessage.call_args_list[1].kwargs
        assert retry_kwargs["channel"] == "#hiring-ops"
        assert "intended channel #hiring-fde" in retry_kwargs["text"]

    def test_other_slack_errors_propagate(self, monkeypatch):
        from slack_sdk.errors import SlackApiError

        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        fake_client = MagicMock()
        fake_client.chat_postMessage.side_effect = SlackApiError(
            "nope", response={"error": "invalid_auth"})
        with patch("slack_sdk.WebClient", return_value=fake_client):
            try:
                post_draft(self.make_item(), fallback_channel="#hiring-ops")
                raise AssertionError("expected SlackApiError")
            except SlackApiError:
                pass
        assert fake_client.chat_postMessage.call_count == 1  # no retry

    def test_missing_channel_raises(self, monkeypatch):
        monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
        item = self.make_item().model_copy(update={"slack_channel": None})
        try:
            post_draft(item)
            raise AssertionError("expected ValueError")
        except ValueError:
            pass
