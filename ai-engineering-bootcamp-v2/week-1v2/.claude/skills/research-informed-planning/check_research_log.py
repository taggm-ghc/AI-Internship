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
    # F11 verdicts
    check_verdicts(path, verdict_table, subclaims, rows, cfg, findings, hdr_line)
    return hdr, rows


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
            for num in NUMBER_RE.findall(claim):
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
    p.add_argument("--fingerprint", action="store_true", help="print the config fingerprint and exit")
    return p


def run(argv):
    args = build_parser().parse_args(argv)
    cfg, fingerprint = load_config(args.config)
    if args.fingerprint:
        print(f"config_version={cfg.get('config_version')} config_sha256={fingerprint}")
        return EXIT_OK, None
    if not args.findings and not args.check_agents:
        raise UsageError("give at least one findings file or --check-agents DIR")
    findings, rows_all, summaries = [], [], {}
    ids_seen = {}
    for f in args.findings:
        lines = read_lines(f)
        if args.legacy:
            summaries[f] = legacy_report(f, lines, cfg, findings)
        else:
            _, rows = check_findings_file(f, lines, cfg, fingerprint, ids_seen, findings)
            rows_all.extend(rows)
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
