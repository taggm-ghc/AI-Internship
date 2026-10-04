-- Migration 003: Add run_label to eval_check_results for before/after comparisons
-- Allows multiple runs (baseline, after_fix) for the same trace/check pair
-- Changes PK from (trace_id, check_name) to (trace_id, check_name, run_label)
--
-- Idempotent (rewritten 2026-10-02 so install_schema() can safely re-apply every migration in order):
-- each step checks current state first. Result is identical to the original on a database that has not
-- yet had 003 applied, and a no-op on one that has. The view is dropped only if it lacks run_label, so
-- grants on an already-correct view are preserved.

-- Add run_label column with default value 'baseline'
ALTER TABLE internship.eval_check_results
ADD COLUMN IF NOT EXISTS run_label TEXT NOT NULL DEFAULT 'baseline';

-- Replace the old primary key with one that includes run_label (only if not already so)
DO $$ BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint c
    JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = ANY (c.conkey)
    WHERE c.conrelid = 'internship.eval_check_results'::regclass
      AND c.contype = 'p' AND a.attname = 'run_label'
  ) THEN
    ALTER TABLE internship.eval_check_results DROP CONSTRAINT IF EXISTS eval_check_results_pkey;
    ALTER TABLE internship.eval_check_results ADD PRIMARY KEY (trace_id, check_name, run_label);
  END IF;
END $$;

-- Update the eval_check_summary view to include run_label (drop only the old, run_label-less definition)
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_views WHERE schemaname = 'internship' AND viewname = 'eval_check_summary'
             AND position('run_label' in definition) = 0) THEN
    DROP VIEW internship.eval_check_summary;
  END IF;
END $$;

CREATE OR REPLACE VIEW internship.eval_check_summary AS
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

INSERT INTO internship.schema_version(version) VALUES (3) ON CONFLICT DO NOTHING;
