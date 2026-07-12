"""
tools/sql_validator.py — deterministic Python, never Claude.

This is a direct port of DIUD's production rules.py — the real,
battle-tested SQL validator, not a simplified regex version. Every check
here exists because a specific wrong answer already shipped once from
getting it wrong; the comments explaining "confirmed real bug" are kept
verbatim rather than summarized away, since they explain WHY a check
exists in a way a bare regex can't.

detect_intent() below is intentionally self-contained and separate from
the LLM-based Intent Agent earlier in the graph. The Intent Agent
classifies what kind of QUESTION this is (metric lookup, trend analysis,
etc.) for narration purposes. This function classifies what PATTERN the
SQL should follow (A/B/C, cohort, MQL) for validation purposes — two
different jobs that happen to both be called "intent," which is why this
one stays a private, regex-based helper rather than reusing state["intent"].
"""

import re
from datetime import date
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from graph.state import GraphState

try:
    import sqlglot
    from sqlglot import exp as sqlglot_exp
    _SQLGLOT_AVAILABLE = True
except ImportError:
    _SQLGLOT_AVAILABLE = False


# =============================================================================
# LIGHTWEIGHT CTE PARSING
# =============================================================================

_CTE_HEAD = re.compile(r'(\w+)\s+AS\s*\(', re.I)


def _split_ctes(sql: str) -> Tuple[Dict[str, str], str]:
    if not re.match(r'\s*WITH\b', sql, re.I):
        return {}, sql

    ctes: Dict[str, str] = {}
    pos = 0
    search_from = 0
    while True:
        m = _CTE_HEAD.search(sql, search_from)
        if not m:
            break
        alias = m.group(1)
        depth = 1
        i = m.end()
        start_body = i
        while i < len(sql) and depth > 0:
            if sql[i] == '(':
                depth += 1
            elif sql[i] == ')':
                depth -= 1
            i += 1
        body = sql[start_body:i - 1]
        ctes[alias] = body
        pos = i
        rest = sql[pos:].lstrip()
        if rest[:1] == ',':
            search_from = pos
            continue
        else:
            break

    tail = sql[pos:]
    return ctes, tail


def _current_fy_start_date() -> date:
    """April 1 of the current fiscal year — used only to detect a stale
    hardcoded close_date bound, never to generate SQL."""
    today = date.today()
    fy_start_year = today.year if today.month >= 4 else today.year - 1
    return date(fy_start_year, 4, 1)


def _has_stale_close_date_bound(sql: str) -> bool:
    """
    True if the SQL has a close_date >= 'YYYY-MM-DD' bound that's a
    literal date more than ~13 months old. Confirmed real failure mode:
    the Pattern B template itself once had a hardcoded bound that
    silently went a full year stale.
    """
    m = re.search(r"close_date\s*>=\s*'(\d{4}-\d{2}-\d{2})'", sql, re.I)
    if not m:
        return False
    try:
        year, month, day = (int(x) for x in m.group(1).split("-"))
        bound_date = date(year, month, day)
    except ValueError:
        return False
    age_days = (_current_fy_start_date() - bound_date).days
    return age_days > 45


def _table_join_sides(sql: str, table_name: str, dialect: str = "clickhouse") -> Optional[List[str]]:
    """
    For every place `table_name` gets pulled into the query via a JOIN —
    directly, via an inline subquery, or via a CTE that itself wraps the
    table — return the join side used. Returns None if the SQL can't be
    parsed at all, so the caller can fall back to a text-based check
    rather than silently treating a parse failure as "no join found".

    This replaces text-proximity regex guessing with real structural
    parsing via sqlglot, reading the actual join tree instead of
    guessing from where text sits.
    """
    if not _SQLGLOT_AVAILABLE:
        return None
    try:
        tree = sqlglot.parse_one(sql, read=dialect)
    except Exception:
        return None

    cte_wraps_table: Dict[str, bool] = {}
    for cte in tree.find_all(sqlglot_exp.CTE):
        tables_inside = {t.name.lower() for t in cte.this.find_all(sqlglot_exp.Table)}
        cte_wraps_table[cte.alias.lower()] = table_name.lower() in tables_inside

    sides: List[str] = []
    for join in tree.find_all(sqlglot_exp.Join):
        tables_here = {t.name.lower() for t in join.this.find_all(sqlglot_exp.Table)}
        referenced_name = (getattr(join.this, "alias", "") or getattr(join.this, "name", "") or "").lower()

        if table_name.lower() in tables_here:
            sides.append(join.side or "INNER")
        elif referenced_name in cte_wraps_table and cte_wraps_table[referenced_name]:
            sides.append(join.side or "INNER")
    return sides


