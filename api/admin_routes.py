"""
api/admin_routes.py -- the HTTP surface for the admin panel.

Per the updated architecture, this no longer has an approval workflow.
/debug/metrics, /debug/feedback, and /debug/alerts stay as read-only
reports (already backed by in-memory data, not ClickHouse). What
changed is /debug/proposals/draft: it used to submit a suggestion to a
persisted queue with its own approve/reject endpoints; now it just
returns the drafted suggestion directly in the response. There's no
GET /debug/proposals (there's nothing to list -- nothing is stored),
and no approve/reject endpoints (there's nothing to approve -- applying
a suggestion means a human edits rules/rules_book.py and opens a PR).
"""

import os
from typing import Optional
from fastapi import APIRouter, Header, HTTPException

from governance.audit_log import get_audit_metrics, get_feedback_metrics, get_flagged_issues, draft_fix_proposal

router = APIRouter()

ADMIN_TOKEN = os.environ.get("DEV_ADMIN_TOKEN", "")


def _require_admin(x_admin_token: Optional[str] = Header(default=None)):
    """
    Same fail-closed pattern as before: if DEV_ADMIN_TOKEN isn't
    configured at all, the whole admin surface is unavailable (503) --
    never silently left open.
    """
    if not ADMIN_TOKEN:
        raise HTTPException(
            status_code=503,
            detail="Admin surface is not configured. Set DEV_ADMIN_TOKEN in the environment to enable it.",
        )
    if not x_admin_token or x_admin_token != ADMIN_TOKEN:
        raise HTTPException(status_code=401, detail="Invalid or missing admin token.")


@router.get("/debug/metrics")
def debug_metrics(x_admin_token: Optional[str] = Header(default=None)):
    """Rule-violation success rate and failure breakdown."""
    _require_admin(x_admin_token)
    return get_audit_metrics()


@router.get("/debug/feedback")
def debug_feedback(x_admin_token: Optional[str] = Header(default=None)):
    """Thumbs up/down rate, broken down by pattern, plus recent comments."""
    _require_admin(x_admin_token)
    return get_feedback_metrics()


@router.get("/debug/alerts")
def debug_alerts(x_admin_token: Optional[str] = Header(default=None)):
    """Cross-referenced flagged issues -- never acts on anything itself."""
    _require_admin(x_admin_token)
    return get_flagged_issues()


@router.post("/debug/proposals/draft")
def debug_proposals_draft(x_admin_token: Optional[str] = Header(default=None)):
    """
    Drafts a suggested fix for each currently-flagged issue and returns
    them directly -- nothing is stored. Each drafted suggestion is meant
    to be reviewed by a person, then manually applied to
    rules/rules_book.py as a normal, reviewed pull request. Refresh this
    endpoint any time; it always reflects the current flags, not a
    stale queue.
    """
    _require_admin(x_admin_token)
    flags = get_flagged_issues()["flags"]
    drafted = []
    for flag in flags:
        try:
            drafted.append(draft_fix_proposal(flag))
        except Exception as e:
            print(f"Failed to draft proposal for {flag['subject']}: {e}")
    return {"drafted": len(drafted), "proposals": drafted}
