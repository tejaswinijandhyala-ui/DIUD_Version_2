"""
agents/sql_agent.py — generates ClickHouse SQL. Nothing else.

This agent receives exactly what it needs and not one thing more: the
question, the relevant business rules (already picked out by
rule_loader.py), the relevant schema (already picked out by
schema_loader.py), and — if this is a retry — the specific errors from
the last attempt.

Notice what this agent does NOT decide: it doesn't choose which rules
apply, and it doesn't choose which tables matter. Those decisions were
already made by deterministic Python before this agent ever runs. Its
only job is to turn "question + rules + schema" into one correct query.
"""

import anthropic
from graph.state import GraphState

client = anthropic.Anthropic()


def _format_rules(rules: dict) -> str:
    """Turns the rules dict into plain text Claude can read as instructions."""
    if not rules:
        return "No specific rules apply beyond standard SQL correctness."
    lines = []
    for rule in rules.values():
        if rule:
            lines.append(f"- {rule['description']}: {rule['rule']}")
    return "\n".join(lines)


def _format_schema(schema: dict) -> str:
    """Turns the schema dict into a plain-text table/column reference."""
    if not schema:
        return "No schema was loaded — this should not happen; treat as an error."
    lines = []
    for table_name, table_info in schema.items():
        lines.append(f"Table: {table_name} ({table_info.get('description', '')})")
        for col, desc in table_info.get("columns", {}).items():
            lines.append(f"  - {col}: {desc}")
        lines.append(f"  Primary key: {table_info.get('primary_key', 'unknown')}")
    return "\n".join(lines)


SYSTEM_PROMPT = """You are the SQL Generation Agent for a revenue intelligence copilot.

Your only job: write a single, correct, optimized ClickHouse SQL query
that answers the user's question. You do not explain the query, you do
not write narrative, you do not add commentary. Return ONLY the raw SQL —
no markdown code fences, no preamble, no explanation.

Rules:
- Apply every business rule you are given, exactly as written. Never
  invent a rule that wasn't provided to you.
- Only use tables and columns from the schema you were given. Never
  reference a table or column that wasn't explicitly listed.
- Always apply mandatory filters, even if the user didn't mention them.
- Use ClickHouse syntax where relevant (FINAL for deduplication,
  toFloat64OrZero() for casting Nullable(String) numeric columns, etc.)
- If aggregating, always include the correct GROUP BY.
- If you genuinely cannot answer the question with the rules and schema
  provided, return exactly: -- UNABLE_TO_GENERATE: <short reason>
  Do not guess a table or column that wasn't given to you.
"""


def run_sql_agent(state: GraphState) -> GraphState:
    """
    Generates, or regenerates, the SQL for this question.

    If this is a retry after a failed SQL or data validation, the
    specific errors from the last attempt are included in the prompt —
    so Claude fixes the actual problem it's told about, instead of
    guessing blindly at a second attempt.
    """
    rules_text = _format_rules(state.get("rules", {}))
    schema_text = _format_schema(state.get("schema", {}))

    retry_context = ""
    previous_errors = state.get("sql_validation_errors") or state.get("data_validation_errors")
    if previous_errors:
        retry_context = (
            "\n\nYour previous attempt had these specific problems — fix them:\n"
            + "\n".join(f"- {e}" for e in previous_errors)
            + f"\n\nPrevious SQL that failed:\n{state.get('sql', '')}"
        )

    user_message = f"""Question: {state['question']}

Filters already identified: {state.get('filters', {})}
Time period: {state.get('time_period', 'not specified')}

Business rules to apply:
{rules_text}

Available schema:
{schema_text}
{retry_context}
"""

    response = client.messages.create(
        model="claude-sonnet-5",
        max_tokens=1000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_message}],
    )

    sql_text = response.content[0].text.strip()

    # Defensive cleanup in case Claude wraps the SQL in markdown fences
    # despite being told not to — better to strip it than fail validation
    # on a formatting accident rather than a real SQL problem.
    if sql_text.startswith("```"):
        sql_text = sql_text.strip("`")
        if sql_text.lower().startswith("sql"):
            sql_text = sql_text[3:].strip()

    state["sql"] = sql_text
    return state
