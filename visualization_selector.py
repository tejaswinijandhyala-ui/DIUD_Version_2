"""
tools/visualization_selector.py -- deterministic Python. No Claude call.

Builds the STANDARD visualization object that's the actual API contract
with the frontend. This is the "Generate Visualization Metadata" step in
the architecture: intent decides the chart TYPE (same rule-based lookup
as before), and the query result decides the actual DATA and LABELS.

The object shape, always:
{
  "type": "kpi" | "bar" | "horizontal_bar" | "line" | "funnel" | "pie" | "table",
  "title": str,
  "subtitle": str | None,
  "x_axis_label": str | None,
  "y_axis_label": str | None,
  "data": [ {"label": ..., "value": ...}, ... ]   -- for kpi/bar/h_bar/line/funnel/pie
  "columns": [...], "rows": [...]                  -- for table only
}

Nothing here is final -- tools/visualization_validator.py checks this
object before it's ever allowed into an API response, and falls back to
a table if anything's wrong. This file's only job is to make a
reasonable first attempt.
"""

from typing import Any, Dict, List, Optional, Tuple
from graph.state import GraphState

# Intent -> chart type. None means "usually doesn't need a chart at all,"
# per the PRD's "avoid unnecessary charts for simple metric lookups."
INTENT_TO_CHART = {
    "metric_lookup": None,
    "trend_analysis": "line",
    "root_cause_analysis": "bar",
    "comparison": "bar",
    "ranking": "horizontal_bar",
    "dashboard_summary": "kpi",
    "forecast": "line",
    "pipegen_funnel": "funnel",
    "pipeline_analysis": "funnel",
    "deal_analysis": "table",
    "risk_identification": "table",
    "recommendation": None,
    "data_export": "table",
    "general_conversation": None,
}

_FUNNEL_LABEL_CANDIDATES = ["deal_stage", "stage"]
_FUNNEL_VALUE_CANDIDATES = ["deal_count", "deals", "count"]


def _is_numeric(value: Any) -> bool:
    if isinstance(value, bool):
        return False
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


def _pick_label_and_value_columns(rows: List[dict]) -> Tuple[Optional[str], Optional[str]]:
    """
    Picks a reasonable label column (first non-numeric) and value column
    (first numeric) from the actual result shape -- this is intentionally
    generic rather than hardcoded to specific column names, since the
    query result's columns vary question to question.
    """
    if not rows:
        return None, None
    first_row = rows[0]
    label_col = next((k for k, v in first_row.items() if not _is_numeric(v)), None)
    value_col = next((k for k, v in first_row.items() if _is_numeric(v)), None)
    return label_col, value_col


def _build_series(rows: List[dict], label_col: str, value_col: str) -> List[Dict[str, Any]]:
    series = []
    for row in rows:
        series.append({"label": row.get(label_col), "value": row.get(value_col)})
    return series


def _build_funnel_series(rows: List[dict]) -> Optional[List[Dict[str, Any]]]:
    """Funnel results come from Pattern A / cohort queries, which always
    have a deal_stage + count-style column pair -- try those exact names
    first before falling back to the generic picker."""
    if not rows:
        return None
    first_row = rows[0]
    label_col = next((c for c in _FUNNEL_LABEL_CANDIDATES if c in first_row), None)
    value_col = next((c for c in _FUNNEL_VALUE_CANDIDATES if c in first_row), None)
    if not label_col or not value_col:
        label_col, value_col = _pick_label_and_value_columns(rows)
    if not label_col or not value_col:
        return None
    return _build_series(rows, label_col, value_col)


def select_visualization(state: GraphState) -> GraphState:
    """
    Picks a chart type from intent, then builds the actual standardized
    visualization object from the real query result. Runs after the
    Insight Agent, since the title/labels benefit from knowing the
    question and the shape of the real data.
    """
    intent = state.get("intent", "general_conversation")
    chart_type = INTENT_TO_CHART.get(intent)
    rows = state.get("query_result", [])
    question = state.get("question", "")

    # Data-shape overrides, same reasoning as before: a single row with
    # one or two values is really just a number, and too many rows for a
    # bar chart to read cleanly should fall back to a table.
    if rows and chart_type and len(rows) == 1 and len(rows[0].keys()) <= 2:
        chart_type = "kpi"
    if chart_type in ("bar", "horizontal_bar") and len(rows) > 25:
        chart_type = "table"

    state["chart_type"] = chart_type  # kept for any code still reading the bare type

    if not chart_type or not rows:
        state["visualization"] = {}
        return state

    if chart_type == "kpi":
        label_col, value_col = _pick_label_and_value_columns(rows)
        value = rows[0].get(value_col) if value_col else list(rows[0].values())[0]
        state["visualization"] = {
            "type": "kpi",
            "title": question[:80],
            "subtitle": None,
            "x_axis_label": None,
            "y_axis_label": None,
            "data": [{"label": value_col or "value", "value": value}],
        }
    elif chart_type == "table":
        columns = list(rows[0].keys()) if rows else []
        state["visualization"] = {
            "type": "table",
            "title": question[:80],
            "subtitle": None,
            "x_axis_label": None,
            "y_axis_label": None,
            "columns": columns,
            "rows": rows,
        }
    elif chart_type == "funnel":
        series = _build_funnel_series(rows)
        state["visualization"] = {
            "type": "funnel",
            "title": question[:80],
            "subtitle": None,
            "x_axis_label": None,
            "y_axis_label": None,
            "data": series or [],
        }
    else:  # bar, horizontal_bar, line, pie
        label_col, value_col = _pick_label_and_value_columns(rows)
        series = _build_series(rows, label_col, value_col) if label_col and value_col else []
        state["visualization"] = {
            "type": chart_type,
            "title": question[:80],
            "subtitle": None,
            "x_axis_label": label_col,
            "y_axis_label": value_col,
            "data": series,
        }

    return state
