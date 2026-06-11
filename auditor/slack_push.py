"""Post a human-approved draft to Slack. The app's only Slack transmit path.

This runs strictly behind the approval gate: the human has already disposed;
this executes that recorded decision and nothing else. Mirrors notion_push.py:
env-gated (SLACK_BOT_TOKEN), output-only, failures isolated by the caller.

Transport: Web API chat.postMessage via slack_sdk.WebClient (bot token,
chat:write + chat:write.public scopes) -- chosen over incoming webhooks so one
token can route per-role channels (#hiring-fde, ...). See
slack_docs/reference/methods/chat.postmessage.md.
"""

from __future__ import annotations

import os

from auditor.models import QueueItem


def is_configured() -> bool:
    return bool(os.environ.get("SLACK_BOT_TOKEN"))


def post_draft(item: QueueItem, fallback_channel: str | None = None) -> str:
    """Post the approved draft to its stamped channel. Returns the channel
    actually posted to. Raises on any Slack failure -- the review loop
    isolates errors so a Slack outage never loses the human's decision.

    If the stamped channel doesn't exist in the workspace (the per-role
    channels come from data and may not be real), retries once into
    fallback_channel with a note naming the intended channel."""
    from slack_sdk import WebClient
    from slack_sdk.errors import SlackApiError

    channel = item.slack_channel
    if not channel:
        raise ValueError(f"queue item {item.fingerprint} has no slack_channel")
    text = (f"{item.triage.proposed_fix.content}\n"
            f"_re: {item.discrepancy.summary} -- approved via pipeline-auditor_")
    client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
    try:
        client.chat_postMessage(channel=channel, text=text, mrkdwn=True)
        return channel
    except SlackApiError as error:
        not_found = error.response.get("error") == "channel_not_found"
        if not (not_found and fallback_channel and fallback_channel != channel):
            raise
        client.chat_postMessage(
            channel=fallback_channel,
            text=f"{text}\n_(intended channel {channel} not found)_",
            mrkdwn=True)
        return fallback_channel