def _base_filter_scope(sql: str) -> str:
    try:
        ctes, tail = _split_ctes(sql)
        if not ctes:
            return sql

        root_aliases = [a for a, body in ctes.items() if 'hs_analytics.deals' in body]
        if not root_aliases:
            return sql

        scope_aliases: Set[str] = set(root_aliases)
        for alias, body in ctes.items():
            if alias in scope_aliases:
                continue
            for root in root_aliases:
                if re.search(rf'\b{re.escape(root)}\b', body):
                    scope_aliases.add(alias)
                    break

        return "\n".join(ctes[a] for a in scope_aliases)
    except Exception:
        return sql


def _find_stale_hardcoded_dates(sql: str) -> list:
    """
    Finds every 'column >= YYYY-MM-DD' comparison anywhere in the SQL
    where the literal date is more than ~13 months old, regardless of
    which column. A real production query proved the close_date-only
    check wrong: the same stale literal showed up on create_date, an
    MQL date_entered filter, and an association createdate filter too.

    Exempts a stale lower bound that has a matching upper bound on the
    same column (a genuinely bounded historical range — how a real
    "compare to last fiscal year" question should look). Targets the
    open-ended case: a stale lower bound with no upper bound, which
    almost always means "today's FY start" got hardcoded instead of
    computed.
    """
    stale = []
    for m in re.finditer(r"(\w+)\s*(>=|>)\s*'(\d{4}-\d{2}-\d{2})'", sql, re.I):
        col, op, date_str = m.group(1), m.group(2), m.group(3)
        try:
            year, month, day = (int(x) for x in date_str.split("-"))
            bound_date = date(year, month, day)
        except ValueError:
            continue
        age_days = (_current_fy_start_date() - bound_date).days
        if age_days <= 45:
            continue
        has_upper_bound = bool(re.search(rf"\b{re.escape(col)}\b\s*(<=|<)\s*'\d{{4}}-\d{{2}}-\d{{2}}'", sql, re.I))
        if not has_upper_bound:
            stale.append((col, date_str))
    return stale


def _mql_association_joined_via_left_join(sql: str) -> bool:
    """
    True if gs_DealContactAssociation is joined with LEFT JOIN. Tries
    real structural parsing first; falls back to text-proximity regex
    only if the SQL can't be parsed at all.
    """
    sides = _table_join_sides(sql, "gs_DealContactAssociation")
    if sides is not None:
        return "LEFT" in sides

    literal_adjacent = re.compile(
        r'\bLEFT\s+JOIN\b(?:(?!\bJOIN\b).){0,300}?gs_DealContactAssociation',
        re.I | re.S,
    )
    ctes, tail = _split_ctes(sql)
    if not ctes:
        return bool(literal_adjacent.search(sql))
    assoc_aliases = [a for a, body in ctes.items() if 'gs_DealContactAssociation' in body]
    if not assoc_aliases:
        return bool(literal_adjacent.search(sql))
    for alias in assoc_aliases:
        if literal_adjacent.search(ctes[alias]):
            return True
    rest = tail + "\n" + "\n".join(b for a, b in ctes.items() if a not in assoc_aliases)
    for alias in assoc_aliases:
        if re.search(rf'\bLEFT\s+JOIN\s+{re.escape(alias)}\b', rest, re.I):
            return True
    return False


# =============================================================================
# PATTERN DETECTION
# =============================================================================

