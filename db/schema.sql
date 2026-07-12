-- db/schema.sql
-- Run this once against your ClickHouse instance before starting the app.
-- Every table here corresponds to exactly one file that reads/writes it:
--   ri_copilot_sessions      <- memory/session_store.py (conversation history)
--   ri_copilot_query_results <- memory/session_store.py (export data)
--   ri_copilot_feedback      <- governance/feedback_store.py (immutable log)
--   ri_copilot_suggestions   <- governance/approval_queue.py (append-only, event-sourced)
--   ri_copilot_observability <- observability/logger.py (per-node + per-request logs)

-- Conversation history. One row per turn (user question or assistant response).
CREATE TABLE IF NOT EXISTS ri_copilot_sessions
(
    conversation_id String,
    role             String,   -- "user" | "assistant"
    content          String,
    created_at       UInt32    -- unix timestamp
)
ENGINE = MergeTree
ORDER BY (conversation_id, created_at);


-- The most recent validated query result per conversation, kept around
-- specifically so a later "export this as CSV" request has real data to
-- work with without re-running the query. Only the latest result per
-- conversation actually matters for export, so this table isn't meant
-- to accumulate a full history the way ri_copilot_sessions does.
CREATE TABLE IF NOT EXISTS ri_copilot_query_results
(
    conversation_id String,
    sql              String,
    columns          String,   -- JSON-encoded list of column names
    rows             String,   -- JSON-encoded list of row objects
    total_rows       UInt32,
    captured_at       UInt32
)
ENGINE = MergeTree
ORDER BY (conversation_id, captured_at);


-- Immutable feedback log. No UPDATE or DELETE ever happens against this
-- table from the application code -- see governance/feedback_store.py's
-- module docstring for why that's a deliberate design choice, not an
-- oversight.
CREATE TABLE IF NOT EXISTS ri_copilot_feedback
(
    feedback_id           String,
    conversation_id       String,
    question              String,
    sql                    String,
    query_result_summary  String,
    response               String,
    feedback_type          String,   -- correct | incorrect | better_explanation | wrong_sql | suggest_rule
    feedback_note          String,
    user_id                String,
    created_at             UInt32
)
ENGINE = MergeTree
ORDER BY created_at;


-- Governance suggestions. Deliberately APPEND-ONLY and event-sourced --
-- every row is a complete snapshot of a suggestion's state at that
-- moment, never an edit of a previous row. "Current state" of a
-- suggestion means "the most recent row for that suggestion_id" (see
-- approval_queue.py's use of ClickHouse's LIMIT BY). This is what makes
-- the governance principles (every decision recorded and auditable,
-- rejected suggestions persist with a reason) true at the data-model
-- level, not just as application logic that could be bypassed.
CREATE TABLE IF NOT EXISTS ri_copilot_suggestions
(
    suggestion_id           String,
    suggestion_type         String,   -- new_rule | prompt_improvement | validator_rule
    description              String,
    proposed_change          String,
    evidence_feedback_ids    String,   -- JSON-encoded list of feedback_ids
    status                    String,   -- pending | approved | rejected
    decided_by                String,
    decision_reason           String,
    created_at                UInt32
)
ENGINE = MergeTree
ORDER BY (suggestion_id, created_at);


-- Per-node and per-request observability. Two kinds of rows land here:
-- one row per graph node execution (from observability/logger.py's
-- logged_node() decorator), and one summary row per completed request
-- (from log_final_outcome()). Distinguish them by whether `event_type`
-- is set -- node-level rows leave it empty, summary rows set it to
-- "final_outcome".
CREATE TABLE IF NOT EXISTS ri_copilot_observability
(
    logged_at              UInt32,
    node                    String DEFAULT '',
    event_type              String DEFAULT '',
    conversation_id         String DEFAULT '',
    question                 String DEFAULT '',
    intent                   String DEFAULT '',
    duration_ms              UInt32 DEFAULT 0,
    error                     String DEFAULT '',
    final_status              String DEFAULT '',
    chart_type                String DEFAULT '',
    sql_retry_count            UInt8 DEFAULT 0,
    data_retry_count            UInt8 DEFAULT 0,
    response_retry_count         UInt8 DEFAULT 0,
    errors                        String DEFAULT ''  -- JSON-encoded list
)
ENGINE = MergeTree
ORDER BY logged_at;
