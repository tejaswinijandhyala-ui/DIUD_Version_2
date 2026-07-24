"""
schema/schema_book.py — the single source of truth for database structure.

Built from DIUD's actual production system prompt (§3 and §4), not
placeholders. Every table, column, and note here reflects the real
ClickHouse schema Claude has been working against.

One thing kept deliberately here, not summarized away: every target
table's numeric columns are Nullable(String), not a real numeric type —
that's not incidental schema trivia, it's the single most common source
of a target silently reading as zero (see the target_table_casting rule
in rules/rules_book.py). The SQL Agent needs to see this at the schema
level, not just the rules level, since it's fundamentally a fact about
the columns themselves.
"""

SCHEMA_BOOK = {
    "hs_analytics.deals": {
        "description": "Core deals fact table. Always query with FINAL.",
        "columns": {
            "deal_id": "Primary key, unique per deal",
            "deal_name": "Deal name",
            "deal_owner": "Owner ID — join to hs_analytics.owners.id (as VARCHAR)",
            "deal_stage": "Current stage — see deal_stage_reference rule for the full allowlist",
            "deal_type": "Deal type; NULL should be treated as 'Not Assigned'",
            "pipeline": "Pipeline name — must equal 'default' for standard reporting",
            "amount": "Deal value",
            "region": "Raw region code — apply dimension_mappings before display",
            "deal_source_rollup": "Source category — mapping differs between Pattern A/B and Pattern C, see rules",
            "kore_primary_industry": "Raw industry — apply dimension_mappings before display",
            "account_priority_level": "P1-P10 — apply dimension_mappings grouping before display",
            "create_date": "When the deal record was created in the CRM",
            "close_date": "Expected or actual close date — primary filter for Pattern B",
            "became_5_deal_date": "Sentinel-dated: '1900-01-01' means never reached this stage",
            "became_10_deal_date": "Sentinel-dated: '1900-01-01' means never reached this stage",
            "became_20_deal_date": "Sentinel-dated: '1900-01-01' means never reached this stage",
            "became_30_deal_date": "Sentinel-dated: '1900-01-01' means never reached this stage",
            "became_40_deal_date": "Sentinel-dated: '1900-01-01' means never reached this stage",
            "became_60_deal_date": "Sentinel-dated: '1900-01-01' means never reached this stage",
            "became_75_deal_date": "Sentinel-dated: '1900-01-01' means never reached this stage",
            "ai_for_x": "Flag/category field used in some breakdowns",
            "deal_url": "Link to the deal record",
            "country": "Deal's country",
            "hubspot_team": "Team ID — join to kore_ai_hubspot.gs_Teams.team_id",
            "is_this_a_deal_with_inception": "Yes/No — excluded only if the user explicitly asks",
            "last_contacted": "Sentinel-dated like became_X columns",
            "is_there_a_confirmation_of_budget": "Part of BANT — 'Yes'/other",
            "who_is_the_decision_maker": "Part of BANT",
            "use_case": "Part of BANT",
            "what_is_the_estimated_timeline": "Part of BANT",
            "forecast_amount": "AE/management forecast amount",
            "forecast_probability": "AE/management forecast probability",
            "management_forecast": "Management forecast category",
            "ae_forecast": "AE forecast category",
        },
        "primary_key": "deal_id",
        "notes": "All mandatory_base_filters must be applied to every query against this table. See rules_book.mandatory_base_filters.",
    },

    "hs_analytics.owners": {
        "description": "AE/owner master data. Always query with FINAL.",
        "columns": {
            "id": "Primary key — join target for deals.deal_owner (cast owners.id to VARCHAR)",
            "firstName": "First name",
            "lastName": "Last name",
            "email": "Email",
        },
        "primary_key": "id",
        "notes": "Join pattern: LEFT JOIN hs_analytics.owners o FINAL ON d.deal_owner = CAST(o.id AS VARCHAR)",
    },

    "hs_analytics.companies": {
        "description": "Company/account master data. Always query with FINAL.",
        "columns": {
            "company_id": "Primary key",
            "name": "Company name",
            "domain": "Company domain",
            "industry": "Raw industry field",
            "country": "Company's country",
            "city": "Company's city",
        },
        "primary_key": "company_id",
    },

    "hs_analytics.contacts": {
        "description": "Contact/lead master data. Always query with FINAL. Source table for MQL calculations.",
        "columns": {
            "contact_id": "Primary key",
            "email": "Contact email",
            "first_name": "First name",
            "last_name": "Last name",
            "company_name": "Associated company name (do not use for deal linkage — see mql rule)",
            "company_priority": "P1-P10 — MQL rule requires IN ('P1'...'P7')",
            "region": "Contact's region",
            "original_source": "Marketing source",
            "lead_status": "MQL rule requires excluding 'Bad Data'",
            "lifecycle_stage": "HubSpot lifecycle stage",
            "date_entered_marketing_qualified_lead_lifecycle_stage_pipeline": "MQL anchor date — MQL rule requires this IS NOT NULL",
        },
        "primary_key": "contact_id",
        "notes": "See the mql rule in rules_book.py for the three mandatory filters on this table.",
    },

    "kore_ai_hubspot.gs_DealContactAssociation": {
        "description": "Many-to-many link between contacts and deals. Required for any MQL-to-deal question.",
        "columns": {
            "contact_id": "Join to hs_analytics.contacts.contact_id",
            "deal_id": "Join to hs_analytics.deals.deal_id — NEVER record_id (confirmed real bug, see mql rule)",
            "createdate": "Must be date-windowed to match the MQL filter's date range",
        },
        "primary_key": "contact_id, deal_id",
        "notes": "Always use DISTINCT in subqueries against this table. Always LEFT JOIN, never INNER JOIN, when linking MQLs to deals.",
    },

    "kore_ai_hubspot.gs_deal_ids_hs": {
        "description": "Allowlist of valid deal IDs. Used in mandatory_base_filters on every deals query.",
        "columns": {
            "deal_id_hs": "The valid deal ID — every deals query filters toInt64(deal_id) IN (SELECT DISTINCT toInt64(deal_id_hs) FROM this table)",
        },
        "primary_key": "deal_id_hs",
    },

    "kore_ai_hubspot.gs_Teams": {
        "description": "Team lookup, joined from deals.hubspot_team.",
        "columns": {
            "team_id": "Primary key",
            "name": "Team name",
        },
        "primary_key": "team_id",
    },

    # ---- Target tables — all Nullable(String) numeric columns ----------

    "kore_ai_hubspot.gs_pipeline_quotas_v1": {
        "description": "T1 — org-wide pipeline generation targets by region/source/stage. Use for pipegen attainment, EOP tracking, coverage.",
        "columns": {
            "id": "Primary key",
            "fy": "Fiscal year as string, e.g. 'FY27' — CAST(fy AS INT) when joining to actuals",
            "quarter": "e.g. 'Q1'",
            "month": "e.g. 'Apr'",
            "region": "Region",
            "source": "Source category",
            "amount_target_20": "L2/default tier — Nullable(String), always cast toFloat64OrZero",
            "deals_target_20": "L2/default tier",
            "amount_target_10": "L2/default tier",
            "deals_target_10": "L2/default tier",
            "amount_target_5": "L2/default tier",
            "deals_target_5": "L2/default tier",
            "amount_target_20_l1": "L1/stretch tier",
            "amount_target_10_l1": "L1/stretch tier",
            "amount_target_5_l1": "L1/stretch tier",
            "amount_target_20_committed": "Committed tier",
            "amount_target_10_committed": "Committed tier",
            "amount_target_5_committed": "Committed tier",
        },
        "primary_key": "id",
        "notes": "See target_table_tiers and target_table_casting rules. Never join raw deal rows to this table — use an independent CTE.",
    },

    "kore_ai_hubspot.gs_partner_targets_region_wise": {
        "description": "T2 — region-level partner pipeline targets by partner type.",
        "columns": {
            "id": "Primary key",
            "fy": "Fiscal year as string",
            "quarter": "Quarter string",
            "region": "Region",
            "partner_team": "Partner team",
            "partner_team_type": "IN ('Hyperscaler','GSI/SI','Reseller/BPO/TSD') — never the literal 'Partner - Non Hyperscaler', see partner_targets rule",
            "hyperscaler_type": "Hyperscaler sub-type",
            "l2_amount_target_20": "L2/default tier",
            "l2_amount_target_10": "L2/default tier",
            "l2_amount_target_5": "L2/default tier",
            "l1_amount_target_20": "L1/stretch tier",
            "committed_amount_target_20": "Committed tier — note committed_amount_target_10 and _5 do NOT exist",
            "msft_c1_amount_target_20": "Microsoft hyperscaler-specific target",
            "aws_c1_amount_target_20": "AWS hyperscaler-specific target",
        },
        "primary_key": "id",
        "notes": "See target_table_tiers and partner_targets rules.",
    },

    "kore_ai_hubspot.gs_partner_targets_psd": {
        "description": "T3 — PSD (Partner Sales Director)-level partner targets. COMMITTED ONLY, no L1/L2.",
        "columns": {
            "id": "Primary key",
            "fy": "Fiscal year as string",
            "quarter": "Quarter string",
            "region": "Region",
            "partner_team": "Partner team",
            "psd": "PSD name",
            "hyperscaler_type": "Hyperscaler sub-type",
            "committed_amount_target_20": "Only tier available in this table",
            "committed_amount_target_10": "Only tier available in this table",
            "committed_amount_target_5": "Only tier available in this table",
        },
        "primary_key": "id",
        "notes": "For L1/L2 PSD-level targets, use gs_partner_targets_region_wise filtered by partner_team instead.",
    },

    "kore_ai_hubspot.gs_marketing_targets": {
        "description": "T4 — Marketing MQL and pipeline targets by source.",
        "columns": {
            "id": "Primary key",
            "fy": "Fiscal year as string",
            "quarter": "Quarter string",
            "region": "Region",
            "original_source": "Marketing source — join key for MQL actuals",
            "mql_target": "MQL count target",
            "amount_target_20": "L2/default tier",
            "deals_target_20": "L2/default tier",
            "l1_mql_target": "L1/stretch tier",
            "l1_deals_target_20": "L1/stretch tier",
        },
        "primary_key": "id",
        "notes": "No Committed tier and no L1 Amount columns exist in this table — only L1 deal-count columns.",
    },

    "kore_ai_hubspot.gs_closed_won_quotas": {
        "description": "Closed Won revenue quotas by AE. Single tier only — no L1/L2/Committed split.",
        "columns": {
            "fy": "Fiscal year as string",
            "quarter": "Quarter string",
            "region": "Region",
            "ae": "AE name — join to hs_analytics.deals.deal_owner",
            "role": "AE role",
            "manager": "AE's manager",
            "assigned_amount_quota": "Nullable(String), always cast toFloat64OrZero",
            "assigned_deals_quota": "Nullable(String), always cast toFloat64OrZero",
            "annualized_amount_quota": "Nullable(String), always cast toFloat64OrZero",
            "annualized_deals_quota": "Nullable(String), always cast toFloat64OrZero",
        },
        "primary_key": "ae, fy, quarter",
        "notes": "See closed_won rule. Join to deals via ae = deal_owner.",
    },
}

