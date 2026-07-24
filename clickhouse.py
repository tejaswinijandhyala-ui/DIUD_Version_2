"""
tools/clickhouse.py — deterministic Python, never Claude.

This function has exactly one job: send the validated SQL to ClickHouse
and hand back whatever rows come back. It does not judge whether those
results look right — that's tools/data_validator.py's job, immediately
after this. Keeping "run the query" and "check if the answer makes
sense" as two separate steps means each one stays simple and easy to
reason about on its own.
"""

import os
import json
import requests
from graph.state import GraphState

CLICKHOUSE_URL = os.environ.get("CLICKHOUSE_URL", "https://your-clickhouse-host/query")
CLICKHOUSE_TOKEN = os.environ.get("CLICKHOUSE_TOKEN", "")
REQUEST_TIMEOUT_SECONDS = 30


def execute_query(state: GraphState) -> GraphState:
    """
    Sends the validated SQL to ClickHouse over its HTTP API.

    Only ever runs after validate_sql has already marked the query safe —
    this function trusts that check happened and doesn't repeat it. If it
    tried to re-validate here too, we'd have the same rule living in two
    places, which is exactly what we're trying to avoid throughout this
    codebase.
    """
    sql = state["sql"]

    # Make sure we get rows back as one JSON object per line, which is
    # what the parser below expects. If the SQL Agent already added a
    # FORMAT clause, don't add a second one.
    if "FORMAT" not in sql.upper():
        sql = f"{sql.rstrip(';')} FORMAT JSONEachRow"

    try:
        response = requests.post(
            CLICKHOUSE_URL,
            headers={"Authorization": f"Bearer {CLICKHOUSE_TOKEN}"},
            data=sql,
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        state["query_result"] = _parse_clickhouse_response(response.text)

    except requests.exceptions.Timeout:
        # A timeout isn't a "retry the same query" situation — the query
        # itself is probably too expensive or too broad. We route it
        # through the same data-validation-failure path as a bad result,
        # so there's one consistent place that decides what "give up
        # gracefully" looks like, rather than a second one just for timeouts.
        state.setdefault("errors", []).append("clickhouse_timeout")
        state["query_result"] = []
        state["data_valid"] = False
        state["data_validation_errors"] = ["The query took too long to run and timed out."]

    except requests.exceptions.RequestException as e:
        state.setdefault("errors", []).append(f"clickhouse_connection_error: {e}")
        state["query_result"] = []
        state["data_valid"] = False
        state["data_validation_errors"] = ["Couldn't connect to the database right now."]

    return state


def _parse_clickhouse_response(raw_text: str) -> list:
    """
    ClickHouse's HTTP API with FORMAT JSONEachRow returns one JSON object
    per line, not a single JSON array. This turns that raw text into a
    normal Python list of dictionaries the rest of the pipeline can use.
    """
    rows = []
    for line in raw_text.strip().split("\n"):
        if line:
            rows.append(json.loads(line))
    return rows
