"""
tools/visualization_validator.py -- deterministic Python, the last
checkpoint before a visualization object is allowed into an API response.

This is the "Visualization Validator" stage: nothing here changes what
the chart SAYS (no number is touched) -- it only decides whether the
chart SHAPE is trustworthy enough to render. If it isn't, this falls
back to a plain table rather than sending the frontend something that
would render broken or misleading.
"""

from typing import Any, Dict, List
from graph.state import GraphState

SUPPORTED_TYPES = {"kpi", "bar", "horizontal_bar", "line", "funnel", "pie", "table"}

# These types are rendered from a labeled series and need real, matching
# labels -- a chart with a value but no label to hang it on is meaningless.
_NEEDS_LABELED_SERIES = {"bar", "horizontal_bar", "line", "funnel", "pie"}


def _validate(viz: Dict[str, Any]) -> List[str]:
    errors = []

    viz_type = viz.get("type")
    if viz_type not in SUPPORTED_TYPES:
        errors.append(f"Unsupported chart type: {viz_type!r}")
        return errors  # nothing else is checkable without a valid type

    if viz_type == "table":
        if not viz.get("columns"):
            errors.append("Table visualization is missing columns.")
        if not viz.get("rows"):
            errors.append("Table visualization has no rows.")
        return errors

    data = viz.get("data")
    if not data:
        errors.append("Visualization has no data.")
        return errors

    if viz_type in _NEEDS_LABELED_SERIES:
        for point in data:
            if point.get("label") is None:
                errors.append("One or more data points is missing a label.")
                break
            if point.get("value") is None:
                errors.append("One or more data points is missing a value.")
                break

    if viz_type in ("bar", "horizontal_bar", "line") and not (viz.get("x_axis_label") and viz.get("y_axis_label")):
        errors.append(f"{viz_type} chart is missing an axis label.")

    return errors


def _fallback_to_table(state: GraphState) -> Dict[str, Any]:
    """
    Builds a plain table from the raw, already-validated query result --
    a table has the loosest requirements of any chart type (just columns
    and rows, which the query result always has), so it's the one type
    that can always be produced as a safe fallback.
    """
    rows = state.get("query_result", [])
    if not rows:
        return {}
    return {
        "type": "table",
        "title": state.get("question", "")[:80],
        "subtitle": "Shown as a table because the requested chart couldn't be validated.",
        "x_axis_label": None,
        "y_axis_label": None,
        "columns": list(rows[0].keys()),
        "rows": rows,
    }


def validate_visualization(state: GraphState) -> GraphState:
    """
    Runs after select_visualization. If the object it built passes every
    check, it's left as-is. If not, falls back to a table rather than
    letting a broken or empty chart reach the frontend.
    """
    viz = state.get("visualization", {})

    if not viz:
        return state  # nothing to visualize this turn -- not an error

    errors = _validate(viz)
    if errors:
        state.setdefault("errors", []).append(
            f"visualization_validation_failed: {'; '.join(errors)} -- falling back to table"
        )
        state["visualization"] = _fallback_to_table(state)

    return state
