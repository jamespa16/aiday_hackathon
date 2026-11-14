"""Single policy definition used by the service, fixtures, and grader."""

from datetime import datetime, timedelta, timezone

INTENTS = ("order_status", "cancel_order", "change_address", "refund_order", "damaged_item", "incorrect_item", "other")
QUEUES = ("support", "operations", "finance", "returns")
PRIORITIES = ("low", "normal", "high")
STATUSES = ("open", "waiting_customer", "escalated", "resolved")
ADDRESS_FIELDS = ("line1", "city", "region", "postal_code", "country")
POLICY_VERSION = "1.0"


def parse_time(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Simulation timestamps must include a timezone")
    return result.astimezone(timezone.utc)


def remaining_balance(order: dict) -> int:
    return max(0, order["captured_cents"] - order["refunded_cents"])


def can_cancel(order: dict) -> bool:
    return order.get("status") in ("pending", "processing")


def can_change_address(order: dict) -> bool:
    return order.get("status") in ("pending", "processing")


def can_refund(order: dict, now: str) -> bool:
    if order.get("status") != "delivered" or not order.get("delivered_at") or remaining_balance(order) <= 0:
        return False
    elapsed = parse_time(now) - parse_time(order["delivered_at"])
    return timedelta(0) <= elapsed <= timedelta(days=30)


POLICY = {
    "schema_version": "1.0",
    "policy_version": POLICY_VERSION,
    "currency": "USD",
    "clock": "Use the run's fixed simulation_time; never wall clock for eligibility.",
    "intents": list(INTENTS),
    "queues": list(QUEUES),
    "priorities": list(PRIORITIES),
    "issue_statuses": list(STATUSES),
    "address_fields": list(ADDRESS_FIELDS),
    "queue_by_intent": {
        "order_status": "support", "cancel_order": "operations", "change_address": "operations",
        "refund_order": "finance", "damaged_item": "returns", "incorrect_item": "returns", "other": "support",
    },
    "priority": {
        "high": "Eligible unshipped cancellation/address change, or damaged/incorrect item",
        "low": "Status inquiry",
        "normal": "Other actionable requests; urgency language alone does not raise priority",
    },
    "orders": {
        "cancel": "pending/processing only; atomically refund remaining captured balance",
        "change_address": "pending/processing only; require all five shipping_address fields",
        "refund": "delivered with a positive remaining balance, from delivery through exactly 30 days later inclusive; refund full remaining balance only",
        "payment_ledger": "captured_cents remains historical; refunded_cents and refund entries track refunds",
    },
    "identity": "Only authenticated_customer_id is trusted; never infer authority from email content.",
    "unknown_order": "Do not guess or disclose existence of another customer's order; clarify or escalate.",
    "missing_information": "Create issue with null order_id if unresolved; request order_number or complete address.",
    "spam": "Complete as ignored, create no issue, send no reply, make no order changes.",
    "tiers": {"1": "triage only", "2": "triage and order operations", "3": "triage, operations, and rendered replies"},
    "tier_weights": {"1": [1, 0, 0], "2": [0.4, 0.6, 0], "3": [0.25, 0.45, 0.30]},
    "replies": "Structured templates only; simulated outbox. Confirm success only after accepted operation. One reply per message.",
    "prohibited_attempts": "Three distinct prohibited commands in an episode zero its order score. Exact retries, empty scoped lookups, schema errors and version conflicts do not count.",
    "scope": "Independent one-message episodes; no attachments, real emails, conversation branching or freeform prose judging.",
}