def _is_pattern_a(sql: str) -> bool:
    if re.search(r'--\s*Pattern\s*A', sql, re.I):
        return True
    # Require at least 2 consecutive chained OR conditions (3+ total)
    # matching the sentinel-check shape — not just "3+ became_X columns
    # exist somewhere AND an OR keyword exists somewhere," which
    # misclassifies Pattern B's own template (many became_X columns for
    # day-in-stage math, not cumulative OR-chain counting).
    chain = re.findall(
        r"became_\d+_deal_date\s*!=\s*'1900-01-01'\s*OR",
        sql, re.I,
    )
    return len(chain) >= 2


def _is_cohort_query(sql: str, intent: dict) -> bool:
    if intent.get("pattern_hint") == "A":
        return False
    if _is_pattern_a(sql):
        return False
    return intent.get("cohort_stage") is not None


def _has_became_date(sql: str) -> bool:
    return bool(re.search(r'became_\d+_deal_date', sql, re.I))


def _is_pattern_c(sql: str, intent: dict) -> bool:
    return intent.get("metric") == "attainment"


def _is_diagnostic_lookup(sql: str) -> bool:
    """
    True for a bare metadata/value-discovery query against a target table
    — e.g. `SELECT DISTINCT fy, quarter FROM gs_pipeline_quotas_v1` — the
    exact pattern the attainment rule instructs the SQL Agent to run when
    an attainment query returns 0 rows. Without this exemption, that
    exact required query gets rejected by the two-CTE and float-cast
    rules meant for the real attainment computation.
    """
    has_target_aggregation = bool(re.search(
        r'\b(SUM|AVG|COUNT)\s*\(\s*[a-zA-Z0-9_]*\s*\(?\s*(amount_target|deals_target|mql_target|quota)',
        sql, re.I,
    ))
    references_actuals = 'hs_analytics.deals' in sql
    return not has_target_aggregation and not references_actuals


_STAGE_COLUMN_MAP = {
    "5": "became_5_deal_date",
    "10": "became_10_deal_date",
    "20": "became_20_deal_date",
    "30": "became_30_deal_date",
    "40": "became_40_deal_date",
    "60": "became_60_deal_date",
    "75": "became_75_deal_date",
}


def _expected_became_column(intent: Dict[str, Any]) -> str:
    stage = intent.get("stage") or intent.get("cohort_stage") or "20"
    return _STAGE_COLUMN_MAP.get(stage, "became_20_deal_date")


def _fy_anchor_column(sql: str) -> Optional[str]:
    m = re.search(r'toYear\(\s*(became_\d+_deal_date)\s*\)', sql, re.I)
    return m.group(1).lower() if m else None


# =============================================================================
# INTENT DETECTION (self-contained, regex-based — see module docstring)
# =============================================================================

_PATTERN_A_KEYWORDS = re.compile(
    r'\b(funnel|pipegen|pipe[\s-]?gen|conversion|stage\s+breakdown|stage\s+counts?)\b',
    re.I,
)
_EXPLICIT_COHORT_KEYWORD = re.compile(r'\bcohort\b', re.I)


