#!/usr/bin/env python3
"""Lint research findings files against the schema in
p3m3/research-skill-revision-plan.md section 5.3/5.4.

Stdlib only, deterministic, no network. All limits, enums, tiers and patterns
come from research_config.json (default: beside this script). This checks FORM,
not truth: a pass does not mean the content is right.

Exit codes: 0 clean, 1 rule errors, 2 usage/IO/config error,
            3 config fingerprint mismatch (F02).
"""
import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

# Fixed schema constant: changing the column order is a code change (plan 5.5).
SOURCE_COLUMNS = [
    "id", "url", "title", "author_org", "year", "portal", "reliability",
    "read_status", "quote", "sub_claim", "stance", "strength", "verifier_status",
]
VERDICT_COLUMNS = ["sub_claim", "verdict", "supporting ids", "antagonistic ids"]
EXIT_OK, EXIT_FINDINGS, EXIT_USAGE, EXIT_FINGERPRINT = 0, 1, 2, 3
# Why: arXiv site-documentation pages carry no paper id; see the F04 check.
ARXIV_HELP_PATH_PREFIX = "/help/"
URL_RE = re.compile(r"https?://[^\s|;,<>\"')\]]+")
SRC_TAG_RE = re.compile(r"\[src:([^\]]+)\]")
MEASURED_TAG_RE = re.compile(r"\[measured:[^\]]*\]")
NUMBER_RE = re.compile(r"\d[\d,]*(?:\.\d+)?")
# P03 context filters (#96 a). Not config-gated: they only REMOVE false positives in --plan mode.
DESIGN_TAG_RE = re.compile(r"\[design:[^\]]*\]")
# Identifier/label tokens: start with a letter, contain a digit (R1-1, D-028, H3a, S2, CWE-209, gpt-4.1).
LABEL_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z][A-Za-z0-9_]*(?:-[A-Za-z0-9_]+|(?<=\d)\.\d+)*")
# Regions that carry no claim numbers: code spans, link targets, bare URLs, ISO dates/months.
P03_MASK_RES = [re.compile(r"`[^`]*`"), re.compile(r"\]\([^)]*\)"), re.compile(r"https?://\S+"),
                re.compile(r"\b\d{4}-\d{2}(?:-\d{2})?\b"),
                # "by 10-07" (MM-DD after a date preposition); a bare "10-20" stays a range
                re.compile(r"(?i)\b(?:by|on|before|until|after|since|due|from|through)\s+(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])\b")]
# A number directly after one of these words is a reference (section 7.2, Table 3, RFC 7231), not a claim.
REF_WORDS = ("section|sections|sec|table|figure|fig|appendix|item|step|week|phase|milestone|gate|option|"
             "iso|iec|rfc|ieee|sp|cwe|cve|no")
REF_BEFORE_RE = re.compile(r"(?i)(?:\b(?:" + REF_WORDS + r")\.?|§§?|#)\s*(?:[\d.]+\s*(?:,|and|&|[-\u2013]|to)\s*)*$")
YEAR_RE = re.compile(r"(?:19|20)\d\d")
# Words that make a 4-digit number a quantity rather than a year ("2000 questions").
UNIT_AFTER_RE = re.compile(r"(?i)^(?:%|[a-z]|\s+(?:percent|ms|s|sec|seconds|minutes|tokens|questions|rows|files|users|"
                           r"requests|calls|items|hits|docs|documents|chunks|bytes|kb|mb|gb|x|times|points|pp)\b)")
SEPARATOR_RE = re.compile(r"^\|?[\s:\-|]+\|?$")
DEFAULT_CONFIG = Path(__file__).with_name("research_config.json")


class Finding:
    def __init__(self, path, line, code, msg, severity="error"):
        self.path, self.line, self.code, self.msg, self.severity = path, line, code, msg, severity

    def text(self):
        tag = "" if self.severity == "error" else " (warning)"
        return f"{self.path}:{self.line}: {self.code}{tag} {self.msg}"

    def as_dict(self):
        return {"path": str(self.path), "line": self.line, "code": self.code,
                "severity": self.severity, "message": self.msg}


class UsageError(Exception):
    pass


