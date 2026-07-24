"""
retry_policy.py — the one place in the whole system that knows the answer
to "have we tried too many times already?"

Why this exists as its own file instead of being scattered inside each
validator: if every node decided its own retry limit, you'd end up with
three slightly different retry rules hiding in three different files,
and no single place to change "actually, let's allow 3 tries" later.
This file is the single source of truth for retry limits, the same way
the Rules Book is the single source of truth for business logic.
"""

from graph.state import GraphState

# How many times each stage is allowed to try again before giving up.
# These numbers are deliberately small — retries cost time and money,
# and a question that fails three times in a row is very unlikely to
# succeed on a fourth blind attempt.
MAX_SQL_RETRIES = 2
MAX_DATA_RETRIES = 2
MAX_RESPONSE_RETRIES = 1


def can_retry_sql(state: GraphState) -> bool:
    """True if the SQL generation step is still allowed to try again."""
    return state.get("sql_retry_count", 0) < MAX_SQL_RETRIES


def can_retry_data(state: GraphState) -> bool:
    """True if we're still allowed to regenerate SQL after a bad data result."""
    return state.get("data_retry_count", 0) < MAX_DATA_RETRIES


def can_retry_response(state: GraphState) -> bool:
    """True if the response-writing step is still allowed to try again."""
    return state.get("response_retry_count", 0) < MAX_RESPONSE_RETRIES


def exit_sql_failed(state: GraphState) -> GraphState:
    """
    Runs when SQL generation has failed validation too many times.
    Sets the honest exit message so the user gets an explanation instead
    of a silent empty response.
    """
    state["response"] = exhausted_message("sql")
    state["final_status"] = "failed"
    return state


def exit_data_failed(state: GraphState) -> GraphState:
    """Runs when the query kept returning bad data after retries."""
    state["response"] = exhausted_message("data")
    state["final_status"] = "failed"
    return state


def exit_response_failed(state: GraphState) -> GraphState:
    """
    Runs when the written response couldn't be made to match the data
    after a retry. Note this is a softer failure than the other two —
    we DO have validated data at this point, just not a trustworthy
    narrative about it, so this is marked "partial_success" rather than
    a full failure.
    """
    state["response"] = exhausted_message("response")
    state["final_status"] = "partial_success"
    return state


def exhausted_message(stage: str) -> str:
    """
    The safe, honest thing to tell the user when retries run out.

    This is the "safe, user-friendly exit path" from the design principle
    we agreed on: never a fabricated answer, never a silent failure —
    always a plain explanation of what happened and what to try next.
    """
    messages = {
        "sql": (
            "I wasn't able to build a safe, valid query for this question "
            "after a couple of attempts. Could you rephrase it, or narrow "
            "down the metric, dimension, or time period you're asking about?"
        ),
        "data": (
            "The query ran successfully, but the results didn't pass our "
            "data quality checks after a couple of attempts. This can mean "
            "the underlying data needs a closer look, or the question needs "
            "to be more specific."
        ),
        "response": (
            "I have a validated answer, but couldn't produce a written "
            "explanation that fully matches the data. Here are the raw, "
            "validated numbers instead, without the narrative summary."
        ),
    }
    return messages.get(
        stage,
        "Something went wrong and I couldn't complete this safely. Please try again.",
    )