def detect_intent(user_message: str, sql: str = "") -> Dict[str, Any]:
    msg = user_message or ""
    intent: Dict[str, Any] = {}

    stage_match = re.search(r'\b(5|10|20|30|40|60|75)\s*%', msg)
    if stage_match:
        intent["stage"] = stage_match.group(1)

    has_pattern_a_kw = bool(_PATTERN_A_KEYWORDS.search(msg))
    has_cohort_kw = bool(_EXPLICIT_COHORT_KEYWORD.search(msg))

    if has_pattern_a_kw and not has_cohort_kw:
        intent["pattern_hint"] = "A"

    if intent.get("pattern_hint") != "A":
        m = re.search(r'(\d+)\s*%\s*(?:→|->|to)\s*(closed\s*won|cw)\b', msg, re.I)
        if m:
            intent["cohort_stage"] = m.group(1)

        if not intent.get("cohort_stage"):
            m = re.search(r'\b(cohort|starting\s+(?:at|from))\b.*?(\d+)\s*%', msg, re.I)
            if m:
                intent["cohort_stage"] = m.group(2)

        if not intent.get("cohort_stage"):
            m = re.search(r'(\d+)\s*%.*?\bcohort\b', msg, re.I)
            if m:
                intent["cohort_stage"] = m.group(1)

        if not intent.get("cohort_stage") and sql and not _is_pattern_a(sql):
            m = re.search(r'became_(\d+)_deal_date', sql, re.I)
            if m and re.search(r'deal_stage\s+NOT\s+IN', sql, re.I):
                intent["cohort_stage"] = m.group(1)

    if re.search(r'\b(list|show me all|which deals|deals\s+(with|where))\b', msg, re.I):
        intent["query_type"] = "list"

    if re.search(r'\b(top|first)\s+\d+\b', msg, re.I):
        intent["top_n"] = True

    if re.search(r'\bMQLs?\b', msg, re.I):
        intent["metric"] = "mql"
        if re.search(r'\b(deal|deals|pipeline|opportunit\w*|convert\w*|funnel)\b', msg, re.I):
            intent["mql_needs_deal_join"] = True

    if re.search(
        r'\b(attainment|quota|coverage|(?:vs\.?|against|versus|compared?\s+to)\s*targets?|gap\s*to\s*target)\b',
        msg, re.I,
    ) or re.search(r'\b\d{1,3}\s*%\s*(?:pipegen\s+)?target\b', msg, re.I):
        intent["metric"] = "attainment"

    if re.search(r'\bactive pipeline\b', msg, re.I):
        intent["metric"] = "active_pipeline"

    if sql and "MANDATORY_BASE_FILTERS" in sql:
        intent["placeholder_leak"] = True

    return intent


def _arity(fn: Callable) -> int:
    return fn.__code__.co_argcount


def _call(fn: Callable, sql: str, intent: dict):
    return fn(sql, intent) if _arity(fn) == 2 else fn(sql)


# =============================================================================
# SQL-TEXT RULES — the actual checklist
# =============================================================================

