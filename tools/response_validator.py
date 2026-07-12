"""
tools/response_validator.py — deterministic Python, the last checkpoint
before anything reaches the user.

This is a direct port of rules.py's fact-binding verifier. It's more
rigorous than a naive "does this number appear in the data" check in one
specific way: it also recognizes SCALED values ($M, $K) and DERIVED
values (percentages and differences calculated between two real facts),
so it doesn't falsely flag legitimate analyst math as a hallucination —
while still catching a number that's nowhere close to anything real.
"""

import re
from typing import List, Set
from graph.state import GraphState

_NUM_PATTERN = re.compile(r'-?\$?\d[\d,]*\.?\d*%?')

# Strip these BEFORE number extraction so stage labels like "20% - Solution"
# and "FY27 Q1" don't get misread as numeric claims about the data.
_STAGE_LABEL_PATTERN = re.compile(r'\b\d{1,3}%\s*[-–—]\s*[A-Za-z][\w/() ]*')
_STAGE_TRANSITION_PATTERN = re.compile(r'\b\d{1,3}%\s*(?:to|→|->)\s*\d{1,3}%')
_FY_QUARTER_PATTERN = re.compile(r'\bFY\s?\d{2,4}\b|\bQ[1-4]\b', re.IGNORECASE)
_YEAR_RANGE = range(2020, 2036)


def _strip_label_noise(text: str) -> str:
    cleaned = _STAGE_LABEL_PATTERN.sub(' ', text)
    cleaned = _STAGE_TRANSITION_PATTERN.sub(' ', cleaned)
    cleaned = _FY_QUARTER_PATTERN.sub(' ', cleaned)
    return cleaned


def extract_numbers(text: str) -> Set[float]:
    cleaned = _strip_label_noise(text)
    raw = _NUM_PATTERN.findall(cleaned)
    out: Set[float] = set()
    for tok in raw:
        cleaned_tok = tok.replace('$', '').replace(',', '').replace('%', '')
        try:
            val = float(cleaned_tok)
        except ValueError:
            continue
        if val in _YEAR_RANGE and '.' not in cleaned_tok:
            continue  # a bare year like 2027 isn't a data claim
        out.add(round(val, 2))
    return out


def extract_numbers_from_rows(rows: List[dict]) -> Set[float]:
    out: Set[float] = set()
    for row in rows:
        for v in row.values():
            try:
                out.add(round(float(v), 2))
            except (TypeError, ValueError):
                continue
    return out


def _relative_tolerance(value: float, base_tolerance: float = 0.5) -> float:
    return max(base_tolerance, abs(value) * 0.01)


def validate_summary_against_facts(
    summary_text: str,
    allowed_rows: List[dict],
    tolerance: float = 0.5,
) -> List[str]:
    """
    Checks every number in the written response against the real data,
    allowing for three legitimate transformations a human analyst makes
    routinely: scaling to millions/thousands, and calculating a
    percentage or a raw difference between two real facts.
    """
    claimed = extract_numbers(summary_text)
    actual = extract_numbers_from_rows(allowed_rows)

    if not actual:
        return []

    # Precompute plausible derived values: scaled (÷1M, ÷1K) and simple
    # ratios between any two real facts, expressed as a percentage.
    derived: Set[float] = set()
    for a in actual:
        derived.add(round(a, 1))
        derived.add(round(a / 1_000_000, 1))
        derived.add(round(a / 1_000, 1))
        derived.add(round(a / 1_000_000, 2))

    actual_nonzero = [a for a in actual if a != 0]
    for a in actual_nonzero:
        for b in actual_nonzero:
            if a == b:
                continue
            ratio = a / b * 100
            derived.add(round(ratio, 1))
            derived.add(round(ratio, 0))

    violations = []
    for c in claimed:
        if c in (0.0, 100.0, 1.0):
            continue  # extremely common as counts/percentages, not worth flagging

        tol = _relative_tolerance(c, tolerance)

        matches_raw = any(abs(c - a) <= tol for a in actual)
        matches_m = any(abs(c - a / 1_000_000) <= tol for a in actual)
        matches_k = any(abs(c - a / 1_000) <= tol for a in actual)
        matches_scale = any(abs(c * 1_000_000 - a) <= max(tol * 1_000_000, abs(a) * 0.01) for a in actual)
        matches_derived = any(abs(c - d) <= tol for d in derived)

        if not (matches_raw or matches_m or matches_k or matches_scale or matches_derived):
            violations.append(f"Unverified number in summary: {c}")

    return violations


# =============================================================================
# GRAPH NODE — adapts the above to this pipeline's GraphState interface
# =============================================================================

def validate_response(state: GraphState) -> GraphState:
    """
    Runs the fact-binding verifier against the response the Insight Agent
    just wrote, using this turn's validated query result as the only
    source of truth. Same _fail()/success pattern as the other two
    validators, including setting final_status on the happy path.
    """
    response_text = state.get("response", "")

    if not response_text.strip():
        _fail(state, ["Response is empty."])
        return state

    violations = validate_summary_against_facts(
        response_text, state.get("query_result", [])
    )

    from governance.audit_log import log_rule_audit  # lazy: avoids a circular import
    log_rule_audit("summary_check", violations, "post_summary", state.get("question", ""))

    if violations:
        _fail(state, violations)
    else:
        state["response_valid"] = True
        state["response_validation_errors"] = []
        state["final_status"] = "success"

    return state


def _fail(state: GraphState, errors: list) -> None:
    state["response_valid"] = False
    state["response_validation_errors"] = errors
    state["response_retry_count"] = state.get("response_retry_count", 0) + 1
