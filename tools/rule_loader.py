"""
tools/rule_loader.py — deterministic Python, never Claude.

This is the "librarian" step. Given what the Intent Agent already figured
out about the question (its intent, metrics, dimensions), this function
goes and fetches only the business rules that are actually relevant —
never the whole Rules Book, and never something invented on the spot.

Why this file matters for the "never invent business logic" principle:
Claude is never given the choice of *which* rules apply to a question.
That decision is made here, in plain deterministic code, before Claude
ever sees the question again for SQL generation. Claude's only job later
is to *apply* whatever rules this function decided were relevant — it
can't reach for a rule that wasn't handed to it.
"""

from graph.state import GraphState
from rules.rules_book import RULES_BOOK, get_rule

# These apply to every single query, no matter what the user asked —
# so they're always included, never conditionally matched.
ALWAYS_INCLUDE = ["mandatory_base_filters", "deduplication", "sql_guardrails", "date_casting"]

# A simple keyword map: if a metric or dimension mentions one of these
# words, pull in the matching rule. This is intentionally simple to start
# with. It can be swapped later for something smarter (like embedding-based
# search) without changing anything else in the pipeline — this function's
# job is always "read the state, return the relevant rules," no matter how
# the matching happens inside it.
KEYWORD_TO_RULE = {
    # fiscal year / dates
    "fy": "fiscal_year",
    "fiscal": "fiscal_year",
    "quarter": "fiscal_year",
    "quota": "fiscal_year",
    # patterns
    "pipegen": "pipegen_funnel",
    "pipe gen": "pipegen_funnel",
    "funnel": "pipegen_funnel",
    "stage count": "pipegen_funnel",
    "conversion": "pipegen_funnel",
    "active pipeline": "active_pipeline",
    "stalled": "active_pipeline",
    "days in stage": "active_pipeline",
    "bant": "active_pipeline",
    "deal list": "active_pipeline",
    "attainment": "attainment",
    "vs target": "attainment",
    "quota attainment": "attainment",
    "coverage": "attainment",
    "on track": "attainment",
    "gap to target": "attainment",
    "cohort": "cohort_funnel",
    # target tables
    "target": "target_table_casting",
    "l1": "target_table_tiers",
    "l2": "target_table_tiers",
    "stretch": "target_table_tiers",
    "committed": "target_table_tiers",
    "closed won": "closed_won",
    "closed_won": "closed_won",
    "cw": "closed_won",
    "partner": "partner_targets",
    "hyperscaler": "partner_targets",
    "psd": "partner_targets",
    "gsi": "partner_targets",
    "reseller": "partner_targets",
    # MQL
    "mql": "mql",
    "marketing qualified": "mql",
    # dimensions / breakdowns
    "region": "dimension_mappings",
    "industry": "dimension_mappings",
    "priority": "dimension_mappings",
    "by ": "breakdown_dimension_rule",
    "breakdown": "breakdown_dimension_rule",
    "group by": "breakdown_dimension_rule",
    # dashboards
    "dashboard": "dashboard_definitions",
    "eop": "dashboard_definitions",
    "exec kpi": "dashboard_definitions",
    "bdr": "dashboard_definitions",
    "ae focus": "dashboard_definitions",
    # stage reference
    "stage": "deal_stage_reference",
    "deal health": "deal_stage_reference",
    "benchmark": "deal_stage_reference",
}


def load_rules(state: GraphState) -> GraphState:
    """
    Looks at the intent, metrics, and dimensions already sitting on the
    shared state, and writes back only the rules relevant to this
    specific question.
    """
    relevant_keys = set(ALWAYS_INCLUDE)

    searchable_text = " ".join(
        state.get("metrics", [])
        + state.get("dimensions", [])
        + [state.get("intent", "")]
    ).lower()

    for keyword, rule_key in KEYWORD_TO_RULE.items():
        if keyword in searchable_text and rule_key in RULES_BOOK:
            relevant_keys.add(rule_key)

    state["rules"] = {key: get_rule(key) for key in relevant_keys}

    return state