# ---------------------------------------------------------------- config
def canonical_json(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def load_config(path):
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except OSError as e:
        raise UsageError(f"cannot read config {path}: {e}")
    try:
        cfg = json.loads(raw)
    except json.JSONDecodeError as e:
        raise UsageError(f"config {path} is not valid JSON: {e}")
    if not isinstance(cfg, dict):
        raise UsageError(f"config {path} must be a JSON object")
    fingerprint = hashlib.sha256(canonical_json(cfg).encode("utf-8")).hexdigest()
    return cfg, fingerprint


def cfg_get(cfg, dotted):
    cur = cfg
    for part in dotted.split("."):
        if not isinstance(cur, dict) or part not in cur:
            raise UsageError(f"config is missing required key '{dotted}'")
        cur = cur[part]
    return cur


def read_lines(path):
    try:
        return Path(path).read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as e:
        raise UsageError(f"cannot read {path}: {e}")


# ---------------------------------------------------------------- parsing
def split_row(line):
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    s = s.replace("\\|", "\x00")
    return [c.strip().replace("\x00", "|") for c in s.split("|")]


def parse_tables(lines):
    """Return list of tables: dict(header, header_line, rows=[(lineno, cells)], heading)."""
    tables, heading, i = [], "", 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("#"):
            heading = ln.lstrip("#").strip().lower()
        if ln.lstrip().startswith("|"):
            start = i
            block = []
            while i < len(lines) and lines[i].lstrip().startswith("|"):
                block.append((i + 1, lines[i]))
                i += 1
            if len(block) >= 2 and SEPARATOR_RE.match(block[1][1].strip()):
                rows = [(n, split_row(t)) for n, t in block[2:]]
                tables.append({"header": split_row(block[0][1]), "header_line": start + 1,
                               "rows": rows, "heading": heading})
            continue
        i += 1
    return tables


def parse_header(lines, cfg, path, findings):
    marker = cfg_get(cfg, "lint.header_marker")
    first = next((n for n, t in enumerate(lines) if t.strip()), None)
    if first is None:
        findings.append(Finding(path, 1, "F01", "file is empty; expected a research-header comment on line 1"))
        return None, 1
    text = lines[first].strip()
    m = re.match(r"^<!--\s*" + re.escape(marker) + r"\s+(\{.*\})\s*-->$", text)
    if not m:
        findings.append(Finding(path, first + 1, "F01",
                                f"first line is not '<!-- {marker} {{json}} -->' (pre-schema file? try --legacy)"))
        return None, first + 1
    try:
        hdr = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        findings.append(Finding(path, first + 1, "F01", f"header JSON invalid: {e}"))
        return None, first + 1
    missing = [k for k in cfg_get(cfg, "lint.header_required_keys") if k not in hdr]
    if missing:
        findings.append(Finding(path, first + 1, "F01", "header missing required keys: " + ", ".join(missing)))
    return hdr, first + 1


# ---------------------------------------------------------------- secrets
def scan_secrets(path, lines, cfg, findings):
    """Flag secret-looking strings. The matched text is NEVER printed."""
    pats = {name: re.compile(p) for name, p in cfg_get(cfg, "secret_patterns").items()}
    for n, text in enumerate(lines, 1):
        for name, rx in pats.items():
            if rx.search(text):
                findings.append(Finding(path, n, "S01",
                                        f"secret-looking string (pattern '{name}'); value withheld, remove it"))


# ---------------------------------------------------------------- helpers
def host_tier(host, cfg):
    host = (host or "").lower()
    for tier, domains in cfg_get(cfg, "reliability_tiers").items():
        for d in domains:
            if host == d or host.endswith("." + d):
                return tier
    return cfg_get(cfg, "reliability_default_tier")


def is_blank(cell, cfg):
    return cell.strip() in cfg_get(cfg, "lint.no_value_markers")


def blocking(row, cfg):
    ec = cfg_get(cfg, "evidence_chain")
    return (row["read_status"] in ec["blocking_read_states"]
            or row["verifier_status"] in ec["blocking_verifier_states"])


def split_ids(cell):
    return [t for t in re.split(r"[,\s;]+", cell.strip()) if t and t != "-"]


def split_subclaims(cell):
    return [t for t in re.split(r"[,\s;]+", cell.strip()) if t]


# ---------------------------------------------------------------- strict checks
def check_findings_file(path, lines, cfg, fingerprint, ids_seen, findings):
    """Validate one findings file. Returns (header, rows) with rows as dicts."""
    scan_secrets(path, lines, cfg, findings)
    hdr, hdr_line = parse_header(lines, cfg, path, findings)
    hdr = hdr or {}
    if hdr.get("config_sha256") is not None and hdr["config_sha256"] != fingerprint:
        findings.append(Finding(path, hdr_line, "F02",
                                f"config fingerprint mismatch: file ran under config_version "
                                f"{hdr.get('config_version')!r} sha {str(hdr['config_sha256'])[:12]}..., "
                                f"given config is version {cfg.get('config_version')!r} sha {fingerprint[:12]}... "
                                f"(use --config at the old version, or re-run)"))
    if hdr.get("date") and not re.fullmatch(cfg_get(cfg, "lint.date_pattern"), str(hdr["date"])):
        findings.append(Finding(path, hdr_line, "F01", f"header date {hdr['date']!r} is not YYYY-MM-DD"))
    limit, used = hdr.get("budget_limit"), hdr.get("budget_used")
    if limit is not None and used is not None:
        if not (isinstance(limit, int) and isinstance(used, int)) or isinstance(limit, bool) or isinstance(used, bool):
            findings.append(Finding(path, hdr_line, "F09", "budget_limit and budget_used must be integers"))
        elif used > limit:
            findings.append(Finding(path, hdr_line, "F09", f"budget_used {used} exceeds budget_limit {limit}"))
    subclaims = hdr.get("subclaims") if isinstance(hdr.get("subclaims"), list) else []
    rid = str(hdr.get("research_id", ""))

    tables = parse_tables(lines)
    src_tables = []
    verdict_table = None
    for t in tables:
        h = [c.strip().lower() for c in t["header"]]
        if h == VERDICT_COLUMNS or (h and h[0] == "sub_claim" and "verdict" in h):
            verdict_table = verdict_table or t
        elif h and h[0] == "id":
            src_tables.append(t)
    if not src_tables:
        findings.append(Finding(path, 1, "F03", "no source table found (first column must be 'id')"))
        return hdr, []
    # Why: every source table is validated (a VERIFIED in a later table must not escape the rules),
    # and a second source table is itself an error because the schema allows exactly one.
    for extra in src_tables[1:]:
        findings.append(Finding(path, extra["header_line"], "F03",
                                "more than one source table (first column 'id'); the schema allows exactly one"))
    for src_table in src_tables:
        if src_table["header"] != SOURCE_COLUMNS:
            findings.append(Finding(path, src_table["header_line"], "F03",
                                    "source table columns differ from schema; expected "
                                    + " | ".join(SOURCE_COLUMNS) + "; got " + " | ".join(src_table["header"])))

    enums = {
        "read_status": cfg_get(cfg, "read_status_values"),
        "stance": cfg_get(cfg, "stance_values"),
        "strength": cfg_get(cfg, "strength_values"),
        "verifier_status": cfg_get(cfg, "verifier_status_values"),
    }
    max_words = cfg_get(cfg, "quote_max_words")
    prefix = cfg_get(cfg, "lint.non_verbatim_quote_prefix")
    id_rx = re.compile(cfg_get(cfg, "lint.source_id_pattern").replace("{research_id}", re.escape(rid)))
    tiers = cfg_get(cfg, "reliability_order")
    override_rx = re.compile(cfg_get(cfg, "lint.override_cell_pattern"))
    arxiv_hosts = cfg_get(cfg, "lint.arxiv_hosts")
    arxiv_rx = re.compile(cfg_get(cfg, "lint.arxiv_id_pattern"))
    anti = set(cfg_get(cfg, "antagonistic_stances"))
    rows, urls_seen = [], {}
    all_src_rows = [rc for t in src_tables for rc in t["rows"]]
    for lineno, cells in all_src_rows:
        if len(cells) != len(SOURCE_COLUMNS):
            findings.append(Finding(path, lineno, "F03",
                                    f"row has {len(cells)} cells, expected {len(SOURCE_COLUMNS)} "
                                    f"(a '|' inside a cell must be written '\\|')"))
            continue
        r = dict(zip(SOURCE_COLUMNS, cells))
        r["_line"] = lineno
        rows.append(r)
        # id
        if not id_rx.fullmatch(r["id"]):
            findings.append(Finding(path, lineno, "F13", f"source id {r['id']!r} does not match the id pattern for research_id {rid!r}"))
        key = (rid, r["id"])
        if key in ids_seen:
            findings.append(Finding(path, lineno, "F13",
                                    f"duplicate source id {r['id']!r} (first at {ids_seen[key][0]}:{ids_seen[key][1]})"))
        else:
            ids_seen[key] = (path, lineno)
        # url
        found = URL_RE.findall(r["url"])
        parsed = urlparse(r["url"].strip())
        if len(found) != 1 or re.search(r"[;\s]", r["url"].strip()):
            findings.append(Finding(path, lineno, "F04", f"url cell must hold exactly one URL, found {len(found)}"))
        elif parsed.scheme not in ("http", "https") or "." not in (parsed.hostname or ""):
            findings.append(Finding(path, lineno, "F04", "url is not a well-formed absolute http(s) URL"))
        else:
            if r["url"] in urls_seen:
                findings.append(Finding(path, lineno, "W08",
                                        f"duplicate URL (also row at line {urls_seen[r['url']]}); merge rows", "warning"))
            urls_seen.setdefault(r["url"], lineno)
            aid = None
            # Why: arXiv's own site documentation (terms of use, API help) lives under /help/ and has no paper id.
            # Narrow on purpose: any other arXiv path still needs a valid id (false positive found 2026-10-02).
            if parsed.path.startswith(ARXIV_HELP_PATH_PREFIX):
                pass
            elif parsed.hostname in arxiv_hosts or any(parsed.hostname.endswith("." + h) for h in arxiv_hosts):
                m = arxiv_rx.search(parsed.path)
                if not m:
                    findings.append(Finding(path, lineno, "F04", "arXiv URL has no valid arXiv id"))
                else:
                    aid = m.group(0)
            if aid:
                yy, mm = int(aid[:2]), int(aid[2:4])
                if re.fullmatch(cfg_get(cfg, "lint.year_pattern"), r["year"]) and (2000 + yy != int(r["year"]) or not 1 <= mm <= 12):
                    findings.append(Finding(path, lineno, "W12",
                                            f"arXiv id {aid} implies {2000 + yy}-{mm:02d} but year cell is {r['year']!r}", "warning"))
        # year
        if not re.fullmatch(cfg_get(cfg, "lint.year_pattern"), r["year"]):
            findings.append(Finding(path, lineno, "F05", f"year {r['year']!r} is not 4 digits"))
        # enums
        for col, allowed in enums.items():
            if r[col] not in allowed:
                findings.append(Finding(path, lineno, "F05",
                                        f"{col} {r[col]!r} not in {allowed}"))
        # sub-claims
        sc = split_subclaims(r["sub_claim"])
        if not sc:
            findings.append(Finding(path, lineno, "F05", "sub_claim is empty; map the row to at least one sub-claim"))
        for s in sc:
            if subclaims and s not in subclaims:
                findings.append(Finding(path, lineno, "F05", f"sub_claim {s!r} is not in the header's subclaims {subclaims}"))
        # reliability (F05 enum + F07 tier)
        rel = r["reliability"]
        m = override_rx.fullmatch(rel)
        base = m.group(1) if m else rel
        if base not in tiers:
            findings.append(Finding(path, lineno, "F05", f"reliability {rel!r} is not one of {tiers} (or 'tier (override: reason)')"))
        elif parsed.hostname:
            expected = host_tier(parsed.hostname, cfg)
            if m:
                if not m.group(2).strip():
                    findings.append(Finding(path, lineno, "F07", "reliability override has no reason"))
                elif abs(tiers.index(base) - tiers.index(expected)) > cfg_get(cfg, "lint.override_max_level_distance"):
                    findings.append(Finding(path, lineno, "F07",
                                            f"override moves tier by more than one level (config tier for {parsed.hostname} is {expected!r})"))
            elif base != expected:
                msg = f"reliability {base!r} != config tier {expected!r} for host {parsed.hostname!r}"
                if cfg_get(cfg, "reliability_override_requires_reason"):
                    msg += "; write 'tier (override: <reason>)' to override"
                findings.append(Finding(path, lineno, "F07", msg))
        # quote
        q = r["quote"].strip()
        status = r["read_status"]
        if is_blank(q, cfg):
            if status.startswith("READ-"):
                findings.append(Finding(path, lineno, "F06", f"{status} row has no quote"))
        else:
            tilde = q.startswith(prefix)
            body = q[len(prefix):].strip() if tilde else q
            if not (len(body) >= 2 and body.startswith('"') and body.endswith('"')):
                findings.append(Finding(path, lineno, "F06", "quote must be wrapped in double quotes"))
            inner = body.strip('"')
            words = len(inner.split())
            if words > max_words:
                findings.append(Finding(path, lineno, "F06", f"quote has {words} words, limit is {max_words}"))
            if status == "READ-VERBATIM" and tilde:
                findings.append(Finding(path, lineno, "F06", f"READ-VERBATIM quote must not carry the '{prefix}' paraphrase prefix"))
            if status != "READ-VERBATIM" and not tilde:
                findings.append(Finding(path, lineno, "F06",
                                        f"quote from non-verbatim read ({status}) must start with '{prefix}' (paraphrase)"))

    # F10 antagonistic coverage
    gap_lines = section_text(lines, "gaps")
    need = cfg_get(cfg, "min_antagonistic_per_subclaim")
    for s in subclaims:
        n = sum(1 for r in rows if s in split_subclaims(r["sub_claim"]) and r["stance"] in anti)
        if n < need and not re.search(r"(?<![A-Za-z0-9])" + re.escape(s) + r"(?![A-Za-z0-9])", gap_lines):
            findings.append(Finding(path, hdr_line, "F10",
                                    f"sub-claim {s} has {n} antagonistic row(s), needs {need}, and no '## Gaps' line names it"))
    # F12/F14 (config_version >= 5 files only; older files keep passing under their own config)
    check_v5_rules(path, hdr, hdr_line, subclaims, rows, gap_lines, cfg, findings)
    # F15/F16 (config_version >= 6 files only)
    check_v6_rules(path, hdr, hdr_line, rows, cfg, findings)
    # F11 verdicts
    check_verdicts(path, verdict_table, subclaims, rows, cfg, findings, hdr_line)
    return hdr, rows


def header_version(hdr):
    """Integer config_version of a findings header, or None if absent/non-numeric."""
    try:
        return int(str(hdr.get("config_version", "")).strip())
    except ValueError:
        return None


def is_antagonist_file(hdr):
    """Adversarial (A*) or red-team (T*) file: by header angle or role."""
    role = str(hdr.get("role", "")).lower()
    return (hdr.get("angle") in ("against", "red_team")
            or "adversarial" in role or "redteam" in role or "red-team" in role)


GAP_LABEL_RE = re.compile(r"^\s*[-*]\s*\**\s*([A-Za-z0-9]+(?:\s*[,/&]\s*[A-Za-z0-9]+)*)\s*\**\s*[:\u2013\u2014-]")
GAP_QUERY_RE = re.compile(r"`[^`]+`|\"[^\"]+\"")


def parse_gap_lines(gap_text, subclaims):
    """Return [(label_subclaims, line_text)] for Gaps bullets that start with a sub-claim label."""
    out = []
    for ln in gap_text.splitlines():
        m = GAP_LABEL_RE.match(ln)
        if not m:
            continue
        labels = [t for t in re.split(r"[\s,/&]+", m.group(1)) if t in subclaims]
        if labels:
            out.append((labels, ln))
    return out


def gap_line_problems(text, cfg):
    """Why a Gaps line fails the dated-query format (empty list = valid)."""
    date_rx = re.compile(cfg_get(cfg, "lint.date_pattern").strip("^$"))
    reasons = cfg_get(cfg, "gap_reasons")
    problems = []
    if not GAP_QUERY_RE.search(text):
        problems.append("no query in backticks or double quotes")
    if not date_rx.search(text):
        problems.append("no YYYY-MM-DD date")
    if not any(r in text for r in reasons):
        problems.append("no reason token (one of " + ", ".join(reasons) + ")")
    return problems


def check_v5_rules(path, hdr, hdr_line, subclaims, rows, gap_text, cfg, findings):
    ver = header_version(hdr)
    if ver is None or ver < 5 or not is_antagonist_file(hdr):
        return
    if "min_fulltext_antagonistic_per_subclaim" not in cfg or "gap_reasons" not in cfg:
        return  # the config in use predates v5; nothing to enforce
    anti = set(cfg_get(cfg, "antagonistic_stances"))
    blocking_states = cfg_get(cfg, "evidence_chain.blocking_read_states")
    need = cfg_get(cfg, "min_fulltext_antagonistic_per_subclaim")
    gaps = parse_gap_lines(gap_text, subclaims)
    valid_gap_for = set()
    if cfg.get("gaps_require_dated_queries"):
        for labels, text in gaps:
            problems = gap_line_problems(text, cfg)
            if problems:
                findings.append(Finding(path, hdr_line, "F14",
                                        f"Gaps line for {','.join(labels)} is not a dated-query gap: "
                                        + "; ".join(problems)))
            else:
                valid_gap_for.update(labels)
    else:
        for labels, _ in gaps:
            valid_gap_for.update(labels)
    for s in subclaims:
        n = sum(1 for r in rows if s in split_subclaims(r["sub_claim"]) and r["stance"] in anti
                and r["read_status"] not in blocking_states)
        if n < need and s not in valid_gap_for:
            findings.append(Finding(path, hdr_line, "F12",
                                    f"sub-claim {s} has {n} full-text antagonistic row(s) (read_status not in "
                                    f"{blocking_states}), needs {need}, and no dated-query Gaps line for it"))


def cfg_version(cfg):
    try:
        return int(str(cfg.get("config_version", "")).strip())
    except ValueError:
        return None


def numeric_row_reason(row, cfg):
    """PR-96-01: why a source row counts as numeric/measured (needs a full-text read), else None.
    Reuses the P03 number-token logic (claim_numbers), so ids/labels/refs/dates and bare years are not counted."""
    strengths = cfg.get("lint", {}).get("numeric_row_strengths", ["measured"])
    q = str(row.get("quote", "")).strip()
    if q.startswith("~"):
        q = q[1:]
    nums = claim_numbers(q)
    if nums:
        return "quote contains number " + repr(nums[0])
    if str(row.get("strength", "")).strip() in strengths:
        return "strength is " + repr(str(row.get("strength")).strip())
    return None


def check_v6_rules(path, hdr, hdr_line, rows, cfg, findings):
    """F15 unknown header keys, F16 role consistency (#96 b), F17 numeric rows need a full-text read (PR-96-01).
    Applies only when BOTH the file header and the config in use are config_version >= 6, so v5 and older
    runs are unaffected."""
    ver, cver = header_version(hdr), cfg_version(cfg)
    if ver is None or ver < 6 or cver is None or cver < 6:
        return
    if cfg.get("lint", {}).get("numeric_rows_require_fulltext"):
        bstates = cfg_get(cfg, "evidence_chain.blocking_read_states")
        for r in rows:
            why = numeric_row_reason(r, cfg)
            if why and r["read_status"] in bstates:
                findings.append(Finding(path, r["_line"], "F17",
                                        f"row {r['id']} has read_status {r['read_status']} but {why}; "
                                        "fetch the page and read it (READ-VERBATIM or READ-SUMMARIZER), "
                                        "or remove the number and the 'measured' strength from this row"))
    if "header_optional_keys" in cfg.get("lint", {}):
        known = set(cfg_get(cfg, "lint.header_required_keys")) | set(cfg_get(cfg, "lint.header_optional_keys"))
        unknown = sorted(k for k in hdr if k not in known)
        if unknown:
            findings.append(Finding(path, hdr_line, "F15",
                                    "header has undeclared keys: " + ", ".join(unknown)
                                    + " (declare them in lint.header_optional_keys or remove them)"))
    kinds = cfg.get("role_consistency")
    if not isinstance(kinds, dict):
        return
    role, angle = str(hdr.get("role", "")), hdr.get("angle")
    # file-name kind vs role and angle
    name = Path(str(path)).name
    fkind = next((k for k, v in kinds.items() if re.search(r"-" + re.escape(v["file_kind"]) + r"(?:-|\.)", name)), None)
    if fkind:
        spec = kinds[fkind]
        if role not in spec["roles"]:
            findings.append(Finding(path, hdr_line, "F16",
                                    f"file name kind {spec['file_kind']!r} expects header role in {spec['roles']}, got {role!r}"))
        if angle in spec.get("angles_forbidden", []) or (
                spec.get("angles_required") and angle not in spec["angles_required"]):
            findings.append(Finding(path, hdr_line, "F16",
                                    f"file name kind {spec['file_kind']!r} is inconsistent with header angle {angle!r}"))
    # source-id role code vs role and angle
    codes_seen = {}
    for r in rows:
        m = re.fullmatch(r"[^-]+-([A-Za-z]+)-S\d+", r["id"])
        if m:
            codes_seen.setdefault(m.group(1)[0], r["_line"])
    for letter, ln in sorted(codes_seen.items()):
        spec = next((v for v in kinds.values() if letter in v["id_codes"]), None)
        if spec is None:
            findings.append(Finding(path, ln, "F16", f"source id role code {letter!r} is not a known role code"))
            continue
        if role not in spec["roles"]:
            findings.append(Finding(path, ln, "F16",
                                    f"source id role code {letter!r} expects header role in {spec['roles']}, got {role!r}"))
        if angle in spec.get("angles_forbidden", []) or (
                spec.get("angles_required") and angle not in spec["angles_required"]):
            findings.append(Finding(path, ln, "F16",
                                    f"source id role code {letter!r} is inconsistent with header angle {angle!r}"))
    if len(codes_seen) > 1:
        findings.append(Finding(path, hdr_line, "F16", "source ids mix role codes " + ", ".join(sorted(codes_seen))))


def version_number(p):
    m = re.search(r"\.v(\d+)$", p.name)
    return int(m.group(1)) if m else None


def latest_version(base):
    """Highest base.vN if any exists beside base, else base itself."""
    base = Path(base)
    best, best_n = base, -1
    for c in base.parent.glob(base.name + ".v*"):
        n = version_number(c)
        if n is not None and n > best_n:
            best, best_n = c, n
    return best


def run_findings_files(findings_dir, rid):
    """Latest version of every base findings file <rid>-*.md in a directory."""
    d = Path(findings_dir)
    if not d.is_dir():
        raise UsageError(f"--findings-dir: {findings_dir} is not a directory")
    return [latest_version(b) for b in sorted(d.glob(f"{rid}-*.md"))]


def parse_findings(lines, cfg):
    """Quiet parse for tooling (metrics): header, source rows (dicts), verdicts {sub_claim: verdict}, gap lines."""
    scratch = []
    hdr, _ = parse_header(lines, cfg, "<parse>", scratch)
    hdr = hdr or {}
    rows, verdicts = [], {}
    for t in parse_tables(lines):
        h = [c.strip().lower() for c in t["header"]]
        if h and h[0] == "sub_claim" and "verdict" in h:
            for _, cells in t["rows"]:
                if len(cells) >= 2:
                    verdicts[cells[0]] = cells[1]
        elif h and h[0] == "id":
            for _, cells in t["rows"]:
                if len(cells) == len(SOURCE_COLUMNS):
                    rows.append(dict(zip(SOURCE_COLUMNS, cells)))
    subclaims = hdr.get("subclaims") if isinstance(hdr.get("subclaims"), list) else []
    gaps = parse_gap_lines(section_text(lines, "gaps"), subclaims)
    return hdr, rows, verdicts, gaps


def is_red_team_file(hdr, rows=()):
    if hdr.get("angle") == "red_team":
        return True
    return any(re.fullmatch(r"[^-]+-T[a-z]{1,7}-S\d+", r.get("id", "")) for r in rows)


def check_run_red_team(findings_dir, rid, profile, cfg, findings):
    """R01: a run on a profile listed in red_team_file_required_for_profile needs a T-role file."""
    required = cfg.get("red_team_file_required_for_profile", [])
    files = run_findings_files(findings_dir, rid)
    if profile not in required:
        return files, False
    for f in files:
        hdr, rows, _, _ = parse_findings(read_lines(f), cfg)
        if is_red_team_file(hdr, rows):
            return files, True
    findings.append(Finding(findings_dir, 1, "R01",
                            f"run {rid} is on profile {profile!r} (requires a red-team file per config "
                            f"red_team_file_required_for_profile {required}) but none of its {len(files)} findings "
                            f"file(s) is a red-team (T-role / angle red_team) file"))
    return files, False


def resolve_run_profile(findings_dir, rid, explicit):
    if explicit:
        return explicit
    spec = Path(findings_dir) / f"{rid}-spec.json"
    try:
        prof = json.loads(spec.read_text(encoding="utf-8")).get("profile")
    except (OSError, ValueError, AttributeError):
        prof = None
    if not prof:
        raise UsageError(f"--run {rid}: profile unknown; pass --profile or provide {spec} with a 'profile' key "
                         f"(findings headers carry no profile field)")
    return prof


def section_text(lines, name):
    out, inside = [], False
    for t in lines:
        if t.startswith("#"):
            inside = t.lstrip("#").strip().lower() == name
            continue
        if inside:
            out.append(t)
    return "\n".join(out)


def check_verdicts(path, table, subclaims, rows, cfg, findings, hdr_line):
    if table is None:
        if subclaims:
            findings.append(Finding(path, hdr_line, "F11", "no '## Verdicts' table; every sub-claim needs a verdict"))
        return
    by_id = {r["id"]: r for r in rows}
    allowed = cfg_get(cfg, "verdict_values")
    blocking_states = cfg_get(cfg, "evidence_chain.blocking_read_states")
    covered = set()
    for lineno, cells in table["rows"]:
        if len(cells) < 4:
            findings.append(Finding(path, lineno, "F11", f"verdict row has {len(cells)} cells, expected 4"))
            continue
        sc, verdict, sup, ant = cells[:4]
        covered.add(sc)
        if subclaims and sc not in subclaims:
            findings.append(Finding(path, lineno, "F11", f"verdict for unknown sub-claim {sc!r}"))
        if verdict not in allowed:
            findings.append(Finding(path, lineno, "F11", f"verdict {verdict!r} not in {allowed}"))
        sup_ids, ant_ids = split_ids(sup), split_ids(ant)
        for sid in sup_ids + ant_ids:
            if sid not in by_id:
                findings.append(Finding(path, lineno, "F11", f"verdict cites source id {sid!r} that is not in the source table"))
        if verdict == "survived":
            known = [by_id[s] for s in sup_ids if s in by_id]
            if known and all(r["read_status"] in blocking_states for r in known):
                findings.append(Finding(path, lineno, "F11",
                                        "'survived' verdict rests only on SUMMARY/TITLE-ONLY/FETCH-FAILED sources"))
            if not sup_ids:
                findings.append(Finding(path, lineno, "F11", "'survived' verdict cites no supporting source ids"))
    for s in subclaims:
        if s not in covered:
            findings.append(Finding(path, table["header_line"], "F11", f"sub-claim {s} has no verdict row"))


# ---------------------------------------------------------------- plan mode
def claim_numbers(claim):
    """Numbers in a plan claim that must trace to a cited quote. Skips ids/labels, section refs, ISO dates,
    years not followed by a unit, code spans, link targets, enumerators, and numbers in the same sentence
    as (before) a [design:...] tag. Real quantities ("72%", "30 questions", "4.99M") are kept."""
    s = claim
    for m in DESIGN_TAG_RE.finditer(claim):
        cut = max([b.end() for b in re.finditer(r"[.;!?]\s", claim[:m.start()])] + [0])
        s = s[:cut] + " " * (m.end() - cut) + s[m.end():]
    for rx in P03_MASK_RES:
        s = rx.sub(lambda m: " " * len(m.group(0)), s)
    labels = [(m.start(), m.end()) for m in LABEL_TOKEN_RE.finditer(s)
              if any(c.isdigit() for c in m.group(0)) and s[m.end():m.end() + 1] != "%"]
    out = []
    for m in NUMBER_RE.finditer(s):
        text = m.group(0).rstrip(",")
        a = m.start()
        b = a + len(text)
        if any(la <= a and b <= lb for la, lb in labels):
            continue
        if YEAR_RE.fullmatch(text) and not UNIT_AFTER_RE.match(s[b:]):
            continue
        if REF_BEFORE_RE.search(s[:a]):
            continue
        if s[b:b + 2].lower() == "xx":
            continue  # HTTP status class such as 4xx/5xx
        if s[max(0, a - 1):a] == "(" and s[b:b + 1] == ")" and text.isdigit() and len(text) <= 2:
            continue
        if re.fullmatch(r"\s*(?:[-*]\s+)?", s[:a]) and s[b:b + 1] in (".", ")"):
            continue  # list marker
        out.append(m.group(0))
    return out


def check_plan(path, lines, cfg, all_rows, findings):
    scan_secrets(path, lines, cfg, findings)
    by_id = {r["id"]: r for r in all_rows}
    ec = cfg_get(cfg, "evidence_chain")
    verified_rx = re.compile(cfg_get(cfg, "lint.verified_label_pattern"), re.I)
    for n, text in enumerate(lines, 1):
        tags = SRC_TAG_RE.findall(text)
        if not tags:
            continue
        ids = [i for t in tags for i in split_ids(t)]
        known = []
        for i in ids:
            if i not in by_id:
                findings.append(Finding(path, n, "P01", f"cited source id {i!r} not found in any findings file given"))
            else:
                known.append(by_id[i])
        for r in known:
            if blocking(r, cfg):
                findings.append(Finding(path, n, "P04",
                                        f"cited {r['id']} is {r['read_status']}/verifier {r['verifier_status']}: "
                                        f"not admissible evidence for a claim, decision or score"))
        claim = MEASURED_TAG_RE.sub("", SRC_TAG_RE.sub("", text))
        if verified_rx.search(claim) and not any(r["verifier_status"] == ec["verified_verifier_state"] for r in known):
            findings.append(Finding(path, n, "P02",
                                    "claim labelled verified/validated but no cited source has verifier_status "
                                    + ec["verified_verifier_state"]))
        if known and not MEASURED_TAG_RE.search(text):
            quote_nums = set()
            for r in known:
                quote_nums.update(x.replace(",", "") for x in NUMBER_RE.findall(r["quote"]))
            for num in claim_numbers(claim):
                if num.replace(",", "") not in quote_nums:
                    findings.append(Finding(path, n, "P03",
                                            f"number {num!r} in claim does not appear in any cited row's quote "
                                            f"(cite [measured:...] if it is our own measurement)"))


# ---------------------------------------------------------------- agents
def parse_frontmatter(text):
    m = re.match(r"^---\s*\n(.*?)\n---\s*(\n|$)", text, re.S)
    fm = {}
    if m:
        for ln in m.group(1).splitlines():
            if ":" in ln and not ln.startswith(" "):
                k, v = ln.split(":", 1)
                fm[k.strip()] = v.strip()
    return fm


def check_agents(directory, cfg, findings):
    d = Path(directory)
    if not d.is_dir():
        raise UsageError(f"--check-agents: {directory} is not a directory")
    spawn = cfg_get(cfg, "spawn_tool")
    for name, key in cfg_get(cfg, "agent_files").items():
        f = d / f"{name}.md"
        if not f.exists():
            findings.append(Finding(f, 1, "A01", f"agent file for '{name}' not found"))
            continue
        text = f.read_text(encoding="utf-8")
        fm = parse_frontmatter(text)
        want_model = cfg_get(cfg, f"model_tiers.{key}")
        if fm.get("model") != want_model:
            findings.append(Finding(f, 1, "A01", f"model {fm.get('model')!r} != config model_tiers.{key} {want_model!r}"))
        tools = {t.strip() for t in fm.get("tools", "").split(",") if t.strip()}
        want_tools = set(cfg_get(cfg, f"allowed_tools.{key}"))
        if tools != want_tools:
            findings.append(Finding(f, 1, "A01", f"tools {sorted(tools)} != config allowed_tools.{key} {sorted(want_tools)}"))
        disallowed = {t.strip() for t in fm.get("disallowedTools", "").split(",") if t.strip()}
        for t in cfg_get(cfg, f"required_disallowed_tools.{key}"):
            if t not in disallowed:
                findings.append(Finding(f, 1, "A01", f"disallowedTools should include '{t}' (config required_disallowed_tools.{key})"))
        want_turns = cfg_get(cfg, f"budgets.{key}_max_turns")
        if fm.get("maxTurns") != str(want_turns):
            findings.append(Finding(f, 1, "A01", f"maxTurns {fm.get('maxTurns')!r} != config budgets.{key}_max_turns {want_turns}"))
        # A02: spawn policy, per agent, from config
        lists_spawn = any(t == spawn or t.startswith(spawn + "(") for t in tools)
        if not cfg_get(cfg, f"may_spawn.{key}"):
            if lists_spawn:
                findings.append(Finding(f, 1, "A02", f"leaf agent lists the '{spawn}' tool (config may_spawn.{key} is false)"))
        else:
            for t in cfg_get(cfg, "spawner_forbidden_tools"):
                if t in tools:
                    findings.append(Finding(f, 1, "A02", f"spawner lists forbidden tool '{t}' (config spawner_forbidden_tools)"))
            if not lists_spawn:
                findings.append(Finding(f, 1, "A02", f"spawner does not list the '{spawn}' tool (config may_spawn.{key} is true)"))
            for k in cfg_get(cfg, "spawner_required_body_keys"):
                if k not in text:
                    findings.append(Finding(f, 1, "A02", f"spawner body does not reference config key '{k}'"))


# ---------------------------------------------------------------- legacy
def legacy_report(path, lines, cfg, findings):
    """Count read states and multi-URL rows in a pre-schema file. Never fails on content."""
    scan_secrets(path, lines, cfg, findings)
    col_pats = [re.compile(p, re.I) for p in cfg_get(cfg, "lint.legacy_read_column_patterns")]
    states = [(name, re.compile(p, re.I)) for name, p in cfg_get(cfg, "lint.legacy_state_patterns")]
    url_cols = [re.compile(p, re.I) for p in cfg_get(cfg, "lint.legacy_url_column_patterns")]
    note_state = None  # file-level note such as "All rows are SEARCH-SUMMARY only"
    for text in lines:
        for name, p in cfg_get(cfg, "lint.legacy_file_note_patterns"):
            if re.search(p, text, re.I):
                note_state = name
    counts = {"READ": 0, "SUMMARY": 0, "TITLE-ONLY": 0, "FETCH-FAILED": 0, "UNCLASSIFIED": 0}
    multi, urls, rows = [], {}, 0
    for t in parse_tables(lines):
        idx = next((i for i, h in enumerate(t["header"]) if any(p.search(h) for p in col_pats)), None)
        if idx is None:
            continue
        for lineno, cells in t["rows"]:
            if not any(p.search(h) for h in t["header"] for p in url_cols):
                continue
            rows += 1
            cell = cells[idx] if idx < len(cells) else ""
            state = next((n for n, rx in states if rx.search(cell)), None) or note_state
            key = {"READ-SUMMARIZER": "READ", "READ-VERBATIM": "READ"}.get(state, state) or "UNCLASSIFIED"
            counts[key] = counts.get(key, 0) + 1
            n_urls = len(URL_RE.findall(" ".join(cells)))
            if n_urls > 1:
                multi.append(lineno)
            for u in URL_RE.findall(cells[1] if len(cells) > 1 else ""):
                if u in urls:
                    findings.append(Finding(path, lineno, "W08", f"duplicate URL (also line {urls[u]})", "warning"))
                urls.setdefault(u, lineno)
    summary = {"rows": rows, "read": counts["READ"], "summary": counts["SUMMARY"],
               "title_only": counts["TITLE-ONLY"], "failed": counts["FETCH-FAILED"],
               "unclassified": counts["UNCLASSIFIED"], "multi_url_rows": len(multi)}
    for ln in multi:
        findings.append(Finding(path, ln, "L01", "row bundles more than one URL (cannot tie a quote to one source)", "warning"))
    return summary


# ---------------------------------------------------------------- main
def summarise(rows, plan_ids):
    read = sum(1 for r in rows if r["read_status"].startswith("READ-"))
    lb = {r["id"] for r in rows if r["id"] in plan_ids}
    unverified = sum(1 for r in rows if r["id"] in lb and r["verifier_status"] != "VERIFIED")
    return {"rows": len(rows), "read": read,
            "summary": sum(1 for r in rows if r["read_status"] == "SUMMARY"),
            "title_only": sum(1 for r in rows if r["read_status"] == "TITLE-ONLY"),
            "failed": sum(1 for r in rows if r["read_status"] == "FETCH-FAILED"),
            "load_bearing_unverified": unverified}


def build_parser():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("findings", nargs="*", help="findings markdown files")
    p.add_argument("--config", default=str(DEFAULT_CONFIG))
    p.add_argument("--plan", help="plan markdown whose [src:ID] citations are checked")
    p.add_argument("--check-agents", metavar="DIR", help="check agent definition files against config")
    p.add_argument("--legacy", action="store_true", help="report counts for pre-schema files; does not grade")
    p.add_argument("--strict", action="store_true", help="treat warnings as errors")
    p.add_argument("--json", action="store_true", help="emit JSON")
    p.add_argument("--run", metavar="RESEARCH_ID", help="run-level checks (R01) for this research id; needs --findings-dir")
    p.add_argument("--findings-dir", metavar="DIR", help="directory holding the run's findings files")
    p.add_argument("--profile", help="profile of the --run (default: 'profile' in <DIR>/<RESEARCH_ID>-spec.json)")
    p.add_argument("--fingerprint", action="store_true", help="print the config fingerprint and exit")
    return p


def run(argv):
    args = build_parser().parse_args(argv)
    cfg, fingerprint = load_config(args.config)
    if args.fingerprint:
        print(f"config_version={cfg.get('config_version')} config_sha256={fingerprint}")
        return EXIT_OK, None
    if bool(args.run) != bool(args.findings_dir):
        raise UsageError("--run and --findings-dir must be given together")
    if not args.findings and not args.check_agents and not args.run:
        raise UsageError("give at least one findings file, --run/--findings-dir or --check-agents DIR")
    findings, rows_all, summaries = [], [], {}
    ids_seen = {}
    for f in args.findings:
        lines = read_lines(f)
        if args.legacy:
            summaries[f] = legacy_report(f, lines, cfg, findings)
        else:
            _, rows = check_findings_file(f, lines, cfg, fingerprint, ids_seen, findings)
            rows_all.extend(rows)
    if args.run:
        profile = resolve_run_profile(args.findings_dir, args.run, args.profile)
        run_files, ok = check_run_red_team(args.findings_dir, args.run, profile, cfg, findings)
        print(f"run {args.run}: profile={profile} files={len(run_files)} red_team_file={'yes' if ok else 'no'}",
              file=sys.stderr)
    if args.plan:
        if args.legacy:
            raise UsageError("--plan cannot be combined with --legacy")
        check_plan(args.plan, read_lines(args.plan), cfg, rows_all, findings)
    if args.check_agents:
        check_agents(args.check_agents, cfg, findings)
    if args.strict:
        for x in findings:
            x.severity = "error"
    errors = [x for x in findings if x.severity == "error"]
    code = EXIT_OK
    if any(x.code == "F02" for x in errors):
        code = EXIT_FINGERPRINT
    elif errors:
        code = EXIT_FINDINGS
    plan_ids = set()
    if args.plan:
        for ln in read_lines(args.plan):
            plan_ids.update(i for t in SRC_TAG_RE.findall(ln) for i in split_ids(t))
    payload = {"config_version": cfg.get("config_version"), "config_sha256": fingerprint,
               "findings": [x.as_dict() for x in findings], "exit": code,
               "summary": summaries if args.legacy else summarise(rows_all, plan_ids),
               "note": "form checked, content not"}
    return code, (payload, findings, args)


def main(argv=None):
    try:
        code, extra = run(sys.argv[1:] if argv is None else argv)
    except UsageError as e:
        print(f"check_research_log: usage/IO error: {e}", file=sys.stderr)
        return EXIT_USAGE
    except SystemExit as e:  # argparse
        return EXIT_USAGE if e.code not in (0, None) else EXIT_OK
    if extra is None:
        return code
    payload, findings, args = extra
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        for x in findings:
            print(x.text())
        s = payload["summary"]
        if args.legacy:
            for f, sm in s.items():
                print(f"{f}: " + " ".join(f"{k}={v}" for k, v in sm.items()))
        else:
            print("read={read} summary={summary} title_only={title_only} failed={failed} "
                  "load_bearing_unverified={load_bearing_unverified}".format(**s))
        n_err = sum(1 for x in findings if x.severity == "error")
        print(f"config_version={payload['config_version']} config_sha256={payload['config_sha256']} "
              f"errors={n_err} warnings={len(findings) - n_err} exit={code} (form checked, content not)")
    return code


if __name__ == "__main__":
    sys.exit(main())
