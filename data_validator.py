"""
tools/data_validator.py — deterministic Python, never Claude.

The SQL Validator checked the query before it ran. This checks the
*results* after it ran — because a query can be perfectly correct SQL
and still return a wrong or suspicious answer.

Includes one check ported directly from DIUD's production rules.py:
funnel_sum_within_cohort, which catches a specific real failure mode —
stage counts that don't come from a single deduplicated cohort CTE will
sum to more than the cohort total, which is mathematically impossible
for a real funnel and always means duplicated rows.
"""

from typing import List, Optional
from graph.state import GraphState

MAX_REASONABLE_ROWS = 10000  # a sanity ceiling, not a real business limit


def _funnel_sum_ok(rows: List[dict]) -> bool:
    """
    Ported from rules.py's RESULT_RULES. The first row of a cohort
    result is the cohort's own total; every subsequent stage (active +
    Closed Won/Lost) should sum to no more than that total — a cohort
    can only shrink or stay flat moving down the funnel, never grow.
    """
    try:
        cohort_total: Optional[float] = None
        active_sum = 0.0
        terminal_sum = 0.0
        for r in rows:
            stage = str(r.get("deal_stage", ""))
            cnt = float(r.get("deal_count", 0) or 0)
            if cohort_total is None:
                cohort_total = cnt
                continue
            if "Closed Won" in stage or "Closed Lost" in stage:
                terminal_sum += cnt
            else:
                active_sum += cnt
        if cohort_total is None:
            return True
        return (active_sum + terminal_sum) <= cohort_total
    except Exception:
        return True


def _looks_like_cohort_result(rows: List[dict]) -> bool:
    """A cheap signal that this result is a cohort funnel, without
    re-running full intent detection here: cohort results always have
    both a deal_stage and a deal_count column."""
    if not rows:
        return False
    first = rows[0]
    return "deal_stage" in first and "deal_count" in first


def validate_data(state: GraphState) -> GraphState:
    """
    Runs sanity checks against the rows ClickHouse returned, and writes
    back whether they're trustworthy enough to hand to the Insight Agent.
    """
    rows = state.get("query_result", [])
    errors = []

    # ---- Check 1: completely empty result ----
    if len(rows) == 0:
        errors.append("Query returned zero rows.")

    # ---- Check 2: suspiciously large result ----
    if len(rows) > MAX_REASONABLE_ROWS:
        errors.append(
            f"Query returned {len(rows)} rows — likely missing an aggregation or filter."
        )

    if rows:
        # ---- Check 3: exact duplicate rows ----
        seen = set()
        duplicate_count = 0
        for row in rows:
            row_signature = tuple(sorted(row.items()))
            if row_signature in seen:
                duplicate_count += 1
            seen.add(row_signature)
        if duplicate_count > 0:
            errors.append(
                f"Found {duplicate_count} exact duplicate rows — check for a fan-out join."
            )

        # ---- Check 4: impossible values ----
        for row in rows:
            for key, value in row.items():
                if isinstance(value, (int, float)):
                    if "amount" in key.lower() and value < 0:
                        errors.append(f"Negative value found in '{key}': {value}")
                        break
                    if "percent" in key.lower() and not (0 <= value <= 100):
                        errors.append(f"Out-of-range percentage in '{key}': {value}")
                        break

        # ---- Check 5: null-heavy results ----
        total_cells = len(rows) * len(rows[0])
        null_cells = sum(1 for row in rows for value in row.values() if value is None)
        if total_cells > 0 and (null_cells / total_cells) > 0.5:
            errors.append("More than half of all values are null — check join conditions.")

        # ---- Check 6: funnel sum within cohort total (ported check) ----
        if _looks_like_cohort_result(rows) and not _funnel_sum_ok(rows):
            errors.append(
                "Funnel stage counts (active stages + Closed Won/Lost) exceed the cohort "
                "total — rows were not derived from a single deduplicated cohort CTE."
            )

    if errors:
        state["data_valid"] = False
        state["data_validation_errors"] = errors
        state["data_retry_count"] = state.get("data_retry_count", 0) + 1
    else:
        state["data_valid"] = True
        state["data_validation_errors"] = []

    return state