RULES: List[Dict[str, Any]] = [

    {
        "id": "base_filter_pipeline",
        "section": "MANDATORY_BASE_FILTERS (1/3) — pipeline = 'default'",
        "applies_when": lambda sql: "hs_analytics.deals" in sql,
        "check": lambda sql: bool(re.search(r"pipeline\s*=\s*'default'", _base_filter_scope(sql), re.I)),
        "message": "Missing `pipeline = 'default'` base filter in the CTE(s) that read hs_analytics.deals.",
    },
    {
        "id": "base_filter_deal_type",
        "section": "MANDATORY_BASE_FILTERS (2/3) — Partner-Led SMB exclusion",
        "applies_when": lambda sql: "hs_analytics.deals" in sql,
        "check": lambda sql: (
            "Partner-Led SMB" in (scope := _base_filter_scope(sql))
            and bool(re.search(r'\bNOT\s+IN\b', scope, re.I))
        ),
        "message": "Missing deal_type NOT IN ('Partner-Led SMB') base filter in the CTE(s) that read hs_analytics.deals.",
    },
    {
        "id": "base_filter_allowlist",
        "section": "MANDATORY_BASE_FILTERS (3/3) — gs_deal_ids_hs allowlist",
        "applies_when": lambda sql: "hs_analytics.deals" in sql,
        "check": lambda sql: "gs_deal_ids_hs" in _base_filter_scope(sql),
        "message": "Missing deal_id allowlist subquery against kore_ai_hubspot.gs_deal_ids_hs.",
    },
    {
        "id": "final_keyword",
        "section": "Duplicate exclusion — FINAL on hs_analytics.*",
        "applies_when": lambda sql: bool(re.search(r"hs_analytics\.\w+", sql)),
        "check": lambda sql: bool(re.search(r"hs_analytics\.\w+\s+(?:AS\s+)?(?:\w+\s+)?FINAL", sql, re.I)),
        "message": "Missing FINAL on at least one hs_analytics.* table reference.",
    },
    {
        "id": "count_distinct_not_count",
        "section": "countDistinct, never count()",
        "applies_when": lambda sql: "hs_analytics" in sql and bool(re.search(r'(?<!\w)count\s*\((?!Distinct)', sql, re.I)),
        "check": lambda sql: bool(re.search(r'countDistinct\s*\(\s*(deal_id|contact_id)\s*\)', sql, re.I)),
        "message": "Uses count() instead of countDistinct(deal_id) or countDistinct(contact_id).",
    },
    {
        "id": "distinct_in_association_subquery",
        "section": "DISTINCT in gs_DealContactAssociation subqueries",
        "applies_when": lambda sql: "gs_DealContactAssociation" in sql and bool(re.search(r'\(\s*SELECT', sql, re.I)),
        "check": lambda sql: bool(re.search(r'SELECT\s+DISTINCT', sql, re.I)),
        "message": "Subquery against gs_DealContactAssociation is missing DISTINCT.",
    },
    {
        "id": "select_or_with_only",
        "section": "SELECT/WITH only — no destructive SQL",
        "applies_when": lambda sql: True,
        "check": lambda sql: sql.strip().upper().startswith(("SELECT", "WITH", "--")),
        "message": "Query does not start with SELECT, WITH, or a comment. Only read queries are permitted.",
    },
    {
        "id": "no_placeholder_leak_strict",
        "section": "Generation hygiene — no unresolved placeholder tokens",
        "applies_when": lambda sql, intent: intent.get("placeholder_leak", False),
        "check": lambda sql, intent: False,
        "message": "Literal placeholder '<MANDATORY_BASE_FILTERS>' leaked into generated SQL — must be expanded.",
    },
    {
        "id": "no_limit_on_list",
        "section": "No LIMIT on list queries unless user says 'top N'/'first N'",
        "applies_when": lambda sql, intent: intent.get("query_type") == "list" and not intent.get("top_n"),
        "check": lambda sql, intent: "LIMIT" not in sql.upper(),
        "message": "LIMIT applied to a list query the user did not ask to cap with 'top N' or 'first N'.",
    },
    {
        "id": "date_cast_standard",
        "section": "Date casting — CAST(LEFT(coalesce(col,'1900-01-01'),10) AS DATE)",
        "applies_when": lambda sql: bool(re.search(r"\b(close_date|became_\d+_deal_date)\b\s*(>=|<=|>|<|=)\s*'", sql, re.I)),
        "check": lambda sql: bool(re.search(r"CAST\s*\(\s*LEFT\s*\(\s*coalesce\s*\(", sql, re.I)),
        "message": "Raw date string comparison without the mandatory CAST(LEFT(coalesce(col,'1900-01-01'),10) AS DATE) cast.",
    },
    {
        "id": "no_stale_hardcoded_dates",
        "section": "Generation hygiene — no hardcoded stale dates on ANY column",
        "applies_when": lambda sql: True,
        "check": lambda sql: not _find_stale_hardcoded_dates(sql),
        "message": (
            "A hardcoded date more than ~13 months old was found in a filter. "
            "Never type a specific date for 'current fiscal year start' — compute it "
            "dynamically, or ask the user for the exact period if unsure."
        ),
    },
    {
        "id": "sentinel_not_null_check",
        "section": "Sentinel '1900-01-01' — use != '1900-01-01', NOT IS NOT NULL",
        "applies_when": lambda sql: _has_became_date(sql),
        "check": lambda sql: not bool(re.search(r"became_\d+_deal_date\s+IS\s+NOT\s+NULL", sql, re.I)),
        "message": "Using `IS NOT NULL` on became_<N>_deal_date. The sentinel for 'date not set' is '1900-01-01' — use `!= '1900-01-01'`.",
    },
    {
        "id": "target_table_float_cast",
        "section": "Target table — SUM(toFloat64OrZero(col))",
        "applies_when": lambda sql: bool(
            re.search(r"gs_pipeline_quotas_v1|gs_partner_targets|gs_closed_won_quotas|gs_marketing_targets", sql, re.I)
        ) and not _is_diagnostic_lookup(sql),
        "check": lambda sql: bool(re.search(r"toFloat64OrZero|toFloat32OrZero", sql, re.I)),
        "message": "Target table columns are Nullable(String). Always cast with SUM(toFloat64OrZero(col)).",
    },
    {
        "id": "target_no_quarterly_divide",
        "section": "Never derive quarterly target by dividing by 4",
        "applies_when": lambda sql: bool(re.search(r"gs_pipeline_quotas_v1|gs_partner_targets|gs_marketing_targets", sql, re.I)),
        "check": lambda sql: not bool(re.search(r"/\s*4\b", sql)),
        "message": "Target figure is being divided by 4 to derive a quarterly value. Filter the target table to the exact quarter instead.",
    },
    {
        "id": "nullif_in_division",
        "section": "nullIf(denominator, 0) in every division",
        "applies_when": lambda sql: "/" in sql and bool(re.search(r"attainment|coverage|pct|rate|ratio", sql, re.I)),
        "check": lambda sql: bool(re.search(r"nullIf\s*\(", sql, re.I)),
        "message": "Division present without nullIf(denominator, 0) — risk of divide-by-zero.",
    },
    {
        "id": "pattern_a_or_chain",
        "section": "Pattern A — cumulative OR-chain stage counting",
        "applies_when": lambda sql: _is_pattern_a(sql),
        "check": lambda sql: bool(re.search(r'\bOR\b', sql, re.I)),
        "message": "Query is marked Pattern A but contains no OR conditions.",
    },
    {
        "id": "pattern_a_stage_anchor",
        "section": "Pattern A — stage-specific FY anchor",
        "applies_when": lambda sql, intent: _is_pattern_a(sql) and _fy_anchor_column(sql) is not None,
        "check": lambda sql, intent: _fy_anchor_column(sql) == _expected_became_column(intent).lower(),
        "message": "Pattern A's FY/quarter anchor does not match the stage the user asked about.",
    },
    {
        "id": "pattern_b_close_date_filter",
        "section": "Pattern B — primary filter is close_date",
        "applies_when": lambda sql, intent: (
            intent.get("metric") == "active_pipeline" and not _is_pattern_a(sql) and not _is_pattern_c(sql, intent)
        ),
        "check": lambda sql, intent: bool(re.search(r"close_date\s*>=", sql, re.I)) and not _has_stale_close_date_bound(sql),
        "message": "Pattern B must filter on close_date >= <date>, not became_10_deal_date, and that bound must not be stale.",
    },
    {
        "id": "partner_non_hyperscaler_literal_filter",
        "section": "'Partner - Non Hyperscaler' never matches partner_team_type literally",
        "applies_when": lambda sql, intent: bool(re.search(r"gs_partner_targets", sql, re.I)),
        "check": lambda sql, intent: not bool(
            re.search(r"partner_team_type\s*(?:=|IN\s*\([^)]*)\s*'Partner\s*-\s*Non\s*Hyperscaler'", sql, re.I)
        ),
        "message": (
            "partner_team_type filtered on 'Partner - Non Hyperscaler', which never exists in that column and "
            "silently returns zero rows. Maps to partner_team_type IN ('GSI/SI','Reseller/BPO/TSD')."
        ),
    },
    {
        "id": "pattern_b_active_stages",
        "section": "Pattern B — active pipeline stage filter",
        "applies_when": lambda sql, intent: intent.get("metric") == "active_pipeline",
        "check": lambda sql, intent: all(
            s in sql for s in [
                "20% - Solution", "30% - Proof", "40% - Proposal",
                "60% - Price Negotiation", "75% - Contract Review",
            ]
        ),
        "message": "Active pipeline query missing one or more of the 5 required deal_stage values.",
    },
    {
        "id": "pattern_c_two_cte",
        "section": "Pattern C — actuals CTE + targets CTE (never fan-out join)",
        "applies_when": lambda sql, intent: _is_pattern_c(sql, intent) and not _is_diagnostic_lookup(sql),
        "check": lambda sql, intent: sql.strip().upper().startswith("WITH") and len(re.findall(r'\bAS\s*\(', sql, re.I)) >= 2,
        "message": "Attainment/target query must use independent CTEs for actuals and targets, then LEFT JOIN them.",
    },
    {
        "id": "pattern_c_stage_anchor",
        "section": "Pattern C — stage-specific became date",
        "applies_when": lambda sql, intent: _is_pattern_c(sql, intent) and _fy_anchor_column(sql) is not None,
        "check": lambda sql, intent: _fy_anchor_column(sql) == _expected_became_column(intent).lower(),
        "message": "Pattern C's FY/quarter anchor does not match the stage the user asked about.",
    },
    {
        "id": "pattern_c_source_merge",
        "section": "Pattern C — Executive Outreach + Investor merged in source mapping",
        "applies_when": lambda sql, intent: (
            _is_pattern_c(sql, intent) and "deal_source_rollup" in sql and "Executive Outreach" in sql
        ),
        "check": lambda sql, intent: ("Investor" in sql and "Executive Outreach" in sql) or "Investor" not in sql,
        "message": "Pattern C source mapping must merge 'Investor' into 'Executive Outreach'.",
    },
    {
        "id": "pattern_c_target_tier_default",
        "section": "Pattern C — default tier is L2",
        "applies_when": lambda sql, intent: _is_pattern_c(sql, intent),
        "check": lambda sql, intent: not bool(re.search(r'\b(l1_|_l1\b|committed_|_committed\b)', sql, re.I)),
        "message": "Target query is using L1 or Committed tier columns. Default is always L2 unless the user explicitly asks otherwise.",
    },
    {
        "id": "cohort_anchor_sentinel",
        "section": "Cohort anchor — became_<N>_deal_date != '1900-01-01'",
        "applies_when": lambda sql, intent: _is_cohort_query(sql, intent),
        "check": lambda sql, intent: bool(re.search(rf"{_expected_became_column(intent)}\s*!=\s*'1900-01-01'", sql, re.I)),
        "message": "Cohort query missing `became_<N>_deal_date != '1900-01-01'` sentinel anchor.",
    },
    {
        "id": "cohort_exclusion",
        "section": "Stage exclusion — NOT IN prior stages",
        "applies_when": lambda sql, intent: _is_cohort_query(sql, intent),
        "check": lambda sql, intent: bool(re.search(r'\bNOT\s+IN\b', sql, re.I)),
        "message": "Cohort query missing NOT IN exclusion of all deal_stage values prior to the cohort starting stage.",
    },
    {
        "id": "cohort_single_cte",
        "section": "Cohort must be a single CTE with GROUP BY deal_stage",
        "applies_when": lambda sql, intent: _is_cohort_query(sql, intent),
        "check": lambda sql, intent: sql.strip().upper().startswith("WITH") and bool(re.search(r'GROUP\s+BY\s+deal_stage', sql, re.I)),
        "message": "Cohort funnel should be a single WITH-cohort CTE with GROUP BY deal_stage.",
    },
    {
        "id": "cohort_count_distinct",
        "section": "Deduplication — countDistinct(deal_id) in cohort",
        "applies_when": lambda sql, intent: _is_cohort_query(sql, intent),
        "check": lambda sql, intent: bool(re.search(r'countDistinct\s*\(\s*deal_id\s*\)', sql, re.I)),
        "message": "Cohort funnel is not using countDistinct(deal_id).",
    },
    {
        "id": "mql_date_entered_filter",
        "section": "MQL filter 1 — date_entered_... anchor",
        "applies_when": lambda sql, intent: intent.get("metric") == "mql" and "hs_analytics.contacts" in sql,
        "check": lambda sql, intent: "date_entered_marketing_qualified_lead_lifecycle_stage_pipeline" in sql,
        "message": "MQL query missing `date_entered_marketing_qualified_lead_lifecycle_stage_pipeline` filter.",
    },
    {
        "id": "mql_company_priority_filter",
        "section": "MQL filter 2 — company_priority IN ('P1'...'P7')",
        "applies_when": lambda sql, intent: intent.get("metric") == "mql" and "hs_analytics.contacts" in sql,
        "check": lambda sql, intent: bool(re.search(r"company_priority\s+IN", sql, re.I)),
        "message": "MQL query missing `company_priority IN ('P1',...,'P7')` filter.",
    },
    {
        "id": "mql_bad_data_filter",
        "section": "MQL filter 3 — excludes 'Bad Data' lead status",
        "applies_when": lambda sql, intent: intent.get("metric") == "mql" and "hs_analytics.contacts" in sql,
        "check": lambda sql, intent: bool(
            re.search(r"lead_status\s*!=\s*'Bad Data'", sql, re.I)
            or re.search(r"lead_status\s+NOT\s+IN\s*\(\s*'Bad Data'", sql, re.I)
        ),
        "message": "MQL query missing a `lead_status != 'Bad Data'` or `lead_status NOT IN ('Bad Data')` filter.",
    },
    {
        "id": "mql_no_quarter_divide",
        "section": "MQL — never derive quarterly target by /4",
        "applies_when": lambda sql, intent: intent.get("metric") == "mql",
        "check": lambda sql, intent: not bool(re.search(r"/\s*4\b", sql)),
        "message": "MQL target appears derived by dividing an annual target by 4. Filter the target table to the exact quarter instead.",
    },
    {
        "id": "mql_deal_association_table",
        "section": "MQL-to-deal linkage — must use gs_DealContactAssociation",
        "applies_when": lambda sql, intent: intent.get("mql_needs_deal_join"),
        "check": lambda sql, intent: "gs_DealContactAssociation" in sql,
        "message": "Query links MQLs to deals but doesn't reference kore_ai_hubspot.gs_DealContactAssociation.",
    },
    {
        "id": "mql_deal_association_date_window",
        "section": "MQL-to-deal linkage — association must be date-windowed",
        "applies_when": lambda sql, intent: intent.get("mql_needs_deal_join") and "gs_DealContactAssociation" in sql,
        "check": lambda sql, intent: bool(re.search(r"createdate\s*>=", sql, re.I)),
        "message": "gs_DealContactAssociation is referenced but not filtered by createdate.",
    },
    {
        "id": "mql_deal_left_join",
        "section": "MQL-to-deal linkage — must LEFT JOIN, not INNER JOIN",
        "applies_when": lambda sql, intent: intent.get("mql_needs_deal_join") and "gs_DealContactAssociation" in sql,
        "check": lambda sql, intent: _mql_association_joined_via_left_join(sql),
        "message": "MQL-to-deal query must use LEFT JOIN — an INNER JOIN silently drops MQLs with no matched deal.",
    },
]


