"""
Post-generation grounding and policy gate (Week 4 Path A fix, measured 2026-10-04).

A deterministic, stdlib-only filter that runs AFTER the model has produced a response and
BEFORE it is sent. It compares the response against the evidence the bot actually had
(retrieved context + tool-call results) and the policy text in that context, and rewrites
the response so that it only asserts what the evidence supports:

  * credential / secret disclosure        -> whole response replaced by a refusal
  * completed-action claims (held, emailed, confirmed ...) that no successful tool call
    executed, or that policy forbids      -> sentence removed, hand-off to the office added
  * numbers / prices / sizes / times not present in the evidence (or the user's own words)
                                          -> parenthetical or sentence removed
  * URLs not present in the evidence      -> URL (or markdown link) removed
  * amenity / service terms not present in the evidence (non-denial sentences)
                                          -> sentence removed
  * day-of-week hours claims for days the evidence gives no hours for -> sentence removed
  * statements that assert X where a tool result says "not X"         -> sentence removed
  * pet-policy requests over a parsed limit, invented softening exceptions, or a dog
    approval without the weight rule      -> pet sentences replaced by the parsed policy
  * a price above the user's own stated budget, recommended without a contrast cue
                                          -> sentence removed

When something is removed, the gate appends (a) the raw tool result(s) as "Our records show:"
(grounded by construction), and/or (b) a hedge pointing to the leasing office, so a
removal never silently leaves an empty or misleading reply.

It does not key on trace ids and contains no expected outcomes. The only domain constants
are the action-verb map, the amenity lexicon and the pet-policy phrasing below, each
commented.

Known limitations (also written into eval_results/*.json "fix.limitations")
--------------------------------------------------------------------------
* Shared assumptions with the evaluator: the gate and scripts/check_functions.py were
  written from the same EARS rules (ground values, enforce limits, no unauthorised action
  claims), and the gate was designed while looking at all 20 Harmony traces. A pass on
  those checks after gating is therefore partly by construction. It shows that the gate
  removes what the checks define as failures; it does not show general answer quality.
  No trace was held out (all 20 were seen during design).
* Removal is cheap; usefulness is not. Deleting everything would also pass the checks, so
  the gate's report counts how many responses fell back to a generic hedge.
* Lexicon-based: an invented amenity outside AMENITY_TERMS, an unlisted action verb, or a
  semantic mismatch (ha-002: "both 1brs work" when one is en-suite) is NOT caught.
* Number grounding is value-level: a real number attached to the wrong unit/name can pass.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Constants (domain lexicons; named once, change = new gate version)
# ---------------------------------------------------------------------------

GATE_NAME = "post_generation_grounding_gate"
GATE_VERSION = "1.0.0"

# Hand-off and hedge wording. Deliberately contains no numbers, URLs or day names, so it is
# grounded by construction, and names the office as the authority (EARS: hand off).
HANDOFF_TEXT = (
    "I can't do that myself - holds, contracts and bookings are handled by the leasing "
    "office, so please contact the office directly."
)
HEDGE_TEXT = "I can't confirm that from our current listings - please contact the leasing office for details."
REFUSAL_TEXT = (
    "Sorry, I can't share passwords, credentials or internal system details. "
    "For account help, please contact the leasing office."
)
EVIDENCE_PREFIX = "Our records show: "

# Completed-action claims: past-tense verb -> tool-name keywords that would back it.
# Present tense / negated / conditional uses are not claims.
ACTION_VERBS = {
    r"held|reserved|on hold": r"hold|reserve",
    r"e-?mailed|texted|sent": r"email|mail|send|text|sms",
    r"confirmed|booked|scheduled|approved|registered": r"confirm|book|schedule|approve|register",
    r"submitted|processed|charged|cancell?ed": r"submit|process|charge|cancel|pay",
}
# Policy phrasing that forbids the bot an action ("Bot cannot hold units").
_FORBID_RE = re.compile(
    r"(?:cannot|can'?t|can not|unable to|not able to|may not|must not|does not|do not|don'?t)\s+"
    r"(?:\w+\s+){0,2}?(hold|reserve|email|e-mail|send|mail|confirm|book|schedule|approve|submit|process|charge|cancel)",
    re.I,
)

# Amenity / service terms a generator tends to invent. A term is unsupported when it is in
# the response (in a non-denial sentence) but nowhere in the evidence.
AMENITY_TERMS = [
    "rooftop", r"cabanas?", "parking", r"garages?", r"pools?", "gym", "fitness", "clubhouse",
    "concierge", r"virtual\s+(?:walk-?through|tour)s?", r"lofts?", r"balcon(?:y|ies)", r"patios?",
    r"washers?", r"dryers?", "laundry", r"dishwashers?", "doorman", r"elevators?", r"hot\s+tub",
    "sauna", r"closets?", "furnished", "in-unit", "hardwood", "granite", "stainless", "luxury",
    "penthouse", r"dog\s+park", r"bike\s+storage", r"package\s+lockers?", "shuttle", r"storage\s+units?",
    r"ev\s+charg\w*", r"security\s+guards?", r"valet",
]
_AMENITY_RES = [re.compile(rf"\b{t}\b", re.I) for t in AMENITY_TERMS]

_NEG_RE = re.compile(
    r"\bno\b|\bnot\b|n't|\bcannot\b|\bnever\b|\bunable\b|\bunavailable\b|\bunknown\b|\bwithout\b", re.I
)
_COND_RE = re.compile(r"\b(?:if|once|when|unless|after|until)\b", re.I)
_URL_RE = re.compile(
    r"https?://[^\s)\]>\"']+|"
    r"(?<![/@\w.])(?:[a-z0-9-]+\.)+(?:com|net|org|io|ly|co|us|app|info|example)\b(?:/[^\s)\]>\"']*)?",
    re.I,
)
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\(([^)\s]+)\)")
_SECRET_RE = re.compile(
    r"\b(?:password|passcode|pin|api[- ]?key|token|secret|credentials?)\b[^.\n]{0,25}?\b(?:is|are|=|:)\s*"
    r"[`'\"]?[^\s`'\".,]{4,}",
    re.I,
)
_QTY_RE = re.compile(
    r"(?P<dollar>\$\s*)?(?<![\w.])(?P<num>\d[\d,]*(?:\.\d+)?)\s*"
    r"(?P<unit>bedrooms?|beds?|br|bd|baths?|sq\.?\s*ft|square\s+(?:feet|foot|ft)|lbs?|pounds?|"
    r"hours?|hrs?|days?|weeks?|months?|mo|%)?(?![a-z])",
    re.I,
)
_UNIT = {
    "bedroom": "br", "bed": "br", "br": "br", "bd": "br", "bath": "bath", "lb": "lb", "pound": "lb",
    "hour": "hour", "hr": "hour", "day": "day", "week": "week", "month": "month", "mo": "month", "%": "pct",
}
_DAY = r"(?:monday|mon|tuesday|tues|tue|wednesday|wed|thursday|thurs|thur|thu|friday|fri|saturday|sat|sunday|sun)"
_DAY_IDX = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
_DAY_SPAN_RE = re.compile(rf"\b({_DAY})\b\s*(?:-|–|—|to|through|thru)\s*\b({_DAY})\b", re.I)
_DAY_ONE_RE = re.compile(rf"\b({_DAY})s?\b", re.I)
_HOURS_CUE_RE = re.compile(r"\b(?:open|opens|hours?|walk-?ins?|office)\b|\d\s*(?:am|pm)\b", re.I)

_NUMWORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
             "seven": 7, "eight": 8, "nine": 9, "ten": 10}
_BREEDS = (r"rottweilers?|pit\s*bulls?|pitbulls?|german\s+shepherds?|shepherds?|dobermans?|labradors?|labs?|"
           r"retrievers?|husk(?:y|ies)|mastiffs?|great\s+danes?|bulldogs?|chihuahuas?|poodles?|beagles?|"
           r"terriers?|corgis?|boxers?|akitas?|malamutes?")
_DOG_RE = re.compile(rf"\b(?:dogs?|pupp(?:y|ies)|{_BREEDS})\b", re.I)
_PET_RE = re.compile(rf"\b(?:cats?|kittens?|pets?|dogs?|pupp(?:y|ies)|{_BREEDS})\b", re.I)
_PET_COUNT_RE = re.compile(
    rf"\b(\d+|{'|'.join(_NUMWORDS)})\s+(?:[a-z-]+\s+)?(?:cats?|kittens?|pets?|dogs?|pupp(?:y|ies)|{_BREEDS})\b", re.I
)
_WEIGHT_RE = re.compile(r"(\d+)\s*-?\s*(?:lbs?\b|pounds?\b)", re.I)
# Phrases that soften a hard limit into a discretionary one. Only invented if absent from policy.
_SOFTENER_RE = re.compile(
    r"as long as|\busually\b|\btypically\b|\bgenerally\b|case[- ]by[- ]case|\bexceptions?\b|"
    r"\bif\s+(?:they|it|he|she|your \w+)(?:'re|'s| is| are)\b|\bdepends\b|pay (?:a |an |the )?(?:extra )?(?:deposits?|fees?)",
    re.I,
)
_APPROVAL_RE = re.compile(
    r"\b(?:yes|sure|allowed|permitted|welcome|fine|ok|okay|no problem|all set|go ahead|you can|can bring|can have)\b",
    re.I,
)
_PRICE_CONTRAST_RE = re.compile(
    r"\bskip\b|\bover\b|\babove\b|exceeds?|out of|too (?:much|expensive|high)|beyond|\bnot\b|n't|"
    r"outside|higher|more than|pricier|\bbut\b|however",
    re.I,
)


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass
class GateResult:
    text: str
    changed: bool
    actions: list[str] = field(default_factory=list)  # human-readable edit log
    fallback_only: bool = False  # True when no original sentence survived


def _as_text(x) -> str:
    if x is None:
        return ""
    if isinstance(x, str):
        return x
    if isinstance(x, dict):
        for k in ("answer", "text", "content", "snippet"):
            if k in x:
                return _as_text(x[k])
        return json.dumps(x, ensure_ascii=False)
    if isinstance(x, (list, tuple)):
        return "\n".join(_as_text(i) for i in x)
    return str(x)


def _tool_list(tool_calls) -> list[dict]:
    if isinstance(tool_calls, str):
        try:
            tool_calls = json.loads(tool_calls)
        except json.JSONDecodeError:
            return []
    if isinstance(tool_calls, dict):
        tool_calls = [tool_calls]
    return [t for t in (tool_calls or []) if isinstance(t, dict)]


# ---------------------------------------------------------------------------
# Detectors (each returns a list of problem strings for one sentence)
# ---------------------------------------------------------------------------


def _quantities(text: str) -> list[tuple[str, str | None, str]]:
    text = _URL_RE.sub(" ", text)
    text = re.sub(r"(?m)^\s*\d+[.)]\s+", "", text)
    out = []
    for m in _QTY_RE.finditer(text):
        if re.match(r"(?:st|nd|rd|th)\b", text[m.end():], re.I):
            continue
        raw = (m.group("unit") or "").lower()
        if m.group("dollar"):
            unit = "usd"
        elif raw.startswith(("sq", "square")):
            unit = "sqft"
        elif raw:
            unit = _UNIT.get(raw.rstrip("s")) or _UNIT.get(raw)
        else:
            unit = None
        num = m.group("num").replace(",", "")
        if "." in num:
            num = num.rstrip("0").rstrip(".")
        out.append((num, unit, m.group(0).strip()))
    return out


def _days(text: str) -> set[int]:
    days: set[int] = set()
    for a, b in _DAY_SPAN_RE.findall(text):
        i, j = _DAY_IDX[a.lower()[:3]], _DAY_IDX[b.lower()[:3]]
        days.update(range(i, j + 1) if i <= j else [*range(i, 7), *range(0, j + 1)])
    for m in _DAY_ONE_RE.finditer(_DAY_SPAN_RE.sub(" ", text)):
        days.add(_DAY_IDX[m.group(1).lower()[:3]])
    return days


def _norm_url(u: str) -> str:
    return u.rstrip(".,;:!?)").lower().rstrip("/")


class _Evidence:
    """Everything the bot was entitled to assert, pre-parsed once per response."""

    def __init__(self, context: str, tool_calls: list[dict], user: str):
        self.context = context
        self.tools = tool_calls
        self.tool_results = [_as_text(t.get("result")).strip() for t in tool_calls]
        self.text = context + "\n" + "\n".join(self.tool_results)
        self.user = user
        q = _quantities(self.text + "\n" + user)
        self.q_pairs = {(v, u) for v, u, _ in q if u}
        self.q_values = {v for v, _, _ in q}
        self.urls = {_norm_url(u) for u in _URL_RE.findall(self.text)}
        self.days = _days(self.text)
        self.forbidden = {m.group(1).lower() for m in _FORBID_RE.finditer(context)}
        # "X (not Y)" / "not Y" in tool results: the response must not assert Y.
        self.negated_facts = sorted(
            {m.group(1).lower() for r in self.tool_results for m in re.finditer(r"\bnot\s+([a-z][\w-]{3,})", r, re.I)}
        )

    def tool_backs(self, tool_kw: str) -> bool:
        for t in self.tools:
            ident = f"{t.get('name', '')} {json.dumps(t.get('args', {}), default=str)}"
            if re.search(tool_kw, ident, re.I) and not re.search(
                r"error|fail|denied|unauthori", _as_text(t.get("result")), re.I
            ):
                return True
        return False


def _ungrounded_values(s: str, ev: _Evidence) -> list[str]:
    probs = []
    for val, unit, snip in _quantities(s):
        ok = (val, unit) in ev.q_pairs if unit else val in ev.q_values
        if not ok:
            probs.append(f"ungrounded value '{snip}'")
    return probs


def _unsupported_claims(s: str, ev: _Evidence) -> list[str]:
    """Value-level grounding problems in one sentence (excluding URLs, handled by rewrite)."""
    probs = _ungrounded_values(s, ev)
    negated = bool(_NEG_RE.search(s))
    if _HOURS_CUE_RE.search(s) and not negated:
        for d in sorted(_days(s) - ev.days):
            probs.append(f"hours claimed for day {d} not in evidence")
    if not negated:
        for rx in _AMENITY_RES:
            m = rx.search(s)
            if m and not rx.search(ev.text):
                probs.append(f"unsupported offering '{m.group(0)}'")
        for fact in ev.negated_facts:
            if re.search(rf"\b{re.escape(fact)}\b", s, re.I):
                probs.append(f"asserts '{fact}' but a tool result says 'not {fact}'")
    return probs


def _action_claims(s: str, ev: _Evidence) -> list[str]:
    probs = []
    for verbs, tool_kw in ACTION_VERBS.items():
        rx = re.compile(
            rf"\b(?:I|we)(?:'ve|\s+have)?\s+(?:just\s+|already\s+|gone ahead and\s+)?(?:{verbs})\b|"
            rf"\b(?:is|are|was|were|been|got|now|it's|it is)\s+(?:now\s+|already\s+|successfully\s+)?(?:{verbs})\b|"
            rf"\b(?:you'?re|you are|you've been|you have been)\s+(?:now\s+|all\s+)?(?:{verbs})\b",
            re.I,
        )
        m = rx.search(s)
        if not m:
            continue
        if _NEG_RE.search(s[max(0, m.start() - 40): m.end()]) or _COND_RE.search(s[: m.start()]):
            continue
        forbidden = any(re.search(tool_kw, f, re.I) for f in ev.forbidden)
        if forbidden:
            probs.append(f"claims '{m.group(0).strip()}' but policy forbids it")
        elif not ev.tool_backs(tool_kw):
            probs.append(f"claims '{m.group(0).strip()}' with no executed tool call")
    return probs


# ---------------------------------------------------------------------------
# Pet policy (numeric limits parsed from evidence, never hardcoded)
# ---------------------------------------------------------------------------


def _pet_limits(text: str) -> dict:
    lim = {}
    m = re.search(r"\bmax(?:imum)?\s*(?:of\s*)?(\d+)|(\d+)\s+pets?\s+(?:max|maximum|limit)", text, re.I)
    if m:
        lim["max_pets"] = int(m.group(1) or m.group(2))
    m = re.search(r"\bdogs?\s*(?:<|under|below|less than)\s*(\d+)\s*(?:lbs?|pounds?)?", text, re.I)
    if m:
        lim["dog_lt"] = int(m.group(1))
    return lim


def _pet_policy_problems(resp: str, ev: _Evidence, lim: dict) -> list[str]:
    if not lim or not _PET_RE.search(ev.user + " " + resp):
        return []
    probs = []
    req = 0
    for m in _PET_COUNT_RE.finditer(ev.user):
        n = m.group(1).lower()
        req += int(n) if n.isdigit() else _NUMWORDS[n]
    states_limit = lambda txt: bool(re.search(r"\bmax|\blimit|\bat most|\bno more than|\bunder \d+|less than \d+", txt, re.I))  # noqa: E731
    approves = bool(_APPROVAL_RE.search(resp))
    if "max_pets" in lim and req > lim["max_pets"] and not states_limit(resp):
        probs.append(f"{req} pets requested over max {lim['max_pets']} without stating the limit")
    if "dog_lt" in lim:
        weights = [int(w) for w in _WEIGHT_RE.findall(ev.user)]
        if any(w >= lim["dog_lt"] for w in weights) and not states_limit(resp):
            probs.append(f"dog weight over {lim['dog_lt']} lbs requested without stating the limit")
        if any(int(w) >= lim["dog_lt"] for w in _WEIGHT_RE.findall(resp)):
            probs.append(f"response states a dog weight at/over the {lim['dog_lt']} lb limit")
        if _DOG_RE.search(ev.user) and approves and not re.search(r"weigh|\blbs?\b|pounds?", resp, re.I):
            probs.append(f"dog approved without the weight rule (under {lim['dog_lt']} lbs)")
    m = _SOFTENER_RE.search(resp)
    if m and not _SOFTENER_RE.search(ev.text):
        probs.append(f"softening exception '{m.group(0)}' not in policy")
    return probs


def _pet_policy_sentence(lim: dict) -> str:
    parts = []
    if "max_pets" in lim:
        parts.append(f"a maximum of {lim['max_pets']} pets per home")
    if "dog_lt" in lim:
        parts.append(f"dogs must be under {lim['dog_lt']} lbs")
    return (
        "Our pet policy: " + "; ".join(parts) + ". I can't make changes to that policy - "
        "please contact the leasing office with any questions."
    )


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

_SENT_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def _strip_bad_urls(s: str, ev: _Evidence, actions: list[str]) -> str:
    def md(m: re.Match) -> str:
        if _norm_url(m.group(2)) in ev.urls:
            return m.group(0)
        actions.append(f"removed link to ungrounded URL '{m.group(2)}'")
        return ""

    s = _MD_LINK_RE.sub(md, s)
    for u in dict.fromkeys(_URL_RE.findall(_MD_LINK_RE.sub(lambda m: m.group(1), s))):
        if _norm_url(u) not in ev.urls:
            actions.append(f"removed ungrounded URL '{u}'")
            s = re.sub(rf"(?:\s*(?:,|\band\b|\bor\b|\balso\b))*\s*{re.escape(u)}", "", s)
    return s.strip()


def apply_gate(response: str, *, retrieved_context="", tool_calls=None, user_input: str = "") -> GateResult:
    """Return the gated response. Pure function; no I/O, no model calls."""
    resp = _as_text(response)
    if not resp.strip():
        return GateResult(resp, False)
    tools = _tool_list(tool_calls)
    ev = _Evidence(_as_text(retrieved_context), tools, _as_text(user_input))
    actions: list[str] = []

    # 0. credential disclosure: nothing in the reply can be trusted; refuse wholesale.
    if _SECRET_RE.search(resp):
        return GateResult(REFUSAL_TEXT, True, ["credential disclosure: response replaced by refusal"], True)

    lim = _pet_limits(ev.text)
    pet_probs = _pet_policy_problems(resp, ev, lim)
    if pet_probs:
        actions += [f"pet policy: {p}" for p in pet_probs]

    need_handoff = need_evidence = False
    kept_lines: list[str] = []
    n_orig = n_kept = 0
    budget_m = re.search(r"(?:under|below|less than|max(?:imum)?|up to|no more than|<)\s*\$\s*(\d[\d,]*)", ev.user, re.I)
    budget = int(budget_m.group(1).replace(",", "")) if budget_m else None

    for line in resp.split("\n"):
        kept_sents = []
        for s in _SENT_SPLIT_RE.split(line):
            if not s.strip():
                continue
            n_orig += 1
            s2 = _strip_bad_urls(s, ev, actions)
            claim_probs = _action_claims(s2, ev)
            if claim_probs:
                actions += [f"removed sentence: {p}" for p in claim_probs]
                need_handoff = True
                continue
            if pet_probs and (_PET_RE.search(s2) or _APPROVAL_RE.search(s2) or _SOFTENER_RE.search(s2)):
                actions.append(f"removed pet-policy sentence '{s2}'")
                continue
            probs = _unsupported_claims(s2, ev)
            if probs:  # try dropping just the parenthetical(s) first
                trimmed = re.sub(r"\s*\([^)]*\)", "", s2)
                if trimmed != s2 and not _unsupported_claims(trimmed, ev):
                    actions += [f"removed parenthetical: {p}" for p in probs]
                    s2 = trimmed
                    probs = []
            if probs:
                actions += [f"removed sentence: {p}" for p in probs]
                need_evidence = True
                continue
            if budget:
                over = [int(p.replace(",", "")) for p in re.findall(r"\$\s*(\d[\d,]*)", s2)]
                if any(p > budget for p in over) and not _PRICE_CONTRAST_RE.search(s2):
                    actions.append(f"removed sentence recommending a price over the user's ${budget} budget")
                    continue
            if re.sub(r"[\W_]+", "", s2):
                kept_sents.append(s2)
                n_kept += 1
        if kept_sents:
            kept_lines.append(" ".join(kept_sents))

    if not actions:
        return GateResult(resp, False)

    out = "\n".join(kept_lines).strip()
    tail: list[str] = []
    if pet_probs:
        tail.append(_pet_policy_sentence(lim))
    if need_evidence and not pet_probs:
        fresh = [r for r in ev.tool_results if r and r not in out]
        if fresh:
            # the tool evidence is the corrected answer; no hedge needed alongside it
            tail.append(EVIDENCE_PREFIX + "; ".join(fresh).rstrip(".") + ".")
        else:
            tail.append(HEDGE_TEXT)
    if need_handoff:
        tail.append(HANDOFF_TEXT)
    text = (out + ("\n" if "\n" in out else " ") + " ".join(tail)).strip() if out else " ".join(tail)
    if not text.strip():
        text = HEDGE_TEXT
    return GateResult(text, text != resp, actions, n_kept == 0)


def gate_trace(trace: dict) -> tuple[dict, GateResult]:
    """Apply the gate to a trace dict; returns (copy with gated assistant_output, GateResult)."""
    key = next((k for k in ("bot_response", "assistant_output", "response") if trace.get(k) is not None), "assistant_output")
    res = apply_gate(
        trace.get(key),
        retrieved_context=trace.get("retrieved_context"),
        tool_calls=trace.get("tool_calls"),
        user_input=_as_text(trace.get("user_input")),
    )
    out = dict(trace)
    out[key] = res.text
    return out, res


FIX_DESCRIPTION = {
    "name": GATE_NAME,
    "module": "grounding_gate.py",
    "description": (
        f"v{GATE_VERSION}. Deterministic post-generation gate: checks each response sentence against "
        "retrieved context + tool results + parsed policy; removes ungrounded values/URLs/amenities/hours, "
        "contradictions of tool results and unbacked or forbidden action claims; replaces pet-policy "
        "violations with the parsed policy; replaces credential disclosure with a refusal; appends tool "
        "evidence, a hedge or an office hand-off. No LLM call, no trace ids, no hardcoded outcomes."
    ),
    "limitations": (
        "Gate and checks share the same EARS rules and similar lexicons, and the gate was designed with all "
        "20 traces visible (no holdout), so the after-fix pass rate is partly by construction: it measures "
        "that the gate removes what the checks call failures, not overall answer quality. Removal can trade "
        "helpfulness for safety (see fallback count). Lexicon-based: unlisted amenities/verbs and semantic "
        "mismatches (e.g. ha-002) are not caught; the checks also miss ha-002. Replayed on recorded traces, "
        "not on new live generations."
    ),
}
