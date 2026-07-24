"""
api/routes.py — the HTTP surface of the whole system.

This file is intentionally thin. It doesn't contain any business logic,
any prompts, or any validation rules — all of that already lives in
graph/, agents/, and tools/. This file's only job is the plumbing every
API needs: parse the incoming request, load conversation history, run
the graph, save the result, and shape the response as JSON.

If you ever find yourself wanting to add a business rule or a new check
inside this file, that's usually a sign it belongs somewhere else —
this file should stay boring.
"""

import uuid
from typing import Optional
import anthropic
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from graph.graph import compiled_graph
from graph.state import new_state
from memory.session_store import get_conversation_history, save_turn, save_query_result
from observability.logger import log_final_outcome
from governance.feedback_store import record_feedback, VALID_FEEDBACK_TYPES

router = APIRouter()


class ChatRequest(BaseModel):
    question: str
    conversation_id: Optional[str] = None  # omit this to start a new conversation
    include_sql: bool = False  # PRD's "Explain SQL" feature — off by default


class ChatResponse(BaseModel):
    conversation_id: str
    response: str
    visualization: Optional[dict] = None
    sql: Optional[str] = None
    status: str


def _extract_anthropic_error_message(exc: anthropic.APIStatusError) -> str:
    """
    Pulls the actual human-readable message out of an Anthropic API
    error -- rate limits, usage caps, auth problems, overloaded model,
    etc. These are genuinely useful and safe to show directly: they're
    not internal implementation details (no stack trace, no file paths,
    no database connection info), they're actionable information about
    why the request didn't go through. Falls back to a plain string
    conversion if the error body isn't shaped the way we expect, so this
    never itself raises trying to be helpful.
    """
    try:
        body = exc.body
        if isinstance(body, dict):
            inner = body.get("error", {})
            if isinstance(inner, dict) and inner.get("message"):
                return inner["message"]
    except Exception:
        pass
    return str(exc)


@router.post("/chat", response_model=ChatResponse)
def chat(request: ChatRequest) -> ChatResponse:
    """
    The single entry point for asking the copilot a question.

    Everything heavier than "parse the request and call the graph"
    belongs inside the graph itself, not here — this function's job is
    just to connect an HTTP request to the pipeline we already built.
    """
    if not request.question or not request.question.strip():
        raise HTTPException(status_code=400, detail="Question cannot be empty.")

    conversation_id = request.conversation_id or str(uuid.uuid4())
    history = get_conversation_history(conversation_id)

    state = new_state(
        question=request.question,
        conversation_id=conversation_id,
        conversation_history=history,
    )

    try:
        result_state = compiled_graph.invoke(state)
    except anthropic.APIStatusError as e:
        # A real, specific error from Claude's API (usage limits, auth
        # issues, rate limits, an overloaded model) -- these are safe
        # and useful to show the user directly, since they explain
        # exactly what to do next (e.g. "you'll regain access on
        # 2026-08-01"), unlike an internal Python exception which could
        # contain anything.
        raise HTTPException(status_code=502, detail=_extract_anthropic_error_message(e)) from e
    except Exception as e:
        # Everything else -- a genuine bug, a ClickHouse connection
        # error, an unexpected crash -- stays hidden behind a generic
        # message. These CAN contain internal details (file paths,
        # connection strings, stack traces) that shouldn't reach a user,
        # so this is the one place we deliberately don't show the real
        # error text. It's still fully visible in Render's logs.
        raise HTTPException(
            status_code=500,
            detail="Something went wrong while processing this question.",
        ) from e

    # One summary row per completed question — logged_node() already
    # captured every individual step; this is the "how did the whole
    # question go" row that's actually useful to check first.
    log_final_outcome(result_state)

    # Save both sides of the exchange for next time, regardless of
    # whether this turn succeeded, failed safely, or asked for clarification —
    # even a clarifying question is part of the conversation's history.
    save_turn(conversation_id, "user", request.question)
    save_turn(conversation_id, "assistant", result_state.get("response", ""))

    # If this question produced real, validated data, keep it around so
    # a follow-up "export this as CSV" in the same conversation has
    # something to work with, without re-running the query.
    query_result = result_state.get("query_result")
    if query_result:
        columns = list(query_result[0].keys()) if query_result else []
        save_query_result(conversation_id, result_state.get("sql", ""), columns, query_result)

    return ChatResponse(
        conversation_id=conversation_id,
        response=result_state.get("response", ""),
        visualization=result_state.get("visualization") or None,
        sql=result_state.get("sql") if request.include_sql else None,
        status=result_state.get("final_status", "success"),
    )


class FeedbackRequest(BaseModel):
    conversation_id: str
    question: str
    sql: str = ""
    response: str
    feedback_type: str  # one of VALID_FEEDBACK_TYPES
    feedback_note: str = ""
    user_id: str = ""


@router.post("/feedback")
def feedback(request: FeedbackRequest):
    """
    Where every 👍, 👎, "wrong SQL," or "suggest a rule" button click
    actually goes. This is a thin wrapper around
    governance/feedback_store.record_feedback() — same principle as the
    rest of this file, no logic here that doesn't already live elsewhere.
    """
    if request.feedback_type not in VALID_FEEDBACK_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"feedback_type must be one of {VALID_FEEDBACK_TYPES}",
        )

    feedback_id = record_feedback(
        conversation_id=request.conversation_id,
        question=request.question,
        sql=request.sql,
        query_result_summary="",
        response=request.response,
        feedback_type=request.feedback_type,
        feedback_note=request.feedback_note,
        user_id=request.user_id,
    )
    return {"feedback_id": feedback_id, "status": "recorded"}
