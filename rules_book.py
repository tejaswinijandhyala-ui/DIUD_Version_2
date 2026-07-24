"""
rules/rules_book.py — the single source of truth for business logic.

This is the real Kore.ai rulebook, extracted from two places:
  1. main.py's system prompt (§3-§14) — the full prose rules Claude was
     given directly.
  2. rules.py's RULEBOOK dict — the condensed versions already used by
     the lookup_business_rule tool.

Where the two disagreed on emphasis, the more detailed main.py version
won, since that's where the worked examples and the "confirmed real bug"
notes live — those notes exist because a specific wrong answer already
shipped once from getting this exact rule wrong, so they're kept
verbatim rather than summarized away.

Claude is never allowed to invent any of this. It can only receive it,
through tools/rule_loader.py, and apply exactly what it's given.
"""

RULES_BOOK = {

    # ---- Always-on filters, regardless of what's being asked ----------

    "mandatory_base_filters": {
        "description": "Filters required on every single query against hs_analytics.deals, no exceptions.",
        "rule": (
            "Every query against hs_analytics.deals must include ALL of: "
            "pipeline = 'default'; deal_stage <> 'Duplicate Record'; "
            "CASE WHEN deal_type IS NULL THEN 'Not Assigned' ELSE deal_type END "
            "NOT IN ('Partner-Led SMB'); "
            "toInt64(deal_id) IN (SELECT DISTINCT toInt64(deal_id_hs) FROM "
            "kore_ai_hubspot.gs_deal_ids_hs) — the valid-deal-ID allowlist; "
            "FINAL on every hs_analytics.* table reference; and "
            "countDistinct(deal_id) for aggregation, never count() or count(deal_id)."
        ),
    },

    "deduplication": {
        "description": "Preventing duplicate-counted rows across joins and tables.",
        "rule": (
            "FINAL is required on every hs_analytics.* table reference. "
            "countDistinct(deal_id) or countDistinct(contact_id) is required for all "
            "deal/contact aggregations — never count() or count(deal_id) as a plain "
            "count. Subqueries against kore_ai_hubspot.gs_DealContactAssociation must "
            "use SELECT DISTINCT. Target table queries must GROUP BY the target's own "
            "dimension columns before SUMming — never join raw deal rows directly to a "
            "target table row."
        ),
    },

    # ---- Fiscal year & dates -------------------------------------------

    "fiscal_year": {
        "description": "Fiscal year boundaries, quarter formula, and the anchor-date rule.",
        "rule": (
            "Fiscal year starts April 1. FY27 = Apr 2026-Mar 2027. Formula: "
            "toYear(date) + if(toMonth(date) >= 4, 1, 0). Quarters: Q1=Apr/May/Jun, "
            "Q2=Jul/Aug/Sep, Q3=Oct/Nov/Dec, Q4=Jan/Feb/Mar. Target tables store fy as "
            "a string like 'FY27' and quarter as 'Q1' — when joining actuals to "
            "targets, CAST(fy AS INT) to get the numeric year (2027), never compare "
            "against the string form. "
            "CANONICAL ANCHOR RULE: 'how many X% deals have we generated/created this "
            "FY' always anchors on became_X_deal_date (when the deal first reached "
            "that stage) — never create_date, never close_date. This must stay the "
            "same anchor every time the same question is asked, in the same session "
            "or a new one; if unsure which anchor a follow-up should use, reuse "
            "whatever anchor the most recent equivalent question in the conversation "
            "used, rather than re-deriving it from scratch."
        ),
    },

    "date_casting": {
        "description": "Mandatory date-column casting and the sentinel-date convention.",
        "rule": (
            "Every date comparison on close_date or became_<N>_deal_date must use "
            "CAST(LEFT(coalesce(column, '1900-01-01'), 10) AS DATE) — never compare "
            "the raw string. The sentinel '1900-01-01' means the date was never set "
            "(the NULL equivalent for these columns). Check with != '1900-01-01', "
            "never IS NOT NULL, for became_<N>_deal_date columns specifically. Never "
            "hardcode a specific date for 'current fiscal year start' — compute it "
            "dynamically. A hardcoded date going stale by a full year is a confirmed "
            "real bug that has already happened."
        ),
    },

    # ---- The three query patterns ---------------------------------------

    "pipegen_funnel": {
        "description": "Pattern A — cumulative pipeline generation / funnel stage counts.",
        "rule": (
            "Use for questions like 'how many deals reached X%', 'pipegen at Y%', "
            "'funnel breakdown', 'stage counts', 'conversion from X% to Y%'. A deal "
            "counts at stage N if it has EVER reached N or beyond — this is a "
            "cumulative OR-chain across all became_<N>_deal_date columns from N "
            "upward, combined with a deal_stage IN (...) check for deals already "
            "past that stage. This is NOT the same as cohort filtering (see "
            "cohort_funnel) — do not add a NOT IN exclusion of earlier stages here. "
            "The FY/quarter anchor (the column inside toYear(...)) must match the "
            "stage the user asked about — e.g. a '40% funnel' question anchors on "
            "became_40_deal_date. If no stage was specified, default the anchor to "
            "became_20_deal_date, never became_10_deal_date."
        ),
    },

    "active_pipeline": {
        "description": "Pattern B — deal-level detail / active pipeline, and the canonical 'Active Deals' definition.",
        "rule": (
            "CORE DEFINITION: deal_stage IN the 20%-75% active stages AND "
            "close_date falls within the current fiscal year. Those two "
            "conditions are the heart of every active-pipeline question — "
            "everything below is what 'the current fiscal year' and 'the active "
            "stages' precisely mean, and the additional conditions that make the "
            "count trustworthy rather than just close. "
            "Use for 'show me the deals', 'list active pipeline', 'stalled deals', "
            "'days in stage', 'BANT status', 'AE deal list'. One row per deal. "
            "Primary filter is close_date, not became_<N>_deal_date. "
            "Active pipeline stages: '20% - Solution', '30% - Proof', "
            "'40% - Proposal', '60% - Price Negotiation', '75% - Contract Review'. "
            "CANONICAL 'ACTIVE DEALS' DEFINITION (any 'active'-flavored question) — "
            "countDistinct(deal_id) for deals meeting ALL of: (1) deal_stage in the "
            "active-stage set above; (2) pipeline = 'default'; (3) close_date within "
            "the reporting period (e.g. current fiscal year — never omit this even "
            "for a 'right now' snapshot; 'active right now' still means 'active AND "
            "expected to close within the period being reported on'); (4) deal_type "
            "<> 'Partner-Led SMB'; (5) any additional dashboard-specific filters "
            "actually called for; (6) always countDistinct(deal_id), never plain "
            "count(*). Do not drop the close_date bound just because the question "
            "says 'right now' instead of naming a fiscal year — a query missing any "
            "one of conditions 1-4 is answering a different, looser question that "
            "only looks similar."
        ),
    },

    "attainment": {
        "description": "Pattern C — actuals vs target / attainment queries.",
        "rule": (
            "Use for 'attainment', 'vs target', 'quota', 'coverage ratio', "
            "'on track', 'gap to target'. Build actuals and targets as two "
            "INDEPENDENT CTEs, joined only at the end with LEFT JOIN — never join "
            "raw deal rows directly to a target table row (fan-out risk). Every "
            "division must be wrapped: nullIf(denominator, 0). Filter the target "
            "table to the exact quarter requested — never derive a quarterly figure "
            "by dividing an annual target by 4. The became_<N>_deal_date anchor must "
            "match the stage being targeted (10% targets anchor on "
            "became_10_deal_date, 20% on became_20_deal_date, etc.) — default to 20% "
            "if the user didn't specify a stage. Source mapping for Pattern C MERGES "
            "'Executive Outreach' and 'Investor' into 'Executive Outreach' — this "
            "differs from Patterns A and B, where they stay separate, because it "
            "matches how the target table buckets them. "
            "IF A TARGET QUERY RETURNS 0 ROWS: immediately run a diagnostic query "
            "(SELECT DISTINCT fy, quarter FROM <target_table>) to find the correct "
            "fy/quarter string format, then re-run the original query with the "
            "corrected filter — do this before writing any response, never say "
            "'let me check' without having already checked. A single-CTE query that "
            "only aggregates a target column, with no accompanying actuals CTE in "
            "the same query, is always an incomplete attempt at an attainment "
            "question, even as an intermediate step toward a follow-up call."
        ),
    },

    "cohort_funnel": {
        "description": "True cohort funnel queries — distinct from Pattern A's cumulative counting.",
        "rule": (
            "Use only for genuine cohort questions ('cohort starting at X%', "
            "'X% cohort to Closed Won') — not for 'pipegen conversion funnel' style "
            "requests, which are Pattern A. Required shape: single WITH-cohort CTE; "
            "became_<N>_deal_date != '1900-01-01' as the cohort anchor (the "
            "sentinel check, never IS NOT NULL); deal_stage NOT IN (...) excluding "
            "every stage before the cohort's starting stage; GROUP BY deal_stage; "
            "countDistinct(deal_id), never count(*) or count(deal_id). Exclusion "
            "lists by starting stage: 10%->CW excludes 1%,5%; 20%->CW excludes "
            "1%,5%,10%; 30%->CW excludes 1%,5%,10%,20%; 40%->CW excludes "
            "1%,5%,10%,20%,30%; 60%->CW excludes 1%,5%,10%,20%,30%,40%; 75%->CW "
            "excludes 1%,5%,10%,20%,30%,40%,60%."
        ),
    },

    # ---- Target tables ----------------------------------------------------

    "target_table_casting": {
        "description": "Mandatory casting for all target table columns.",
        "rule": (
            "Every column in every target table (gs_pipeline_quotas_v1, "
            "gs_partner_targets_region_wise, gs_partner_targets_psd, "
            "gs_marketing_targets, gs_closed_won_quotas) is Nullable(String), even "
            "when it holds a number. Always cast before any arithmetic: "
            "SUM(toFloat64OrZero(column)). Raw arithmetic on an uncast column "
            "silently produces NULL or a type error instead of the real value — this "
            "is the single most common cause of a target reading as zero when the "
            "real target isn't zero."
        ),
    },

    "target_table_tiers": {
        "description": "The three target tiers (L2/L1/Committed) and how to tell them apart by column name.",
        "rule": (
            "Three tiers exist: L2 (base, DEFAULT), L1 (stretch), Committed. Always "
            "use L2 unless the user explicitly says 'L1', 'stretch', or 'committed'. "
            "Never mix tiers in one query unless explicitly asked to compare them. "
            "Column naming differs by table: in gs_pipeline_quotas_v1 (T1), L2 has "
            "no prefix/suffix (amount_target_20), L1 has suffix _l1 "
            "(amount_target_20_l1), Committed has suffix _committed. In "
            "gs_partner_targets_region_wise (T2), L2 has prefix l2_ "
            "(l2_amount_target_20), L1 has prefix l1_, Committed has prefix "
            "committed_. gs_partner_targets_psd (T3) has COMMITTED ONLY — no L1/L2 "
            "columns exist there; use T2 filtered by partner_team for L1/L2 at PSD "
            "level. gs_marketing_targets has no Committed tier and no L1 Amount "
            "columns at all — only L1 deal-count columns."
        ),
    },

    "closed_won": {
        "description": "Closed Won revenue rules and quota table.",
        "rule": (
            "Closed Won = deal_stage IN ('Closed Won', '90% - Deal Desk Review'), "
            "with close_date between the fiscal year bounds. Quota source: "
            "kore_ai_hubspot.gs_closed_won_quotas, cast with toFloat64OrZero() — "
            "this table has only ONE quota tier, no L1/L2/Committed split. Join to "
            "deals via ae = deal_owner. Columns: assigned_amount_quota, "
            "assigned_deals_quota, annualized_amount_quota, annualized_deals_quota."
        ),
    },

    "partner_targets": {
        "description": "Partner pipeline target rules, including a confirmed mapping bug.",
        "rule": (
            "T2 (gs_partner_targets_region_wise) uses l2_/l1_/committed_ prefixes; "
            "committed_amount_target_10 and committed_amount_target_5 do NOT exist "
            "in T2. T3 (gs_partner_targets_psd) is COMMITTED ONLY. Filter "
            "partner_team_type IN ('Hyperscaler','GSI/SI','Reseller/BPO/TSD') as "
            "needed. CONFIRMED BUG TO AVOID: the actuals-side label "
            "'Partner - Non Hyperscaler' never exists as a literal value in the "
            "target table's partner_team_type column — filtering on that literal "
            "string returns zero rows every time, which is NOT the same as the "
            "target actually being zero. It maps to the combination "
            "partner_team_type IN ('GSI/SI','Reseller/BPO/TSD') — everything in "
            "that column except 'Hyperscaler'. All target columns here are "
            "Nullable(String): always SUM(toFloat64OrZero(col))."
        ),
    },

    # ---- MQL ----------------------------------------------------------------

    "mql": {
        "description": "MQL calculation rules and MQL-to-deal linkage.",
        "rule": (
            "MQL actuals from hs_analytics.contacts require ALL THREE filters: "
            "(1) date_entered_marketing_qualified_lead_lifecycle_stage_pipeline IS "
            "NOT NULL; (2) company_priority IN ('P1','P2','P3','P4','P5','P6','P7'); "
            "(3) lead_status != 'Bad Data'. Missing any one inflates or deflates "
            "the count. MQL targets: filter gs_marketing_targets to the exact "
            "quarter, never divide an annual target by 4. "
            "MQL-TO-DEAL LINKAGE (required whenever a question connects MQLs to the "
            "deals they became): never answer with only an MQL count or only a deal "
            "count alone. The join MUST go through "
            "kore_ai_hubspot.gs_DealContactAssociation (never inferred from "
            "company_name, owner, or any other matching field), MUST use LEFT JOIN "
            "so MQLs with no matched deal count as 'MQL without a deal' instead of "
            "being silently dropped, and the association itself MUST be windowed by "
            "createdate matching the same date range as the MQL filter. "
            "CONFIRMED BUG TO AVOID: gs_DealContactAssociation.deal_id matches "
            "hs_analytics.deals.deal_id — never record_id. Joining against record_id "
            "instead produces wrong or incomplete matches even though the query runs "
            "without error."
        ),
    },

    # ---- Dimension mappings ---------------------------------------------

    "dimension_mappings": {
        "description": "Standard region, industry, and priority groupings applied across all patterns.",
        "rule": (
            "Apply these in the SELECT clause, not WHERE, unless the user is "
            "explicitly filtering on the mapped label. REGION: 'japac'->'JAPAC', "
            "'Africa'->'Middle East', 'india___sea'->'ISEA'. INDUSTRY: "
            "('Financial Services','Banking','Insurance')->'Financial Services'; "
            "('Manufacturing Discreet','Manufacturing Process','CPG')->'Manufacturing'; "
            "('Hi-Tech','Telecom / Media / Entertainment')->'TMT'; "
            "('Business Services','Government','Energy & Utilities','Education',"
            "'Restaurants','null','Energy') or NULL->'Other'. ACCOUNT PRIORITY: "
            "('P1'-'P4')->'P1-P4', ('P5'-'P7')->'P5-P7', ('P8'-'P10')->'P8-P10'. "
            "PARTNER NAME: COALESCE(reseller_partner_associated, "
            "referral_partner_associated, 'Not Available') — never check only one "
            "of the two source columns; a deal with a referral partner but no "
            "reseller partner (or vice versa) still has a real partner and must not "
            "be counted as unassigned."
        ),
    },

    "breakdown_dimension_rule": {
        "description": "When to GROUP BY a dimension, and when not to.",
        "rule": (
            "A bare count/total/value question with no dimension named or clearly "
            "implied must return ONE aggregate row — never GROUP BY region, source, "
            "industry, or any other dimension just because the column exists or was "
            "used in a recent turn. This applies even if a breakdown would 'look "
            "nicer.' 'How many X deals this FY' -> one number, no GROUP BY. "
            "'How many X deals by region' -> GROUP BY region. When a breakdown IS "
            "requested, use the exact dimension named — do not substitute a "
            "different one, and do not add a second, unrequested dimension on top "
            "of it."
        ),
    },

    # ---- Dashboards -------------------------------------------------------

    "dashboard_definitions": {
        "description": "The nine standard dashboards and what each one tracks.",
        "rule": (
            "EOP: pipeline vs EOP target, active stages only, current quarter end "
            "window. EXEC KPI: total active pipeline, closed won, CW attainment %, "
            "win rate, coverage. CS: renewals, upsells, expansions. "
            "GLOBAL PIPELINE GOVERNANCE: executive cross-region/source/partner view. "
            "GLOBAL PIPEGEN: 5/10/20% pipeline amount + deal count vs "
            "gs_pipeline_quotas_v1, attainment %, funnel conversion. "
            "PARTNERSHIP: partner pipeline vs partner target tables, PSD/hyperscaler "
            "splits. MARKETING: MQL actual vs target (see mql rule), source/region "
            "performance. AE FOCUS: AE pipeline, CW ARR, quota attainment, win "
            "rate, avg deal size, sales cycle. BDR FOCUS: meetings created, "
            "opportunities generated, PipeGen by BDR, target attainment. If it's "
            "unclear which dashboard a question refers to, ask the user."
        ),
    },

    "deal_stage_reference": {
        "description": "Full deal stage list, active-pipeline stage category rollup, and health benchmarks.",
        "rule": (
            "Full stage allowlist: '1% - IQM Scheduled', '5% - IQM Held', "
            "'10% - Discovery', '20% - Solution', "
            "'30% - Proof', '40% - Proposal', '60% - Price Negotiation', "
            "'75% - Contract Review', '90% - Deal Desk Review', 'Closed Won', "
            "'Closed Lost', \"Didn't Qualify\", 'Prospect Disengaged', 'Deal on Hold'. "
            "Note: the §3 mandatory base-filter allowlist used for pipegen/funnel "
            "queries starts at '10% - Discovery' — '1%' and '5%' only appear in "
            "Pattern B's deal-level detail view and the health-benchmark table "
            "below, not in Pattern A funnel counting. "
            "Stage category rollup: Active Pipeline = 20/30/40/60/75%; Fallen Out = "
            "Prospect Disengaged, Closed Lost, Didn't Qualify; Closed Won = "
            "90% - Deal Desk Review, Closed Won; everything else (including "
            "1% and 5%) = Pre-Qualification. "
            "Days-in-stage health benchmarks (days / green / yellow / red): "
            "1% IQM Scheduled 7 / <10 / <14 / >=14; 5% IQM Held 21 / <31 / <42 / >=42; "
            "10% Discovery 28 / <42 / <56 / >=56; 20% Solution 41 / <61 / <82 / >=82; "
            "30% Proof 15 / <22 / <30 / >=30; 40% Proposal 29 / <43 / <58 / >=58; "
            "60% Price Negotiation 27 / <40 / <54 / >=54; "
            "75% Contract Review 34 / <51 / <68 / >=68."
        ),
    },

    # ---- SQL hygiene ------------------------------------------------------

    "sql_guardrails": {
        "description": "General SQL generation guardrails that apply to every query, regardless of pattern.",
        "rule": (
            "SELECT or WITH only — never INSERT, UPDATE, DELETE, DROP, ALTER, "
            "TRUNCATE. FINAL on every hs_analytics.* table. All mandatory base "
            "filters on every deals query. countDistinct(deal_id), never count() or "
            "count(deal_id). No LIMIT unless the user explicitly says 'top N' or "
            "'first N'. All target table numeric columns: "
            "SUM(toFloat64OrZero(col)). Every division wrapped with "
            "nullIf(denominator, 0). Date columns: "
            "CAST(LEFT(coalesce(col,'1900-01-01'),10) AS DATE). Never compute a "
            "quarterly target by dividing an annual figure by 4. Always state the "
            "total row count in the answer."
        ),
    },

    "response_format": {
        "description": "The mandatory structure for every database-backed answer (used by narration, not SQL generation).",
        "rule": (
            "Every answer follows four parts, always in this order: (1) DIRECT "
            "ANSWER FIRST — one bolded sentence answering exactly what was asked, "
            "in the exact shape it was asked (a single-number question gets one "
            "bolded number, not a breakdown, even if the underlying data has other "
            "dimensions available); (2) BRIEF EXPLANATION — 1-3 sentences of real "
            "context, not a restatement of the number; (3) VISUAL, if one adds "
            "value — never a substitute for parts 1 and 2, write the full text "
            "answer as if no chart were going to appear at all; (4) FILTERS "
            "APPLIED — an explicit list of every active filter (pattern used, FY "
            "anchor column, FY/quarter/region/source/stage filters) so the user can "
            "verify the answer matches what they expected."
        ),
    },
}


def get_rule(key: str):
    """Fetch a single rule by its key, or None if it doesn't exist."""
    return RULES_BOOK.get(key)


def all_rule_keys():
    """
    Every rule key currently in the book. Used by the loader to know what's
    available, and later by the Governance Agent to check whether a
    proposed new rule actually already exists in some form.
    """
    return list(RULES_BOOK.keys())
