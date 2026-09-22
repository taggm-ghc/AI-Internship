---
name: research-informed-planning
description: Use before writing any implementation code for a new non-trivial feature or initiative in this project. Ensures work is (1) recorded in p3m3's PMI-style planning framework -- a permanent work-package ID, deliverables, dependencies, requisites, matching todo-digest.md/week2-priority-checklist.md's existing format -- BEFORE any code exists, not documented after the fact; (2) grounded against real external literature, research, lessons-learned, and de-facto standards via web search, with the plan explicitly revised against what's found (cite what changed and why, not just that research happened); (3) where the research surfaces authoritative source material, ingests it into this project's own RAG corpus (POST /ingest with real provenance) so the decisions have a durable, citable evidence trail inside the app itself, not only in a planning document. Triggers on: a new feature request, "let's build X", an architectural decision, anything that would otherwise warrant Claude Code's own EnterPlanMode -- this Skill supersedes plan-mode's scratch file as the durable record for this project.
---

# research-informed-planning

Extracted 2026-09-22 from the observability-dashboard exercise (p3m3 permanent item #17), where this exact sequence was arrived at only after two corrections mid-task: implementation code was written before any p3m3 entry existed for the work, and a Claude Code plan-mode scratch file (`~/.claude/plans/...`) was initially treated as sufficient planning documentation when it isn't -- p3m3 is this project's durable planning record, not Claude Code's own ephemeral plan file.

## The process

1. **p3m3 entry first, before any code.** A new permanent ID (or an extension of an existing item) in `p3m3/todo-digest.md`, with deliverables/dependencies/requisites in the format already established there. If a narrative walkthrough is warranted (a multi-step build, a corrected mistake worth keeping on record), add a matching subsection to `p3m3/week2-priority-checklist.md` in the style of its existing "D7"/"D-N" sections -- lead with the rule or decision, then why, then how it applies, same structure this project's memory/feedback entries use.

2. **Research before finalizing the design.** Search for real literature, production lessons-learned, and de-facto standards relevant to the domain -- not just local codebase patterns. For each finding that changes the plan, say explicitly what changed and why, citing the source. A finding that doesn't change anything is worth noting too, briefly, so it's clear research happened rather than assumed.

3. **Where research surfaces authoritative source material, ingest it.** `POST /ingest` the real source (full text via `WebFetch`, verified author/date -- don't trust search-result summaries alone) with a real `provenance` payload (source, source_url, author, published_at, fetched_at, `provenance_type`). This makes the design decision citable from inside the running app's own RAG corpus, not only from a planning document that lives outside it.

4. **Then implement**, against the reviewed p3m3 entry -- not against the plan-mode scratch file, which is disposable.

5. **Verify end-to-end, by actually running the code**, not by reading it. This project has been burned by exactly this gap more than once: a syntax check (`ast.parse`, `python -c "import ..."`) catches typos, not runtime bugs -- e.g. a Streamlit `st.page_link()` call to the entrypoint script that only failed with `StreamlitPageNotFoundError` when actually executed via `streamlit.testing.v1.AppTest`, or a DataFrame column with dict values that only broke PyArrow serialization when a chart actually rendered. Prefer the tool that runs the real code path (`AppTest` for Streamlit pages, a live local server + real HTTP calls for API endpoints) over one that only parses it.

6. **Maximize component reuse deliberately, not as an afterthought.** When a second file needs logic a first file already has, extract the shared logic into its own importable module rather than duplicating it -- duplication is a standing source of drift and bugs (two copies of a cold-start-retry helper silently diverging is a real failure mode this project hit). This is itself a p3m3-worthy principle, not just a one-off code-review comment: state it in the plan when a new piece of work will share logic with existing code, and name what's being reused and what's new.

## What this replaces

Claude Code's `EnterPlanMode` still applies for the actual step-by-step design/pseudocode work, but its output (a file under `~/.claude/plans/`) is a scratch draft, not the deliverable. The p3m3 entry from step 1 is what persists and what future sessions read -- write the plan there, not only in the ephemeral plan file, before treating planning as done.
