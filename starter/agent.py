"""Model-free, status-only baseline and participant extension hooks."""

from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Any

from aiday.client import Client


def triage_message(message: dict[str, Any], client: Client) -> list[dict[str, Any]]:
    """TODO extension: classify the message and return issue objects.

    The baseline recognizes obvious status questions. It intentionally leaves
    order linkage empty because it does not guess from customer text.
    """
    text = f"{message.get('subject', '')} {message.get('body_text', '')}".lower()
    if not any(term in text for term in ("where is", "order status", "tracking", "delivery status")):
        return []
    return [
        {
            "issue_id": f"status-{message['message_id']}",
            "thread_id": message["thread_id"],
            "source_message_ids": [message["message_id"]],
            "title": "Order status inquiry",
            "intent": "order_status",
            "priority": "low",
            "queue": "support",
            "order_id": None,
            "extracted_fields": {},
            "status": "open",
        }
    ]


def handle_order(message: dict[str, Any], client: Client) -> None:
    """TODO extension: look up the order and perform allowed typed operations."""
    return None


def compose_reply(message: dict[str, Any], client: Client) -> dict[str, Any] | None:
    """TODO extension: return a reply.send payload, or None for silence."""
    return None


def process_message(message: dict[str, Any], client: Client, tier: int | None = None) -> None:
    if tier is None:
        tier = int(client.run()["tier"])
    # Make run configuration available to hooks without changing their simple
    # (message, client) signature.
    client.tier = tier  # type: ignore[attr-defined]
    issues = triage_message(message, client)
    for issue in issues:
        client.issue_upsert(message["message_id"], issue)
    if tier >= 2:
        handle_order(message, client)
    reply_payload = compose_reply(message, client) if tier >= 3 else None
    if reply_payload is not None:
        client.reply(message["message_id"], reply_payload)
    disposition = "processed" if issues or reply_payload is not None else "ignored"
    client.complete(message["message_id"], disposition)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tier", type=int, default=os.environ.get("AIDAY_TIER", "1"),
        help="declared run tier (defaults to AIDAY_TIER, then 1)",
    )
    parser.add_argument("--max-idle-polls", type=int, default=20, help="stop after this many empty inbox polls")
    parser.add_argument("--poll-interval", type=float, default=0.25, help="seconds between empty inbox polls")
    args = parser.parse_args()
    if args.tier not in (1, 2, 3):
        parser.error("--tier must be 1, 2, or 3")
    if args.max_idle_polls < 1 or args.poll_interval < 0:
        parser.error("--max-idle-polls must be positive and --poll-interval cannot be negative")
    base_url = os.environ.get("AIDAY_BASE_URL", "http://127.0.0.1:8000")
    token = os.environ.get("AIDAY_TOKEN")
    if not token:
        parser.error("set AIDAY_TOKEN to the practice run token")
    with Client(base_url, token) as client:
        run_tier = client.run()["tier"]
        if args.tier != run_tier:
            parser.error(f"--tier {args.tier} does not match authenticated run tier {run_tier}")
        client.tier = run_tier  # type: ignore[attr-defined]
        idle_polls = 0
        while True:
            page = client.inbox()
            messages = page.get("messages", [])
            if not messages:
                if page.get("finished", False):
                    break
                idle_polls += 1
                if idle_polls >= args.max_idle_polls:
                    print("Inbox stayed empty; stopping after the configured polling limit.", file=sys.stderr)
                    break
                if args.poll_interval:
                    time.sleep(args.poll_interval)
                continue
            idle_polls = 0
            process_message(messages[0], client, tier=run_tier)


if __name__ == "__main__":
    main()
