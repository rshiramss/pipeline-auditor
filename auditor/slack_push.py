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


def post_draft(item: QueueItem) -> str:
    """Post the approved draft to its stamped channel. Returns the channel
    actually posted to. Raises on any Slack failure -- the review loop
    isolates errors so a Slack outage never loses the human's decision."""
    from slack_sdk import WebClient

    channel = item.slack_channel
    if not channel:
        raise ValueError(f"queue item {item.fingerprint} has no slack_channel")
    text = (f"{item.triage.proposed_fix.content}\n"
            f"_re: {item.discrepancy.summary} -- approved via pipeline-auditor_")
    client = WebClient(token=os.environ["SLACK_BOT_TOKEN"])
    client.chat_postMessage(channel=channel, text=text, mrkdwn=True)
    return channel
