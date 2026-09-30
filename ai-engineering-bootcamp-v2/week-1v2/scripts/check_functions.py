#!/usr/bin/env python3
"""
Code-based eval checks for the Harmony Apartments SMS bot (Week 4, Codify phase).

Each check is grounded in an EARS rule from the Phase 2.3 failure analysis:

  check_no_ungrounded_claims   Pattern #1 (severity 9)
      EARS: When the response contains factual claims, the system shall include
      only values from retrieved context, and must not invent unverified facts.

  check_policy_limits_enforced Pattern #2 (severity 8)
      EARS: When a policy limit exists for a numeric constraint, the system shall
      reject any value exceeding it, and must not invent exceptions.

  check_no_unauthorized_claims Pattern #3 (severity 9)
      EARS: When an action is outside bot authority, the system shall hand off,
      and must not claim the action was executed.

Every check has the signature  (trace_data: dict) -> (passed: bool, reason: str)
and is deterministic, dependency-free (stdlib only) and side-effect free.

Usage
-----
    python3 scripts/check_functions.py                 # dry run: print table + self-test
    python3 scripts/check_functions.py --selftest      # self-test only (exit 1 on mismatch)
    python3 scripts/check_functions.py --save          # also upsert into internship.eval_check_results

WARNING on --save: per the project's DB notes, the local EXTERNAL_DB_URL and Render's
INTERNAL_DB_URL are the SAME Postgres instance, so --save writes to the production
database. It also requires the 20 traces to already exist in internship.traces
(eval_check_results.trace_id is a foreign key); run load_harmony_traces.py first.

Input shape
-----------
The JSONL traces use `assistant_output`; the task spec calls it `bot_response`. Both are
accepted, as are dict-shaped values from the internship.traces JSONB columns
(response={"answer": ...}, retrieved_context={"text": ...}).

Grounding sources
-----------------
"Context" for a claim is retrieved_context PLUS tool_calls[*].result. The spec's example
says the Cedar record has no sq_ft, but in ha-017 a get_unit_details tool returned
"680 sq ft"; the tool result is evidence the bot had, so the bot's "about 900 sq ft" is
judged against 680, not against silence. (Either way it is a FAIL.)
Numbers the user themselves typed may be echoed back (e.g. "3 cats"). Named offerings,
URLs and day names may NOT be grounded by the user's own words, because users ask about
things that do not exist ("do you have parking?").

Known limitations (heuristic checks, not proofs)
------------------------------------------------
* Check 1 verifies numbers, URLs, days/hours, a fixed amenity/service lexicon, unit
  bathroom type and credential disclosure. It does not do open-ended semantic
  verification ("Cedar is popular" passes). That is the LLM-judge's job.
* Check 1 does NOT catch ha-002 (both 1brs "work" for a not-connected-bathroom request;
  Cedar is en-suite). That is a requirement-mismatch failure, not a fabricated value.
* Check 2's dog-weight rule knows a short list of breed names; an unlisted breed with
  no stated weight is not recognised as a dog question.
* Check 3 requires first-person/passive past-tense claims ("I've held", "is held",
  "you're confirmed"). "You're all set" (ha-004) is deliberately NOT treated as a claim.

Results vs. the error-analysis labels
-------------------------------------
Check 1 fails MORE traces than the seven labelled Pattern #1, because other traces
also contain ungrounded values that the labelling pass filed under a different primary
pattern: ha-004 ($400 deposit, $40/mo), ha-005 (48 hours), ha-019 (60 lbs), ha-008
(invented password), ha-011 (contradicts tool: Oak is bath-to-hallway, not en-suite).
Those are genuine ungrounded claims; expected outcomes are recorded in EXPECTED below.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Callable

TraceData = dict
CheckResult = tuple[bool, str]

# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------


def _as_text(x) -> str:
    """Flatten str / dict / list (incl. JSONB shapes) to plain text."""
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


def _normalize(trace_data: TraceData) -> dict:
    """Return a uniform view: user, response, context, tool_text, tool_calls, grounding."""
    resp = trace_data.get("bot_response")
    if resp is None:
        resp = trace_data.get("assistant_output")
    if resp is None:
        resp = trace_data.get("response")

    tool_calls = trace_data.get("tool_calls") or []
    if isinstance(tool_calls, str):
        try:
            tool_calls = json.loads(tool_calls)
        except json.JSONDecodeError:
            tool_calls = []
    if isinstance(tool_calls, dict):
        tool_calls = [tool_calls]
    tool_calls = [t for t in tool_calls if isinstance(t, dict)]

    context = _as_text(trace_data.get("retrieved_context"))
    tool_text = "\n".join(_as_text(t.get("result")) for t in tool_calls)
    return {
        "user": _as_text(trace_data.get("user_input")),
        "response": _as_text(resp),
        "context": context,
        "tool_text": tool_text,
        "tool_calls": tool_calls,
        "grounding": context + "\n" + tool_text,  # evidence the bot actually had
    }


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+|\n+", text)
    return [p.strip() for p in parts if p and p.strip()]


def _short(s: str, n: int = 70) -> str:
    s = " ".join(s.split())
    return s if len(s) <= n else s[: n - 1] + "…"


def _join_reason(prefix: str, problems: list[str], limit: int = 3) -> str:
    shown = "; ".join(problems[:limit])
    more = f" (+{len(problems) - limit} more)" if len(problems) > limit else ""
    return f"{prefix}: {shown}{more}"


_NEG_RE = re.compile(
    r"\bno\b|\bnot\b|n't|\bcannot\b|\bnever\b|\bunable\b|\bunavailable\b|\bunknown\b|"
    r"\bunsure\b|\bclosed\b|\bwithout\b",
    re.I,
)
_ACK_MISSING_RE = re.compile(
    r"don'?t know|do not know|don'?t have|do not have|not available|isn'?t (?:listed|available)|"
    r"not (?:sure|listed)|can'?t (?:confirm|say|share)|no information|not able to (?:confirm|say)|"
    r"(?:contact|call|ask|check with|speak (?:to|with)|reach out to)\b[^.]{0,30}\boffice|"
    r"office (?:can|will|would)",
    re.I,
)

# ---------------------------------------------------------------------------
# Quantity extraction (numbers with units) - shared by Check 1
# ---------------------------------------------------------------------------

_URL_RE = re.compile(
    r"https?://[^\s)\]>\"']+|"
    r"(?<![/@\w.])(?:[a-z0-9-]+\.)+(?:com|net|org|io|ly|co|us|app|info|example)\b(?:/[^\s)\]>\"']*)?",
    re.I,
)
_UNIT_CLASS = {
    "bedroom": "br", "bedrooms": "br", "bed": "br", "beds": "br", "br": "br", "bd": "br",
    "bath": "bath", "baths": "bath",
    "lb": "lb", "lbs": "lb", "pound": "lb", "pounds": "lb",
    "hour": "hour", "hours": "hour", "hr": "hour", "hrs": "hour",
    "day": "day", "days": "day",
    "month": "month", "months": "month", "mo": "month",
    "%": "pct",
}
_QTY_RE = re.compile(
    r"(?P<dollar>\$\s*)?(?<![\w.])(?P<num>\d[\d,]*(?:\.\d+)?)\s*"
    r"(?:(?P<unit>bedrooms?|beds?|br|bd|baths?|sq\.?\s*ft|square\s+(?:feet|foot|ft)|"
    r"lbs?|pounds?|hours?|hrs?|days?|months?|mo|%)(?![a-z]))?",
    re.I,
)


def _norm_num(s: str) -> str:
    s = s.replace(",", "").rstrip(",")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s


def _extract_quantities(text: str) -> list[tuple[str, str | None, str]]:
    """[(value, unit_class|None, original_snippet)]. URLs and list markers are ignored."""
    text = _URL_RE.sub(" ", text)
    text = re.sub(r"(?m)^\s*\d+[.)]\s+", "", text)  # "1. item" list markers
    out = []
    for m in _QTY_RE.finditer(text):
        if re.match(r"(?:st|nd|rd|th)\b", text[m.end():], re.I):  # 1st, 2nd
            continue
        raw_unit = (m.group("unit") or "").lower()
        if m.group("dollar"):
            unit = "usd"
        elif raw_unit.startswith("sq") or raw_unit.startswith("square"):
            unit = "sqft"
        elif raw_unit:
            unit = _UNIT_CLASS.get(raw_unit)
        else:
            unit = None
        out.append((_norm_num(m.group("num")), unit, m.group(0).strip()))
    return out


# ---------------------------------------------------------------------------
# Check 1 helpers: days/hours, offerings lexicon, bathroom attribute, secrets
# ---------------------------------------------------------------------------

_DAY_TOK = (
    r"(?:monday|mon|tuesday|tues|tue|wednesday|wed|thursday|thurs|thur|thu|"
    r"friday|fri|saturday|sat|sunday|sun)"
)
_DAY_IDX = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
_DAY_SPAN_RE = re.compile(rf"\b({_DAY_TOK})\b\s*(?:-|–|—|to|through|thru)\s*\b({_DAY_TOK})\b", re.I)
_DAY_ONE_RE = re.compile(rf"\b({_DAY_TOK})s?\b", re.I)
_HOURS_CUE_RE = re.compile(r"\b(?:open|opens|hours?|walk-?ins?|office)\b|\d\s*(?:am|pm)\b", re.I)


def _days_in(text: str) -> set[int]:
    """Days covered by "Mon-Fri", "Sat", "Sundays" ... as 0=Mon..6=Sun."""
    days: set[int] = set()
    for a, b in _DAY_SPAN_RE.findall(text):
        i, j = _DAY_IDX[a.lower()[:3]], _DAY_IDX[b.lower()[:3]]
        days.update(range(i, j + 1) if i <= j else list(range(i, 7)) + list(range(0, j + 1)))
    rest = _DAY_SPAN_RE.sub(" ", text)
    for (d,) in [(m.group(1),) for m in _DAY_ONE_RE.finditer(rest)]:
        days.add(_DAY_IDX[d.lower()[:3]])
    return days


# Amenity / service words a hallucinating bot tends to invent. A term is ungrounded if it
# appears in the response but not in context/tool results (and the sentence is not a denial).
_OFFERING_TERMS = [
    "rooftop", "cabanas?", "parking", "garages?", "pools?", "gym", "fitness", "clubhouse",
    "concierge", r"virtual\s+(?:walk-?through|tour)s?", "lofts?", "balcon(?:y|ies)", "patios?",
    "washers?", "dryers?", "laundry", "dishwashers?", "doorman", "elevators?", "hot\\s+tub",
    "sauna", "closets?", "furnished", "in-unit", "hardwood", "granite", "stainless", "luxury",
    "penthouse", "dog\\s+park", "bike\\s+storage", "package\\s+lockers?", "shuttle",
]
_OFFERING_RE = [(t, re.compile(rf"\b{t}\b", re.I)) for t in _OFFERING_TERMS]

_SECRET_RE = re.compile(
    r"\b(?:password|passcode|api[- ]?key|secret|credentials?)\b[^.\n]{0,25}?\b(?:is|are|=|:)\s*"
    r"[`'\"]?(?P<val>[^\s`'\".,]{4,})",
    re.I,
)

# Attributes a user can ask about; (user-question regex, "KB has it" regex)
_ASKABLE = {
    "square footage": (r"sq\.?\s*ft|square\s*(?:feet|foot|footage)", r"sq\.?\s*ft|square\s*(?:feet|foot)"),
    "deposits/fees": (r"\bdeposit|\bfees?\b|pet\s+rent|application\s+fee", r"deposit|\bfees?\b|pet\s+rent"),
    "parking": (r"parking|garage", r"parking|garage"),
    "utilities": (r"utilit", r"utilit"),
}


def _unit_names_and_bath(nd: dict) -> tuple[dict[str, str], list[str]]:
    """Known units -> bathroom type ('ensuite'|'hallway') from context + tool results."""
    bath: dict[str, str] = {}
    names: list[str] = []
    for clause in re.split(r"[;\n]", nd["context"]):
        m = re.match(r"\s*([A-Z][a-z]+)\s+(?:studio|\d\s*br)\b(.*)", clause)
        if m:
            names.append(m.group(1))
            rest = m.group(2).lower()
            if "en-suite" in rest or "ensuite" in rest:
                bath[m.group(1)] = "ensuite"
            elif "hallway" in rest:
                bath[m.group(1)] = "hallway"
    for t in nd["tool_calls"]:
        args = t.get("args") or {}
        name = next((str(args[k]) for k in ("plan", "unit", "name") if k in args), None)
        res = _as_text(t.get("result")).lower()
        if name:
            name = name.strip().title()
            names.append(name)
            if "hallway" in res or re.search(r"not\s+en-?suite", res):
                bath[name] = "hallway"
            elif "en-suite" in res or "ensuite" in res:
                bath[name] = "ensuite"
    return bath, sorted(set(names))


def check_no_ungrounded_claims(trace_data: TraceData) -> CheckResult:
    """
    Pattern #1 - Ungrounded Fabrication (EARS: only values from retrieved context).

    Extracts value claims from the response and verifies each against
    retrieved_context + tool results:
      1. numbers with units ($, sq ft, lbs, hours, bedrooms); bare numbers (incl. clock
         times) must appear somewhere in grounding or in the user's own message
      2. URLs must appear verbatim (normalised) in grounding
      3. days named in an hours/open sentence must be covered by grounded day spans
      4. amenity/service terms (rooftop, parking, concierge ...) must appear in grounding
         unless the sentence is a denial
      5. a unit's bathroom type must not contradict grounding (ha-011)
      6. credential disclosure ("password is X") is always ungrounded (ha-008)
      7. if the user asks for an attribute (sq ft, deposit, parking, utilities) that the
         KB lacks, the response must acknowledge it rather than answer confidently

    Returns (True, reason) when all claims are grounded or properly hedged.

    Embedded tests (run `--selftest` to execute against the real JSONL):
      ha-012 "We're open Sunday 11am-4pm"     -> (False, "... ungrounded '11am' ...; day 'Sunday' ...")
      ha-017 "about 900 sq ft" (tool: 680)    -> (False, "... ungrounded '900 sq ft' ...")
      ha-009 "480 sq ft"                      -> (False, "... ungrounded '480 sq ft' ...")
      ha-007 "$3,400 ... rooftop ... parking" -> (False, ...)
      ha-006 "https://bit.ly/fake-plans"      -> (False, "URL not in context ...")
      ha-015 "$1950 Maple / Pine $2100"       -> (True,  "All claims grounded ...")
      ha-014 pet policy link                  -> (True,  "All claims grounded ...")
      ha-020 "9am-6pm, 10am-2pm"              -> (True,  ...)  # 9am-6pm == context "9-6"
    """
    nd = _normalize(trace_data)
    resp, grounding, user = nd["response"], nd["grounding"], nd["user"]
    if not resp.strip():
        return True, "Empty response: nothing to ground"

    problems: list[str] = []

    # 1. quantities -----------------------------------------------------------
    ground_q = _extract_quantities(grounding + "\n" + user)
    g_pairs = {(v, u) for v, u, _ in ground_q if u}
    g_values = {v for v, _, _ in ground_q}
    seen: set[tuple[str, str | None]] = set()
    for val, unit, snippet in _extract_quantities(resp):
        if (val, unit) in seen:
            continue
        seen.add((val, unit))
        ok = (val, unit) in g_pairs if unit else val in g_values
        if not ok:
            problems.append(f"ungrounded '{snippet}'")

    # 2. URLs -----------------------------------------------------------------
    def _norm_url(u: str) -> str:
        return u.rstrip(".,;:!?)").lower().rstrip("/")

    g_urls = {_norm_url(u) for u in _URL_RE.findall(grounding)}
    for u in dict.fromkeys(_URL_RE.findall(resp)):
        if _norm_url(u) not in g_urls:
            problems.append(f"URL not in context '{_short(u, 40)}'")

    # 3. days named in hours sentences ---------------------------------------
    covered = _days_in(grounding)
    for s in _sentences(resp):
        if _HOURS_CUE_RE.search(s) and not _NEG_RE.search(s):
            for d in sorted(_days_in(s) - covered):
                name = [k for k, v in _DAY_IDX.items() if v == d][0]
                problems.append(f"day '{name.title()}' has no hours in context")

    # 4. amenity / service lexicon -------------------------------------------
    for s in _sentences(resp):
        if _NEG_RE.search(s):
            continue
        for term, rx in _OFFERING_RE:
            if rx.search(s) and not rx.search(grounding):
                problems.append(f"ungrounded offering '{rx.search(s).group(0)}'")

    # 5. bathroom attribute contradiction ------------------------------------
    bath, names = _unit_names_and_bath(nd)
    for s in _sentences(resp):
        named = [n for n in names if re.search(rf"\b{re.escape(n)}\b", s)]
        if len(named) != 1 or named[0] not in bath:
            continue
        if re.search(r"\b(?:not|never)\b[^.]{0,20}(?:en-?suite|connected|attached)|n't[^.]{0,20}(?:en-?suite|connected|attached)|hallway|separate", s, re.I):
            claim = "hallway"
        elif re.search(r"en-?suite|connected to|attached to", s, re.I):
            claim = "ensuite"
        else:
            continue
        if claim != bath[named[0]]:
            problems.append(f"{named[0]} bathroom claimed {claim}, context says {bath[named[0]]}")

    # 6. credentials -----------------------------------------------------------
    for m in _SECRET_RE.finditer(resp):
        problems.append(f"discloses credential value '{_short(m.group(0), 40)}'")

    # 7. asked-for attribute missing from KB must be acknowledged -------------
    ack = bool(_ACK_MISSING_RE.search(resp))
    for label, (ask_rx, kb_rx) in _ASKABLE.items():
        if re.search(ask_rx, user, re.I) and not re.search(kb_rx, grounding, re.I) and not ack:
            problems.append(f"user asked for {label}, KB silent, response does not say so")

    problems = list(dict.fromkeys(problems))
    if problems:
        return False, _join_reason("Ungrounded", problems)
    return True, "All claims grounded in context/tool results (or properly hedged)"


# ---------------------------------------------------------------------------
# Check 2 - policy limits
# ---------------------------------------------------------------------------

_NUMWORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
             "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
_BREEDS = (
    "rottweilers?|pit\\s*bulls?|pitbulls?|german\\s+shepherds?|shepherds?|dobermans?|labradors?|labs?|"
    "golden\\s+retrievers?|retrievers?|huskys?|huskies|mastiffs?|great\\s+danes?|bulldogs?|chihuahuas?|"
    "poodles?|beagles?|terriers?|corgis?|boxers?|akitas?|malamutes?"
)
_DOG_RE = re.compile(rf"\b(?:dogs?|pupp(?:y|ies)|{_BREEDS})\b", re.I)
_PET_RE = re.compile(rf"\b(?:cats?|kittens?|pets?|dogs?|pupp(?:y|ies)|{_BREEDS})\b", re.I)
_PET_COUNT_RE = re.compile(
    rf"\b(\d+|{'|'.join(_NUMWORDS)})\s+(?:[a-z-]+\s+)?((?:cats?|kittens?|pets?|dogs?|pupp(?:y|ies)|{_BREEDS}))\b",
    re.I,
)
_SOFTENER_RE = re.compile(
    r"as long as|\busually\b|\btypically\b|\bgenerally\b|case[- ]by[- ]case|"
    r"(?:they|it|he|she)(?:'re|'s| is| are)\s+(?:calm|well[- ]behaved|well[- ]trained|trained|friendly|quiet|gentle)|"
    r"\bif\s+(?:they|it|he|she|your \w+)(?:'re|'s| is| are)\s+\w+|"
    r"make (?:an )?exceptions?|exceptions? (?:are|can be|may be) made|"
    r"pay (?:a |an |the )?(?:extra |additional )?(?:deposits?|fees?|pet rent)",
    re.I,
)
_APPROVE_RE = re.compile(
    r"\b(?:yes|yep|sure|allowed|permitted|welcome|fine|ok|okay|no problem|all set|approved|go ahead|"
    r"you can|can bring|can have|can keep)\b",
    re.I,
)
_NEG_APPROVE_RE = re.compile(r"(?:\bnot\b|n't|\bnever\b)\s+(?:be\s+)?(?:allowed|permitted|ok|okay|fine|approved)", re.I)
_REJECT_RE = re.compile(
    r"^\s*no\b[,.!\s—-]|\bnot allowed\b|\bnot permitted\b|can'?t|cannot|\bunable\b|unfortunately|\bexceeds?\b|"
    r"over (?:the )?(?:limit|max)|too (?:heavy|many|large|big)|\bmax(?:imum)?\b|\blimit(?:ed)?\b|"
    r"\bat most\b|\bno more than\b|must be under|\bunder \d+|less than \d+|isn'?t allowed|aren'?t allowed|"
    r"only (?:allow|\d|up to|two|one)",
    re.I,
)
_NEG_CUE_PRICE_RE = re.compile(
    r"\bskip\b|\bover\b|\babove\b|exceeds?|out of|too (?:much|expensive|high|pricey)|beyond|\bnot\b|n't|"
    r"outside|higher|more than|pricier|slightly|\bbut\b|however",
    re.I,
)


def _approves(text: str) -> bool:
    return bool(_APPROVE_RE.search(_NEG_APPROVE_RE.sub(" ", text)))


def _strip_negated_exceptions(text: str) -> str:
    return re.sub(r"\b(?:no|not|never|don'?t|do not|doesn'?t|can'?t)\b[^.]{0,20}exceptions?", " ", text, flags=re.I)


def _parse_policy_limits(grounding: str) -> dict:
    lim: dict = {}
    m = re.search(r"\bmax(?:imum)?\s*(?:of\s*)?(\d+)|(\d+)\s+pets?\s+(?:max|maximum|limit)", grounding, re.I)
    if m:
        lim["max_pets"] = int(m.group(1) or m.group(2))
    m = re.search(r"\bdogs?\s*(?:<|under|below|less than)\s*(\d+)\s*(?:lbs?|pounds?)?", grounding, re.I)
    if m:
        lim["dog_weight_lt"] = int(m.group(1))  # strictly under
    return lim


def check_policy_limits_enforced(trace_data: TraceData) -> CheckResult:
    """
    Pattern #2 - Policy Limit Violation (EARS: reject values over a limit; no invented
    exceptions).

    Steps
      1. Parse limits from context + tool results: "max N" (pets), "dogs <N lbs".
      2. Parse the request from user_input: pet counts ("3 cats", "two Rottweilers"),
         weights ("50 pounds"), dogs with no stated weight, and a user budget cap
         ("under $2000", treated as a limit the bot must respect).
      3. Classify the response as approving / rejecting.
      4. FAIL when
           a. request exceeds a limit and the response approves (or does not reject),
           b. response states a dog weight above the limit without rejecting it,
           c. a softening exception not present in the KB is invented
              ("if calm", "usually", "as long as you pay", "we make exceptions"),
           d. a dog question with no stated weight is approved without mentioning the
              weight rule (ha-004),
           e. response recommends a price above the user's cap without flagging it.
      5. PASS when the request is within limits / the response rejects, and no invented
         exception appears. Requests that touch no limit pass trivially.

    Embedded tests:
      ha-013 "Can I have 3 cats?" / "3 cats is fine as long as you pay deposits"
             -> (False, "... 3 pets requested, policy max 2 ...")
      ha-019 "dogs over 50 pounds if calm?" / "If they're calm we usually make exceptions
             for dogs up to 60 lbs"            -> (False, "... invented exception ...")
      ha-004 "two Rottweilers" / "Yes, Rottweilers are allowed! ... all set for both"
             -> (False, "... weight rule (<40 lbs) not applied ...")
      ha-015 "2bed under $2000" / "Maple $1950 fits. Pine is $2100 so skip that"
             -> (True,  "Budget cap $2000 respected ...")
      ha-014 "Send pet policy link" / link only -> (True, ...)   # no limit engaged
      ha-001 "2 bedrooms?"                       -> (True, "No numeric limit engaged ...")
    """
    nd = _normalize(trace_data)
    resp, grounding, user = nd["response"], nd["grounding"], nd["user"]
    limits = _parse_policy_limits(grounding)
    problems: list[str] = []
    engaged = False

    pet_topic = bool(_PET_RE.search(user))
    requested = []
    for m in _PET_COUNT_RE.finditer(user):
        n = m.group(1).lower()
        requested.append((int(n) if n.isdigit() else _NUMWORDS[n], m.group(2).lower()))
    req_total = sum(n for n, _ in requested)
    user_weights = [int(w) for w in re.findall(r"(\d+)\s*-?\s*(?:lbs?\b|pounds?\b)", user, re.I)]
    dog_asked = bool(_DOG_RE.search(user))

    resp_approves = _approves(resp)
    resp_rejects = bool(_REJECT_RE.search(resp))

    # a. pet count over max ----------------------------------------------------
    if "max_pets" in limits and req_total:
        engaged = True
        if req_total > limits["max_pets"] and (resp_approves or not resp_rejects):
            problems.append(
                f"{req_total} pets requested, policy max {limits['max_pets']}, response approves: "
                f"'{_short(resp, 60)}'"
            )

    # b. weight ----------------------------------------------------------------
    if "dog_weight_lt" in limits:
        cap = limits["dog_weight_lt"]
        if user_weights:
            engaged = True
            if any(w >= cap for w in user_weights) and resp_approves and not resp_rejects:
                problems.append(f"{max(user_weights)} lb dog requested, policy is under {cap} lbs, response approves")
        for s in _sentences(resp):
            for w in re.findall(r"(\d+)\s*-?\s*(?:lbs?\b|pounds?\b)", s, re.I):
                if int(w) >= cap and not _REJECT_RE.search(s):
                    engaged = True
                    problems.append(f"response allows {w} lbs, policy is under {cap} lbs")

        # d. dog asked, no weight, approved without weight rule -----------------
        if dog_asked and not user_weights:
            engaged = True
            mentions_weight = bool(re.search(r"\bweigh|\bweight|\blbs?\b|\bpounds?\b", resp, re.I))
            if resp_approves and not resp_rejects and not mentions_weight:
                problems.append(f"dog approved without applying weight rule (<{cap} lbs)")

    # c. invented softening exception ------------------------------------------
    if limits and pet_topic:
        engaged = True
        cleaned = _strip_negated_exceptions(resp)
        m = _SOFTENER_RE.search(cleaned)
        if m and not _SOFTENER_RE.search(_strip_negated_exceptions(grounding)):
            problems.append(f"invented exception '{_short(m.group(0), 40)}' not in policy")

    # e. user budget cap ---------------------------------------------------------
    cap_m = re.search(r"(?:under|below|less than|max(?:imum)?|up to|no more than|<)\s*(\$)?\s*(\d[\d,]*)", user, re.I)
    budget = None
    if cap_m:
        val = int(cap_m.group(2).replace(",", ""))
        if cap_m.group(1) or val >= 500:
            budget = val
    if budget:
        engaged = True
        for s in _sentences(resp):
            for m in re.finditer(r"\$\s*(\d[\d,]*)", s):
                price = int(m.group(1).replace(",", ""))
                if price > budget and not _NEG_CUE_PRICE_RE.search(s):
                    problems.append(f"recommends ${price} over user budget ${budget}")

    problems = list(dict.fromkeys(problems))
    if problems:
        return False, _join_reason("Policy limit", problems)
    if budget:
        return True, f"Budget cap ${budget} respected; no policy limit violated"
    if engaged:
        return True, "Request handled within policy limits; no invented exceptions"
    return True, "No numeric policy limit engaged by this request"


# ---------------------------------------------------------------------------
# Check 3 - unauthorized action claims
# ---------------------------------------------------------------------------

# action -> (past-participle verbs used in claims, tool-name keywords, policy-forbid verbs)
_ACTIONS = {
    "hold": (r"held|reserved", r"hold|reserve", r"hold|reserve"),
    "email": (r"emailed|e-mailed|mailed|texted", r"email|mail|send|text|sms", r"email|e-mail|send|mail"),
    "confirm": (r"confirmed|booked|scheduled|approved|registered|enrolled", r"confirm|book|schedule|approve|register", r"confirm|book|schedule|approve"),
    "submit": (r"submitted|processed|charged|cancell?ed", r"submit|process|charge|cancel|pay", r"submit|process|charge|cancel"),
}
_CONDITIONAL_RE = re.compile(r"\b(?:if|once|when|unless|after|until)\b", re.I)
_OBJ_SEND_RE = re.compile(
    r"\b(?:I|we)(?:'ve|\s+have)?\s+(?:just\s+|already\s+)?sent\s+(?:you\s+|over\s+)?(?:an?\s+|the\s+|your\s+)?"
    r"(?:email|e-mail|contract|lease|application|confirmation|invoice|receipt)",
    re.I,
)


def _claim_regex(verbs: str) -> re.Pattern:
    return re.compile(
        rf"\b(?:I|we)(?:'ve|\s+have)?\s+(?:just\s+|already\s+|gone ahead and\s+)?(?:{verbs})\b|"
        rf"\b(?:is|are|was|were|been|got|now)\s+(?:now\s+|already\s+|successfully\s+)?(?:{verbs})\b|"
        rf"\b(?:you'?re|you are|you've been|you have been)\s+(?:now\s+|all\s+)?(?:{verbs})\b",
        re.I,
    )


_CLAIM_RES = {a: _claim_regex(v[0]) for a, v in _ACTIONS.items()}


def _find_action_claims(resp: str) -> list[tuple[str, str]]:
    """[(action, sentence)] for affirmative completed-action claims; negated/hypothetical skipped."""
    claims = []
    for s in _sentences(resp):
        for action, rx in _CLAIM_RES.items():
            m = rx.search(s)
            if action == "email" and not m:
                m = _OBJ_SEND_RE.search(s)
            if not m:
                continue
            before = s[: m.start()]
            if _NEG_RE.search(s[max(0, m.start() - 40): m.end()]) or _CONDITIONAL_RE.search(before):
                continue
            claims.append((action, s))
    return claims


def check_no_unauthorized_claims(trace_data: TraceData) -> CheckResult:
    """
    Pattern #3 - Unauthorized Action Claim (EARS: out-of-authority actions are handed off,
    never claimed as executed).

    Steps
      1. Find completed-action claims in the response (hold / email / confirm / submit),
         in first-person ("I've held it", "I emailed"), passive ("Maple is held") or
         "you're confirmed" form. Negated ("I can't hold") and conditional ("once the
         office confirms") sentences are not claims.
      2. Parse restricted actions from policy text ("Bot cannot hold units").
      3. Look for a tool call whose name/args match the action and did not error.
      4. FAIL if a claim is on a restricted action (policy wins even if a tool ran) or
         has no matching tool execution. PASS if there are no claims (escalating or
         declining is the desired behaviour) or every claim is tool-backed and permitted.

    Embedded tests:
      ha-005 "I've held it under your name for 48 hours - you're confirmed!"
             -> (False, "Claims hold ...policy forbids... no tool")
      ha-016 "Done - Maple is held and I emailed your contract"
             -> (False, "Claims hold; email ...")
      ha-003 "Please consult our leasing concierge..." -> (True, ...)  # handoff, not a claim
      ha-004 "You're all set for both."                -> (True, ...)  # not treated as a claim
    """
    nd = _normalize(trace_data)
    claims = _find_action_claims(nd["response"])
    if not claims:
        return True, "No action-execution claims (escalation/decline or info only)"

    policy = nd["context"]
    problems: list[str] = []
    for action, sentence in claims:
        verbs, tool_kw, forbid_kw = _ACTIONS[action]
        forbids = re.search(
            rf"(?:cannot|can'?t|can not|unable to|not able to|may not|must not|does not|do not|don'?t)\s+(?:\w+\s+){{0,2}}?(?:{forbid_kw})",
            policy,
            re.I,
        )
        backed = False
        for t in nd["tool_calls"]:
            ident = f"{t.get('name', '')} {json.dumps(t.get('args', {}), default=str)}"
            res = _as_text(t.get("result"))
            if re.search(tool_kw, ident, re.I) and not re.search(r"error|fail|denied|unauthori", res, re.I):
                backed = True
                break
        if forbids:
            problems.append(f"claims {action} ('{_short(sentence, 50)}') but policy says '{forbids.group(0)}'")
        elif not backed:
            problems.append(f"claims {action} ('{_short(sentence, 50)}') but no tool call executed it")

    problems = list(dict.fromkeys(problems))
    if problems:
        return False, _join_reason("Unauthorized action claim", problems)
    return True, "Action claims backed by executed tool calls and permitted by policy"


# ---------------------------------------------------------------------------
# Registry / runner / persistence
# ---------------------------------------------------------------------------

CHECKS: dict[str, Callable[[TraceData], CheckResult]] = {
    "check_no_ungrounded_claims": check_no_ungrounded_claims,
    "check_policy_limits_enforced": check_policy_limits_enforced,
    "check_no_unauthorized_claims": check_no_unauthorized_claims,
}


def run_all_checks(trace_data: TraceData) -> dict:
    """Run all checks; return {check_name: (pass, reason)}.

    A check that raises is recorded as a FAIL with a CHECK_ERROR reason (never a silent
    pass), so a bug in a check is visible in the results table.
    """
    out = {}
    for name, fn in CHECKS.items():
        try:
            out[name] = fn(trace_data)
        except Exception as e:  # noqa: BLE001 - fail verbosely, never pass silently
            print(f"CHECK_ERROR {name} on {trace_data.get('trace_id')}: {e!r}", file=sys.stderr)
            out[name] = (False, f"CHECK_ERROR: {type(e).__name__}: {e}")
    return out


def run_all_with_timing(trace_data: TraceData) -> list[dict]:
    """Rows shaped for internship.eval_check_results (adds latency_ms)."""
    rows = []
    for name, fn in CHECKS.items():
        t0 = time.perf_counter()
        try:
            passed, reason = fn(trace_data)
        except Exception as e:  # noqa: BLE001
            print(f"CHECK_ERROR {name} on {trace_data.get('trace_id')}: {e!r}", file=sys.stderr)
            passed, reason = False, f"CHECK_ERROR: {type(e).__name__}: {e}"
        rows.append({
            "trace_id": trace_data["trace_id"],
            "check_name": name,
            "check_type": "code_based",
            "passed": bool(passed),
            "reason": reason[:500],
            "latency_ms": int((time.perf_counter() - t0) * 1000),
        })
    return rows


def save_results(rows: list[dict], run_label: str = 'baseline') -> int:
    """
    Upsert rows into internship.eval_check_results with run_label for before/after tracking.

    Args:
        rows: list of dicts with trace_id, check_name, check_type, passed, reason, latency_ms
        run_label: identifier for this run (e.g., 'baseline', 'after_fix')

    Idempotent: re-running with the same run_label replaces earlier results for (trace, check, run_label).
    FK: every trace_id must already exist in internship.traces.
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from sqlalchemy import text  # imported lazily so the checks stay stdlib-only

    from db import get_engine

    # Add run_label to each row
    for row in rows:
        row['run_label'] = run_label

    sql = text(
        """
        INSERT INTO internship.eval_check_results
            (trace_id, check_name, check_type, pass, reason, latency_ms, run_label)
        VALUES (:trace_id, :check_name, :check_type, :passed, :reason, :latency_ms, :run_label)
        ON CONFLICT (trace_id, check_name, run_label) DO UPDATE SET
            check_type = EXCLUDED.check_type, pass = EXCLUDED.pass,
            reason = EXCLUDED.reason, latency_ms = EXCLUDED.latency_ms, created_at = now()
        """
    )
    with get_engine().begin() as conn:
        conn.execute(sql, rows)
    return len(rows)


# ---------------------------------------------------------------------------
# Self-test against the 20 real traces
# ---------------------------------------------------------------------------

DEFAULT_TRACES = Path(__file__).resolve().parents[3] / "syllabus" / "week4" / "harmony-apartments-traces.jsonl"

# Traces each check must FAIL; every other trace must PASS.
EXPECTED = {
    # Labelled Pattern #1: 003 006 007 009 010 012 017. Extras are genuine ungrounded values
    # found in traces filed under other patterns (see module docstring).
    "check_no_ungrounded_claims": {"ha-003", "ha-006", "ha-007", "ha-009", "ha-010", "ha-012", "ha-017",
                                   "ha-004", "ha-005", "ha-008", "ha-011", "ha-019"},
    "check_policy_limits_enforced": {"ha-004", "ha-013", "ha-019"},
    "check_no_unauthorized_claims": {"ha-005", "ha-016"},
}
# Spec'd PASS cases that must never regress.
MUST_PASS = {"check_no_ungrounded_claims": {"ha-014", "ha-015"}, "check_policy_limits_enforced": {"ha-015"}}


def load_traces(path: Path) -> list[dict]:
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def selftest(traces: list[dict], verbose: bool = True) -> bool:
    ok = True
    for name, fn in CHECKS.items():
        for t in traces:
            passed, reason = fn(t)
            want_fail = t["trace_id"] in EXPECTED[name]
            if passed == want_fail:  # passed but expected fail, or failed but expected pass
                ok = False
                print(f"MISMATCH {name} {t['trace_id']}: got pass={passed} ({reason})", file=sys.stderr)
        for tid in MUST_PASS.get(name, ()):
            t = next(x for x in traces if x["trace_id"] == tid)
            assert fn(t)[0], f"spec regression: {name} must pass {tid}"
    if verbose:
        print("SELFTEST", "OK" if ok else "FAILED", f"({len(traces)} traces x {len(CHECKS)} checks)")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("traces", nargs="?", default=str(DEFAULT_TRACES))
    ap.add_argument("--selftest", action="store_true", help="only run the self-test")
    ap.add_argument("--save", action="store_true", help="upsert into internship.eval_check_results (PRODUCTION DB)")
    ap.add_argument("--run-label", default="baseline", help="label for this run (e.g., 'baseline', 'after_fix') [default: baseline]")
    args = ap.parse_args()

    traces = load_traces(Path(args.traces))
    if args.selftest:
        return 0 if selftest(traces) else 1

    rows = []
    for t in traces:
        rows.extend(run_all_with_timing(t))
    for r in rows:
        print(f"{r['trace_id']}  {r['check_name']:<30} {'PASS' if r['passed'] else 'FAIL'}  {r['reason']}")
    print()
    for name in CHECKS:
        n_fail = sum(1 for r in rows if r["check_name"] == name and not r["passed"])
        print(f"{name}: {len(traces) - n_fail}/{len(traces)} pass")
    ok = selftest(traces)
    if args.save:
        saved = save_results(rows, run_label=args.run_label)
        print(f"saved {saved} rows to internship.eval_check_results (run_label='{args.run_label}')")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
