"""
observability/logger.py -- per the updated architecture: "Use standard
application logging instead of storing logs in ClickHouse. Recommended:
Python logging, Render Logs, LangSmith/OpenTelemetry (future
enhancement)."

Same two functions as before (log_event, logged_node) and the same
node-wrapping pattern -- every agent and tool is still automatically
timed and logged without any of those files containing logging code
themselves. The only thing that changed is where the log line goes:
Python's logging module instead of a ClickHouse INSERT. Render captures
stdout/stderr from the running process automatically, so this needs no
extra configuration to show up in the dashboard's Logs tab.
"""

import logging
import time
import functools
import json

logger = logging.getLogger("ri_copilot")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


def log_event(**fields) -> None:
    """
    Writes one observability event as a structured log line. Never
    raises -- a logging failure should never be the reason a real,
    user-facing request fails.
    """
    try:
        logger.info(json.dumps(fields, default=str))
    except Exception:
        pass


def logged_node(node_name: str):
    """
    A decorator that wraps any graph node (an agent or a tool) and
    automatically logs its name, how long it took, and whether it
    raised an error -- without that node's own code needing to know
    anything about logging at all.
    """
    def decorator(func):
        @functools.wraps(func)
        def wrapper(state, *args, **kwargs):
            start = time.time()
            error = None
            try:
                return func(state, *args, **kwargs)
            except Exception as e:
                error = str(e)
                raise
            finally:
                duration_ms = int((time.time() - start) * 1000)
                log_event(
                    node=node_name,
                    conversation_id=state.get("conversation_id", ""),
                    question=state.get("question", ""),
                    intent=state.get("intent", ""),
                    duration_ms=duration_ms,
                    error=error,
                    sql_retry_count=state.get("sql_retry_count", 0),
                    data_retry_count=state.get("data_retry_count", 0),
                    response_retry_count=state.get("response_retry_count", 0),
                )
        return wrapper
    return decorator


def log_final_outcome(state: dict) -> None:
    """
    Called once per request, right after the graph finishes -- a single
    summary line per question, separate from the per-node lines
    logged_node() produces. This is the line worth grepping Render's
    logs for first when checking "how's the system doing."
    """
    log_event(
        event_type="final_outcome",
        conversation_id=state.get("conversation_id", ""),
        question=state.get("question", ""),
        intent=state.get("intent", ""),
        final_status=state.get("final_status", ""),
        sql_retry_count=state.get("sql_retry_count", 0),
        data_retry_count=state.get("data_retry_count", 0),
        response_retry_count=state.get("response_retry_count", 0),
        chart_type=state.get("chart_type"),
        errors=state.get("errors", []),
    )
