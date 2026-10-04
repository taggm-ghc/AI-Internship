# Week 4: I thought my bot was good. Measurement said 30%.

This week's lesson was the vibe-check trap: "it works on the examples I tried" is not evaluation. I fell into it, and a script got me out.

I applied the TRACE loop (Trace, Read, Analyze, Codify, Enforce) to 20 recorded conversations from Harmony, a property-leasing SMS bot. I read every trace by hand and wrote down what went wrong before writing any code. Three failure patterns stood out (the full taxonomy is below). Each became a deterministic Python check: same input, same verdict, a stated reason, no ML judge.

1. **Ungrounded claims.** The bot states specifics found in neither its retrieved context nor its tool results. In one trace it said about 900 sq ft where the tool result said 680.
2. **Policy limits.** It approved "3 cats" when the policy maximum is 2.
3. **Unauthorized actions.** It said "Done - Maple is held and I emailed your contract", though it made no tool calls and cannot do either.

**Measured baseline.** The checks pass 8/20, 17/20 and 18/20. A trace must pass all three, so only **6/20 (30%)** pass. My ship matrix says anything under 85% is BLOCK. This bot should not ship.

**The honest part.** My own earlier notes claimed a higher baseline and an 85% result after a fix. Neither was measured. The baseline was impossible on its face, because overall pass cannot exceed the weakest single check (8/20). The 85% came from a hardcoded list of traces that "would pass", not from any fix. I caught it by re-running the checks and doing the arithmetic, then retracted both numbers in my records. Re-running was cheap. Trusting a tidy number in my own docs was the expensive mistake.

**The fix.** The top failure is fabrication, so I built a post-generation grounding gate. It checks each specific in a reply against retrieved context and tool results before the reply is sent.

**After-fix result:**
Measured with the same three checks: 6/20 (30%) became 20/20 (100%). That is not a ship recommendation. The gate changed 14 of 20 replies, and in 9 of them no original sentence survived: the reply is only evidence, hedge, hand-off or refusal text. The gate and the checks share rules and nothing was held out, so 100% is partly overfitting, and one semantic error in the set (ha-002) slips past both.

Hand-reading the 20 traces gave a 6-category failure taxonomy: fabricated fact (5), unauthorized action claim (3), policy limit violation (3), requirement/KB contradiction (2), security disclosure (1), missed intent (1).

Limits: the checks are heuristics, not proofs, and 20 traces is a small sample.

**Then on my capstone.** The Harmony traces were course-provided, so I ran the same loop on VERA, my research assistant for published evidence on AI coding assistants. I captured 60 live traces: 30 frozen questions, each answered by today's bare /ask and by a version grounded in VERA's own arXiv search. Reading them first, the bare version cited almost nothing checkable (16 of 20 dev answers) and accepted false premises. Nine deterministic checks, validated against blind labels (they catch nearly every labelled failure but over-flag some passes), show grounding removed fabricated citations, unsourced numbers and vague "studies show" claims (12 to 0). The all-pass rate barely moved, though: 10% to 15% on dev, 20% to 20% held-out. The new top failure is retrieval: in 13 of 19 answers at most one of five search results was on topic, and the model rightly declined. Tracing also caught a bug: VERA's search rebuilt its rate limiter on every question. Next fix: retrieval.

Takeaway: a deterministic check is only as honest as the number you report from it. Run it, then do the arithmetic.

Screenshots: [SCREENSHOT PLACEHOLDER: capture from the live deployment]
Code: [REPO LINK PLACEHOLDER] https://github.com/taggm-ghc/AI-Internship