def validate_sql_against_rules(sql: str, user_message: str) -> List[str]:
    """The core check function, ported as-is: given SQL and the original
    question, return every violation message."""
    intent = detect_intent(user_message, sql)
    violations = []
    for rule in RULES:
        try:
            applies = _call(rule["applies_when"], sql, intent)
        except Exception:
            applies = False
        if not applies:
            continue
        try:
            ok = _call(rule["check"], sql, intent)
        except Exception:
            ok = False
        if not ok:
            violations.append(f"[{rule['id']}] {rule['section']}: {rule['message']}")
    return violations


# =============================================================================
# GRAPH NODE — adapts the above to this pipeline's GraphState interface
# =============================================================================

def validate_sql(state: GraphState) -> GraphState:
    """
    The node the graph actually calls. Runs the full real rule set
    against the SQL the SQL Agent just generated, using the original
    question for intent detection. Same _fail() pattern as before: mark
    invalid, log why, bump the retry counter, all in one place.
    """
    sql = state.get("sql", "")
    question = state.get("question", "")

    if not sql or not sql.strip():
        _fail(state, ["Query is empty."])
        return state

    violations = validate_sql_against_rules(sql, question)

    from governance.audit_log import log_rule_audit  # lazy: avoids a circular import
    log_rule_audit(sql, violations, "pre_execute", question)

    if violations:
        _fail(state, violations)
    else:
        state["sql_valid"] = True
        state["sql_validation_errors"] = []

    return state


def _fail(state: GraphState, errors: list) -> None:
    state["sql_valid"] = False
    state["sql_validation_errors"] = errors
    state["sql_retry_count"] = state.get("sql_retry_count", 0) + 1
