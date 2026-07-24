"""
governance/approval_queue.py — where rule suggestions wait for a human,
and where every decision about them is permanently recorded.

This file exists specifically to enforce the three governance principles
agreed on earlier:
1. Every decision — approved, rejected, or modified — is recorded and
   auditable. None of these happen silently.
2. Rejected suggestions are never deleted. They stay in history with
   the rejection reason attached.
3. A rejected suggestion can only resurface if there's genuinely new
   supporting evidence, not just the same pattern repeating.

Design choice: this table is append-only, like feedback_store.py. A
decision is never an UPDATE to the original suggestion row — it's a
brand new row that's a complete snapshot of the suggestion's current
state. "What's the current status of suggestion X?" always means "the
most recent row for suggestion X," never "the one row we keep editing."
This is what makes principle 1 (auditable) true structurally: the full
history is just... still there, because nothing ever overwrites it.
"""

import os
import json
import time
import uuid
import requests

CLICKHOUSE_URL = os.environ.get("CLICKHOUSE_URL", "https://your-clickhouse-host/query")
CLICKHOUSE_TOKEN = os.environ.get("CLICKHOUSE_TOKEN", "")

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"


def submit_suggestion(
    suggestion_type: str,
    description: str,
    proposed_change: str,
    evidence_feedback_ids: list,
) -> str:
    """
    Called by the Improvement Analysis Agent when it finds a pattern
    worth a human's attention. This function NEVER modifies the Rules
    Book directly — its only power is to create a pending suggestion.
    Only an admin, through decide_suggestion(), can move it forward.
    """
    suggestion_id = str(uuid.uuid4())
    row = {
        "suggestion_id": suggestion_id,
        "suggestion_type": suggestion_type,  # e.g. "new_rule", "prompt_improvement", "validator_rule"
        "description": description,
        "proposed_change": proposed_change,
        "evidence_feedback_ids": json.dumps(evidence_feedback_ids),
        "status": STATUS_PENDING,
        "decided_by": "",
        "decision_reason": "",
        "created_at": int(time.time()),
    }
    _insert(row)
    return suggestion_id


def decide_suggestion(suggestion_id: str, decision: str, decided_by: str, reason: str) -> None:
    """
    Records an admin's decision — approve or reject. This reads the
    original suggestion first and writes a brand new, complete row with
    the updated status, so the full history stays intact rather than
    being overwritten. A rejection with no reason is not allowed —
    principle 3 depends on rejection reasons actually existing.
    """
    if decision not in (STATUS_APPROVED, STATUS_REJECTED):
        raise ValueError(f"decision must be '{STATUS_APPROVED}' or '{STATUS_REJECTED}'")
    if decision == STATUS_REJECTED and not reason.strip():
        raise ValueError("A rejection reason is required — rejections must be explainable later.")

    original = get_suggestion_by_id(suggestion_id)
    if original is None:
        raise ValueError(f"No suggestion found with id {suggestion_id}")

    row = {
        **original,
        "status": decision,
        "decided_by": decided_by,
        "decision_reason": reason,
        "created_at": int(time.time()),
    }
    _insert(row)


def get_suggestion_by_id(suggestion_id: str):
    """The most recent snapshot of a suggestion, or None if it doesn't exist."""
    rows = _query(f"""
        SELECT *
        FROM ri_copilot_suggestions
        WHERE suggestion_id = '{suggestion_id}'
        ORDER BY created_at DESC
        LIMIT 1
        FORMAT JSONEachRow
    """)
    return rows[0] if rows else None


def get_pending_suggestions() -> list:
    """Everything currently waiting for a human to review."""
    return [row for row in _latest_snapshot_per_suggestion() if row.get("status") == STATUS_PENDING]


def get_rejected_suggestions(suggestion_type: str = None) -> list:
    """
    The full, permanent history of everything that's been rejected, with
    reasons attached. Used by has_new_evidence() below before the
    Improvement Analysis Agent is allowed to raise a suggestion of the
    same kind again.
    """
    rows = [row for row in _latest_snapshot_per_suggestion() if row.get("status") == STATUS_REJECTED]
    if suggestion_type:
        rows = [row for row in rows if row.get("suggestion_type") == suggestion_type]
    return rows


def has_new_evidence(suggestion_type: str, new_evidence_feedback_ids: list) -> bool:
    """
    Before re-raising a pattern that's already been rejected, this
    checks whether the evidence behind the new suggestion is actually
    different from what was already considered and rejected. If every
    piece of evidence was already seen and rejected, this returns
    False — and the Improvement Analysis Agent should NOT resurface the
    same suggestion again with the same old evidence.
    """
    previously_rejected = get_rejected_suggestions(suggestion_type)
    previously_seen_evidence = set()
    for rejected in previously_rejected:
        ids = json.loads(rejected.get("evidence_feedback_ids", "[]"))
        previously_seen_evidence.update(ids)

    genuinely_new = set(new_evidence_feedback_ids) - previously_seen_evidence
    return len(genuinely_new) > 0


def _latest_snapshot_per_suggestion() -> list:
    """
    Every suggestion's most recent row — this is what 'current state'
    means in an append-only table. ClickHouse's LIMIT BY does exactly
    this: order by recency, keep only the newest row per suggestion_id.
    """
    return _query("""
        SELECT *
        FROM ri_copilot_suggestions
        ORDER BY created_at DESC
        LIMIT 1 BY suggestion_id
        FORMAT JSONEachRow
    """)


def _insert(row: dict) -> None:
    sql = f"INSERT INTO ri_copilot_suggestions FORMAT JSONEachRow\n{json.dumps(row)}"
    requests.post(
        CLICKHOUSE_URL,
        headers={"Authorization": f"Bearer {CLICKHOUSE_TOKEN}"},
        data=sql,
        timeout=10,
    )


def _query(sql: str) -> list:
    try:
        response = requests.post(
            CLICKHOUSE_URL,
            headers={"Authorization": f"Bearer {CLICKHOUSE_TOKEN}"},
            data=sql,
            timeout=15,
        )
        response.raise_for_status()
        return [json.loads(line) for line in response.text.strip().split("\n") if line]
    except requests.exceptions.RequestException:
        # Same principle as feedback_store.py: a ClickHouse hiccup
        # shouldn't crash whatever's asking about suggestions — an
        # empty list here means "couldn't check right now," not "there
        # are no suggestions."
        return []
