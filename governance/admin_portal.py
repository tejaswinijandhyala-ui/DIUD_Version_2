"""
governance/admin_portal.py — the functions behind the admin review screen.

This is intentionally thin: check the admin token, then call straight
through to approval_queue.py. No new decision logic lives here that
isn't already in that file — this is just the safe, authenticated
doorway to it, the same pattern DIUD already uses with DEV_ADMIN_TOKEN.
"""

import os
from governance.approval_queue import (
    get_pending_suggestions,
    get_rejected_suggestions,
    get_suggestion_by_id,
    decide_suggestion,
)

ADMIN_TOKEN = os.environ.get("DEV_ADMIN_TOKEN", "")


class NotAuthorized(Exception):
    """Raised when the token check fails — a clear, specific error
    instead of a generic exception, so the API layer can turn this into
    a proper 401 response."""
    pass


def _check_token(token: str) -> None:
    if not ADMIN_TOKEN or token != ADMIN_TOKEN:
        raise NotAuthorized("Invalid or missing admin token.")


def list_pending(token: str) -> list:
    """Everything currently waiting for review."""
    _check_token(token)
    return get_pending_suggestions()


def list_rejected(token: str, suggestion_type: str = None) -> list:
    """
    The full rejected history with reasons — this is what makes
    principle 2 (rejected suggestions persist) actually visible to an
    admin, not just true in the database.
    """
    _check_token(token)
    return get_rejected_suggestions(suggestion_type)


def view_suggestion(token: str, suggestion_id: str):
    """
    Full detail on one suggestion, including its supporting evidence.
    This is the 'view evidence, compare current vs suggested' step from
    the governance flow — in its simplest form, just reading the
    suggestion and letting a human inspect it before deciding anything.
    """
    _check_token(token)
    return get_suggestion_by_id(suggestion_id)


def approve(token: str, suggestion_id: str, admin_name: str, reason: str = "") -> None:
    """Approves a suggestion. A reason is optional here — approving
    something is generally self-explanatory — but always recommended."""
    _check_token(token)
    decide_suggestion(
        suggestion_id,
        decision="approved",
        decided_by=admin_name,
        reason=reason or "Approved.",
    )


def reject(token: str, suggestion_id: str, admin_name: str, reason: str) -> None:
    """
    Rejects a suggestion. Notice `reason` has no default value here —
    that's deliberate, on top of approval_queue.py already enforcing it.
    It should be impossible to even call this function without deciding
    why, not just impossible for the database to accept it without one.
    """
    _check_token(token)
    decide_suggestion(suggestion_id, decision="rejected", decided_by=admin_name, reason=reason)