# Maps question keywords to the tables actually needed to answer them —
# same pattern as rule_loader.py's KEYWORD_TO_RULE, kept deliberately
# simple to start with.
RELEVANT_TABLES_BY_METRIC = {
    "pipeline": ["hs_analytics.deals"],
    "pipegen": ["hs_analytics.deals", "kore_ai_hubspot.gs_pipeline_quotas_v1"],
    "funnel": ["hs_analytics.deals"],
    "active pipeline": ["hs_analytics.deals"],
    "deal": ["hs_analytics.deals", "hs_analytics.owners"],
    "ae": ["hs_analytics.deals", "hs_analytics.owners"],
    "closed won": ["hs_analytics.deals", "kore_ai_hubspot.gs_closed_won_quotas"],
    "attainment": ["hs_analytics.deals"],
    "target": ["kore_ai_hubspot.gs_pipeline_quotas_v1"],
    "quota": ["kore_ai_hubspot.gs_closed_won_quotas"],
    "partner": ["hs_analytics.deals", "kore_ai_hubspot.gs_partner_targets_region_wise", "kore_ai_hubspot.gs_partner_targets_psd"],
    "hyperscaler": ["kore_ai_hubspot.gs_partner_targets_region_wise"],
    "psd": ["kore_ai_hubspot.gs_partner_targets_psd"],
    "mql": ["hs_analytics.contacts", "kore_ai_hubspot.gs_marketing_targets"],
    "marketing": ["hs_analytics.contacts", "kore_ai_hubspot.gs_marketing_targets"],
    "contact": ["hs_analytics.contacts"],
    "region": ["hs_analytics.deals"],
    "industry": ["hs_analytics.deals"],
    "company": ["hs_analytics.companies"],
    "team": ["kore_ai_hubspot.gs_Teams"],
    "cohort": ["hs_analytics.deals"],
    "bant": ["hs_analytics.deals"],
    "stalled": ["hs_analytics.deals"],
    "days in stage": ["hs_analytics.deals"],
}
