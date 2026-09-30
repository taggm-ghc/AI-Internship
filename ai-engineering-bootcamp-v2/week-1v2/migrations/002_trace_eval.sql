-- Week 4 TRACE Evaluation System Schema
-- Stores request/response traces for error analysis and eval suite

CREATE TABLE IF NOT EXISTS internship.traces (
    id text PRIMARY KEY,
    endpoint_type text NOT NULL CHECK (endpoint_type IN ('ask', 'ask_stream', 'agent')),
    user_input text NOT NULL,
    system_prompt text,

    -- Response envelope (structured JSON from the endpoint)
    response jsonb NOT NULL,

    -- RAG-specific fields (for /ask and /agent)
    retrieved_context jsonb,              -- array of retrieved chunks
    rag_status text CHECK (rag_status IN ('supported', 'insufficient', 'not_applicable')),

    -- Agent-specific fields (for /agent)
    agent_trace jsonb,                    -- array of Think/Act/Observe steps
    tool_calls jsonb,                     -- array of tool call summaries
    grounding text CHECK (grounding IN ('no_tool_call', 'tool_found_nothing', 'tool_error', 'tool_sources')),
    model_turns integer,

    -- Metadata
    model text,
    prompt_tokens integer,
    completion_tokens integer,
    total_tokens integer,
    latency_ms integer,
    cost_usd numeric(10, 6),
    embedding_cost_usd numeric(10, 6),

    -- Trace source / classification
    source text NOT NULL,                 -- 'harmony_sample', 'vera_live', etc.
    channel text,                         -- 'sms', 'web', 'voice' (when available)

    created_at timestamptz NOT NULL DEFAULT now(),

    -- Indexes for common queries
    CONSTRAINT traces_response_not_null CHECK (response IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS traces_source_created_idx ON internship.traces(source, created_at DESC);
CREATE INDEX IF NOT EXISTS traces_rag_status_idx ON internship.traces(rag_status);
CREATE INDEX IF NOT EXISTS traces_grounding_idx ON internship.traces(grounding);
CREATE INDEX IF NOT EXISTS traces_endpoint_idx ON internship.traces(endpoint_type);


-- Annotations: human labels and eval results for error analysis
CREATE TABLE IF NOT EXISTS internship.trace_annotations (
    trace_id text PRIMARY KEY REFERENCES internship.traces(id) ON DELETE CASCADE,

    -- Error Analysis (Trace & Read phase)
    open_code_notes text,                 -- free-text notes on what failed or succeeded

    -- Axial Coding (Analyze phase)
    failure_category text,                -- e.g., 'instruction_miss', 'hallucination', 'formatting_error'

    -- Binary Pass/Fail Judgment
    pass_fail boolean,
    reason_if_fail text,                  -- one-line reason why it failed (for debugging)

    -- Eval Suite Results (Codify phase)
    eval_results jsonb DEFAULT '{}',      -- {check_name: {pass: bool, reason: str}}

    -- Metadata
    analyst text,                         -- who added this annotation (optional)
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS annotations_category_idx ON internship.trace_annotations(failure_category);
CREATE INDEX IF NOT EXISTS annotations_pass_fail_idx ON internship.trace_annotations(pass_fail);


-- Failure taxonomy tracking (summary table for queries)
CREATE TABLE IF NOT EXISTS internship.failure_categories (
    category text PRIMARY KEY,
    description text,
    severity text CHECK (severity IN ('low', 'medium', 'high', 'critical')),
    examples text,                        -- comma-separated list of trace_ids for reference
    count_failing integer DEFAULT 0,
    count_total integer DEFAULT 0,
    last_updated timestamptz NOT NULL DEFAULT now()
);


-- Eval Check Results (one row per check per trace)
CREATE TABLE IF NOT EXISTS internship.eval_check_results (
    trace_id text NOT NULL REFERENCES internship.traces(id) ON DELETE CASCADE,
    check_name text NOT NULL,
    check_type text CHECK (check_type IN ('code_based', 'llm_judge')),
    pass boolean NOT NULL,
    reason text,                          -- one-line explanation of failure
    latency_ms integer,                   -- how long the check took
    created_at timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (trace_id, check_name)
);

CREATE INDEX IF NOT EXISTS check_results_check_name_idx ON internship.eval_check_results(check_name);
CREATE INDEX IF NOT EXISTS check_results_pass_idx ON internship.eval_check_results(pass);


-- Useful views for analysis and reporting

-- Summary: Pass rate by check
CREATE OR REPLACE VIEW internship.eval_check_summary AS
SELECT
    check_name,
    COUNT(*) as total_runs,
    SUM(CASE WHEN pass THEN 1 ELSE 0 END) as pass_count,
    SUM(CASE WHEN pass THEN 1 ELSE 0 END)::float / COUNT(*) as pass_rate
FROM internship.eval_check_results
GROUP BY check_name
ORDER BY check_name;


-- Summary: Failure categories ranked by frequency
CREATE OR REPLACE VIEW internship.failure_category_summary AS
SELECT
    failure_category,
    COUNT(*) as total,
    SUM(CASE WHEN pass_fail = false THEN 1 ELSE 0 END) as failures,
    SUM(CASE WHEN pass_fail = false THEN 1 ELSE 0 END)::float / COUNT(*) as failure_rate
FROM internship.trace_annotations
WHERE failure_category IS NOT NULL
GROUP BY failure_category
ORDER BY failures DESC;


-- Update schema version
INSERT INTO internship.schema_version(version) VALUES (2) ON CONFLICT DO NOTHING;
