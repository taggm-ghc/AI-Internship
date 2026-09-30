-- Migration 003: Add run_label to eval_check_results for before/after comparisons
-- Allows multiple runs (baseline, after_fix) for the same trace/check pair
-- Changes PK from (trace_id, check_name) to (trace_id, check_name, run_label)

-- Add run_label column with default value 'baseline'
ALTER TABLE internship.eval_check_results
ADD COLUMN run_label TEXT NOT NULL DEFAULT 'baseline';

-- Drop the old primary key
ALTER TABLE internship.eval_check_results
DROP CONSTRAINT eval_check_results_pkey;

-- Create new primary key including run_label
ALTER TABLE internship.eval_check_results
ADD PRIMARY KEY (trace_id, check_name, run_label);

-- Update the eval_check_summary view to include run_label
DROP VIEW IF EXISTS internship.eval_check_summary;

CREATE VIEW internship.eval_check_summary AS
SELECT
    run_label,
    check_name,
    check_type,
    COUNT(*) as total_traces,
    SUM(CASE WHEN pass = true THEN 1 ELSE 0 END) as passed,
    ROUND(100.0 * SUM(CASE WHEN pass = true THEN 1 ELSE 0 END) / COUNT(*), 2) as pass_rate
FROM internship.eval_check_results
GROUP BY run_label, check_name, check_type
ORDER BY run_label, check_name;

