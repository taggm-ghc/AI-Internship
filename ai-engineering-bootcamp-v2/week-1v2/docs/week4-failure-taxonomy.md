# Week 4 failure taxonomy (Analyze)

## Method
1. Read all 20 Harmony Apartments traces and the open-coding notes in `internship.trace_annotations.open_code_notes`.
2. Open coding: one note per trace on what went wrong. 15 traces failed, 5 passed (ha-009, 014, 015, 018, 020).
3. Axial coding: clustered notes into 6 categories, one primary category per failing trace.
4. Counted and ranked by frequency x severity (critical=4, high=3, medium=2, low=1).

## Categories
| Rank | Category | Severity | Count | Score | Example traces | Covering check |
|---|---|---|---|---|---|---|
| 1 | `fabricated_fact` | high | 5/20 | 15 | ha-006, ha-007, ha-010, ha-012, ha-017 | check_no_ungrounded_claims |
| 2 | `unauthorized_action_claim` | critical | 3/20 | 12 | ha-005, ha-016, ha-001 | check_no_unauthorized_claims |
| 3 | `policy_limit_violation` | high | 3/20 | 9 | ha-004, ha-013, ha-019 | check_policy_limits_enforced |
| 4 | `requirement_or_kb_contradiction` | high | 2/20 | 6 | ha-002, ha-011 | none (ha-011 only secondarily via ungrounded check; ha-002 uncovered) |
| 5 | `security_disclosure` | critical | 1/20 | 4 | ha-008 | check_no_ungrounded_claims (credential disclosure rule) |
| 6 | `unhelpful_missed_intent` | medium | 1/20 | 2 | ha-003 | none |

Machine-readable: `config/failure_taxonomy.json`; also in `internship.failure_categories` (6 rows).

## Prioritization
Fabricated facts lead on frequency (5 of 15 failures). Unauthorized action claims rank second because of critical severity. Policy-limit violations are third. Security disclosure has a single instance but is critical, so it should be fixed regardless of rank.

## Checks
`scripts/check_functions.py` provides `check_no_ungrounded_claims`, `check_policy_limits_enforced` and `check_no_unauthorized_claims`. The security rule sits inside the ungrounded-claims check.

## Coverage gaps
- `requirement_or_kb_contradiction`: ha-002 (ignores the user's bathroom constraint) is not caught by any check; ha-011 only incidentally.
- `unhelpful_missed_intent` (ha-003): no check; needs an LLM judge.
- Secondary issues not categorized: ha-007 replied in English to a Spanish query; ha-006 truncated address; no language-match check exists.
- ha-001 is a borderline label (low confidence); the check does not flag it.
- Small sample (n=20); counts are indicative only.
- Annotation rows: the `failure_category` assignment is recorded in the database (`internship` schema); counts above match it (5 traces have category `none`).
