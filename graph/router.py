"""
router.py — the traffic signals of the pipeline.

Every node in the graph does one job and hands control back. This file is
where all the "okay, now what?" decisions live — should we ask for
clarification, retry, move forward, or give up safely? Keeping every
routing decision in one file (instead of scattered inside each agent)
means you can read this one file top to bottom and understand the entire
shape of the pipeline without opening ten other files.
"""

from graph.state import GraphState
from graph.retry_policy import can_retry_sql, can_retry_data, can_retry_response


def route_after_intent(state: GraphState) -> str:
    """
    Right after we figure out what the user meant: if it's ambiguous,
    go ask a clarifying question instead of guessing. Otherwise, move on
    to retrieving the business rules.
    """
    if state.get("needs_clarification"):
        return "ask_clarification"
    return "retrieve_rules"


def route_after_sql_validation(state: GraphState) -> str:
    """
    Right after checking the generated SQL for correctness.

    Three possible outcomes:
    1. SQL is valid -> run it against ClickHouse.
    2. SQL is invalid, but we haven't hit the retry limit -> try generating it again.
    3. SQL is invalid and we're out of retries -> stop safely, don't guess.
    """
    if state.get("sql_valid"):
        return "execute_query"
    if can_retry_sql(state):
        return "generate_sql"
    return "sql_failed_exit"


def route_after_data_validation(state: GraphState) -> str:
    """
    Right after checking the query's *results* for quality problems
    (empty results, duplicates, impossible values, and so on).

    Note this loops back to SQL generation, not just "try running the
    same query again" — bad data usually means the query itself was
    wrong in a way syntax-checking couldn't catch.
    """
    if state.get("data_valid"):
        return "generate_insight"
    if can_retry_data(state):
        return "generate_sql"
    return "data_failed_exit"


def route_after_response_validation(state: GraphState) -> str:
    """
    Right after checking that the final written response actually matches
    the validated numbers. This is the last safety net before anything
    reaches the user.
    """
    if state.get("response_valid"):
        return "return_response"
    if can_retry_response(state):
        return "generate_insight"
    return "response_failed_exit"
