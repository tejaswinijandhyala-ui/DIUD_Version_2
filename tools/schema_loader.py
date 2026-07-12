"""
tools/schema_loader.py — deterministic Python, never Claude.

Same idea as tools/rule_loader.py, but for database structure instead of
business logic. Given what the question is about, hand Claude only the
tables and columns it actually needs — never the whole schema.

Why this matters: sending the entire schema on every request wastes
tokens, but it also gives Claude more surface area to accidentally join
the wrong tables together. Keeping this list small and specific to the
question is a real accuracy improvement, not just a cost optimization.
"""

from graph.state import GraphState
from schema.schema_book import SCHEMA_BOOK, RELEVANT_TABLES_BY_METRIC


def load_schema(state: GraphState) -> GraphState:
    """
    Looks at the metrics and dimensions already on the shared state and
    returns only the table definitions relevant to this question.
    """
    relevant_tables = set()

    searchable_text = " ".join(
        state.get("metrics", []) + state.get("dimensions", [])
    ).lower()

    for keyword, tables in RELEVANT_TABLES_BY_METRIC.items():
        if keyword in searchable_text:
            relevant_tables.update(tables)

    # Safe default: if nothing matched, hand over the deals table, since
    # nearly every revenue question touches it in some form.
    if not relevant_tables:
        relevant_tables.add("hs_analytics.deals")

    state["schema"] = {
        table: SCHEMA_BOOK[table]
        for table in relevant_tables
        if table in SCHEMA_BOOK
    }

    return state
