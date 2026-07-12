"""
governance/feedback_store.py -- in-memory feedback log, per the updated
architecture: "For Phase 1, feedback persistence is not required. If
feedback collection is introduced later, use a lightweight operational
database instead of ClickHouse."

This is actually a reversion to how your original DIUD already worked
(_FEEDBACK_LOG was always an in-memory list there, never ClickHouse --
that was something added in an earlier pass that this change now
correctly walks back). Every entry is also written to Python's logging
module, so it shows up in Render's logs even without any database.

HONEST TRADEOFF: feedback is lost on restart, same as conversation
history. For a Phase 1 read-only copilot, that's an accepted cost, not
an oversight -- see memory/session_store.py's docstring for the same
reasoning applied here.
"""

import logging
import time
import uuid
from typing import List

logger = logging.getLogger("ri_copilot.feedback")

VALID_FEEDBACK_TYPES = ["correct", "incorrect", "better_explanation", "wrong_sql", "suggest_rule"]

_FEEDBACK_LOG: List[dict] = []  # most-recent-first, capped
_FEEDBACK_LOG_CAP = 500


def record_feedback(
    conversation_id: str,
    question: str,
    sql: str,
    query_result_summary: str,
    response: str,
    feedback_type: str,
    feedback_note: str = "",
    user_id: str = "",
) -> str:
    """
    Records one feedback event in this process's memory, and logs it so
    it's visible in Render's logs regardless of whether anything ever
    reads the in-memory list back.
    """
    if feedback_type not in VALID_FEEDBACK_TYPES:
        raise ValueError(f"Unknown feedback_type: {feedback_type}")

    feedback_id = str(uuid.uuid4())
    entry = {
        "feedback_id": feedback_id,
        "conversation_id": conversation_id,
        "question": question,
        "sql": sql,
        "query_result_summary": query_result_summary,
        "response": response,
        "feedback_type": feedback_type,
        "feedback_note": feedback_note,
        "user_id": user_id,
        "created_at": int(time.time()),
    }
    _FEEDBACK_LOG.insert(0, entry)
    del _FEEDBACK_LOG[_FEEDBACK_LOG_CAP:]

    logger.info(
        "feedback recorded type=%s conversation=%s question=%r note=%r",
        feedback_type, conversation_id, question[:120], feedback_note[:200],
    )
    return feedback_id


def get_recent_feedback(limit: int = 200) -> list:
    """Reads from this process's in-memory log -- used by
    governance/audit_log.py to look for patterns within the current
    session's traffic, not across restarts."""
    return _FEEDBACK_LOG[:limit]
