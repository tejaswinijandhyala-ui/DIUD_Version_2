"""
memory/session_store.py -- in-memory conversation state, per the updated
architecture decision: DIUD is a read-only Revenue Intelligence Copilot,
not a system of record. Conversation memory only needs to last as long
as an active chat session, so it lives in this process's memory, not in
a ClickHouse table.

HONEST TRADEOFF, stated plainly: this means conversation history and
cached query results are lost on a server restart or redeploy, and
won't be shared correctly across multiple server instances if this ever
runs behind a load balancer with more than one process. For DIUD's
actual scope -- a single Render web service, conversations that matter
within a session, not across weeks -- that's an acceptable trade, not
an oversight. If that changes later, this file is the one place to
swap in a real store without touching any other file, since every
caller only depends on these four function signatures.
"""

import time
from typing import Dict, List, Optional

MAX_HISTORY_TURNS = 10  # keeps prompts small -- older turns rarely matter to the current question

# conversation_id -> list of {"role": ..., "content": ..., "created_at": ...}
_CONVERSATIONS: Dict[str, List[dict]] = {}

# conversation_id -> {"sql": ..., "columns": ..., "rows": ..., "total_rows": ..., "captured_at": ...}
_LATEST_QUERY_RESULT: Dict[str, dict] = {}


def get_conversation_history(conversation_id: str) -> list:
    """
    Returns the recent turns for a conversation. Returns an empty list
    for a brand new conversation, or one this process hasn't seen (e.g.
    right after a restart) -- both are normal, expected cases, not errors.
    """
    turns = _CONVERSATIONS.get(conversation_id, [])
    return [{"role": t["role"], "content": t["content"]} for t in turns[-MAX_HISTORY_TURNS:]]


def save_turn(conversation_id: str, role: str, content: str) -> None:
    """Appends one turn. Trims stored history so a very long-running
    conversation doesn't grow this process's memory unbounded."""
    turns = _CONVERSATIONS.setdefault(conversation_id, [])
    turns.append({"role": role, "content": content, "created_at": int(time.time())})
    if len(turns) > MAX_HISTORY_TURNS * 4:
        del turns[: len(turns) - MAX_HISTORY_TURNS * 4]


def save_query_result(conversation_id: str, sql: str, columns: List[str], rows: List[Dict]) -> None:
    """
    Keeps the most recent validated query result for a conversation, so
    a same-session "export this as CSV" request has real data to work
    with. Only the latest result matters -- this deliberately overwrites
    rather than accumulating a history, since export always means "the
    data I was just looking at," not an archive.
    """
    _LATEST_QUERY_RESULT[conversation_id] = {
        "sql": sql,
        "columns": columns,
        "rows": rows,
        "total_rows": len(rows),
        "captured_at": int(time.time()),
    }


def get_latest_query_result(conversation_id: str) -> Optional[Dict]:
    """Returns the most recent query result for a conversation, or None
    if there isn't one yet -- a brand new conversation, one that's only
    had general-conversation turns, or one from before a restart."""
    return _LATEST_QUERY_RESULT.get(conversation_id)
