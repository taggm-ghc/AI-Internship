"""Tests for check_research_log.py. Synthetic inputs only; no network.
The script is run as a subprocess (the real code path) as well as imported."""
import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).parent
SCRIPT = HERE / "check_research_log.py"
CONFIG = HERE / "research_config.json"
sys.path.insert(0, str(HERE))
import check_research_log as crl  # noqa: E402

COLS = "| " + " | ".join(crl.SOURCE_COLUMNS) + " |"
SEP = "|" + "---|" * len(crl.SOURCE_COLUMNS)


def fingerprint(path=CONFIG):
    return crl.load_config(path)[1]


def header(sha, **over):
    h = {"research_id": "RH", "plan_ref": "p.md", "role": "researcher", "angle": "against",
         "model": "sonnet", "parent": "main", "depth": 1, "config_version": "1",
         "config_sha256": sha, "budget_limit": 4, "budget_used": 4, "date": "2026-10-02",
         "subclaims": ["S1"]}
    h.update(over)
    return "<!-- research-header " + json.dumps(h) + " -->"


def row(id="RH-R-S1", url="https://arxiv.org/abs/2310.01798", title="T", org="Org", year="2023",
        portal="arXiv", rel="high", read="READ-VERBATIM", quote='"short quote"', sub="S1",
        stance="against", strength="measured", ver="-"):
    return "| " + " | ".join([id, url, title, org, year, portal, rel, read, quote, sub, stance, strength, ver]) + " |"


def doc(sha, rows=None, hdr=None, verdicts=None, gaps="", cols=COLS):
    rows = rows if rows is not None else [row()]
    verdicts = verdicts if verdicts is not None else ["| S1 | survived | RH-R-S1 | - |"]
    parts = [hdr or header(sha), "", "## Sources", cols, SEP, *rows, "", "## Gaps", gaps, "",
             "## Verdicts", "| sub_claim | verdict | supporting ids | antagonistic ids |", "|---|---|---|---|", *verdicts]
    return "\n".join(parts) + "\n"


def run(args, tmp=None):
    p = subprocess.run([sys.executable, str(SCRIPT), *map(str, args)], capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def write(tmp_path, name, text):
    f = tmp_path / name
    f.write_text(text, encoding="utf-8")
    return f


@pytest.fixture
def sha():
    return fingerprint()


def codes(out):
    return sorted({ln.split(": ", 1)[1].split()[0] for ln in out.splitlines() if ": " in ln and ln.count(":") >= 2
                   and ln.split(": ", 1)[1][:1] in "FPWSAL"})


# ---------------------------------------------------------------- good input
def test_valid_file_exit_0(tmp_path, sha):
    f = write(tmp_path, "good.md", doc(sha))
    rc, out, err = run([f])
    assert rc == 0, out
    assert "config_sha256=" + sha in out


def test_fingerprint_flag_prints_hash(sha):
    rc, out, _ = run(["--fingerprint"])
    assert rc == 0 and sha in out


def test_fingerprint_is_hash_of_canonical_json(sha):
    import hashlib
    cfg = json.loads(CONFIG.read_text())
    assert sha == hashlib.sha256(json.dumps(cfg, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


def test_json_output(tmp_path, sha):
    f = write(tmp_path, "good.md", doc(sha))
    rc, out, _ = run([f, "--json"])
    data = json.loads(out)
    assert rc == 0 and data["exit"] == 0 and data["config_sha256"] == sha and data["summary"]["rows"] == 1


# ---------------------------------------------------------------- one bad file per rule
BAD = {
    "F01": lambda sha: doc(sha).replace(header(sha) + "\n", "# no header\n"),
    "F03": lambda sha: doc(sha, cols=COLS.replace("title", "name")),
    "F04": lambda sha: doc(sha, [row(url="https://a.example/x ; https://b.example/y")]),
    "F04b": lambda sha: doc(sha, [row(url="not a url")]),
    "F05": lambda sha: doc(sha, [row(read="READ")]),
    "F05b": lambda sha: doc(sha, [row(sub="S9")]),
    "F06a": lambda sha: doc(sha, [row(quote='"' + " ".join(["w"] * 26) + '"')]),
    "F06b": lambda sha: doc(sha, [row(read="READ-SUMMARIZER", quote='"no tilde"')]),
    "F06c": lambda sha: doc(sha, [row(read="READ-VERBATIM", quote="-")]),
    "F07": lambda sha: doc(sha, [row(rel="low")]),
    "F09": lambda sha: doc(sha, hdr=header(sha, budget_used=9)),
    "F10": lambda sha: doc(sha, [row(stance="for")]),
    "F11a": lambda sha: doc(sha, verdicts=[]),
    "F11b": lambda sha: doc(sha, verdicts=["| S1 | survived | RH-R-S99 | - |"]),
    "F11c": lambda sha: doc(sha, [row(read="SUMMARY", quote="-")]),
    "F13": lambda sha: doc(sha, [row(), row()]),
    "S01": lambda sha: doc(sha, gaps="api_key = abcdefghijklmnop1234"),
}


@pytest.mark.parametrize("key", sorted(BAD))
def test_bad_file_gives_expected_code(tmp_path, sha, key):
    f = write(tmp_path, "bad.md", BAD[key](sha))
    rc, out, _ = run([f])
    want = key[:3]
    assert rc == 1, out
    assert want in codes(out), out
    assert "bad.md:" in out  # path:line: CODE format
    assert any(ln.split(":")[1].isdigit() for ln in out.splitlines() if ln.startswith(str(f)))


def test_secret_value_never_printed(tmp_path, sha):
    secret = "abcdefghijklmnop1234"
    f = write(tmp_path, "s.md", doc(sha, gaps=f"token = {secret}"))
    rc, out, err = run([f])
    assert rc == 1 and "S01" in out and secret not in out + err
    rc, out, err = run([f, "--json"])
    assert secret not in out + err


def test_fingerprint_mismatch_exit_3(tmp_path, sha):
    f = write(tmp_path, "old.md", doc("0" * 64))
    rc, out, _ = run([f])
    assert rc == 3 and "F02" in out


def test_config_value_change_gives_exit_3_on_old_findings(tmp_path, sha):
    f = write(tmp_path, "g.md", doc(sha))
    cfg = json.loads(CONFIG.read_text())
    cfg["quote_max_words"] = 10
    c2 = write(tmp_path, "c2.json", json.dumps(cfg))
    rc, out, _ = run([f, "--config", c2])
    assert rc == 3


def test_quote_limit_comes_from_config(tmp_path):
    cfg = json.loads(CONFIG.read_text())
    cfg["quote_max_words"] = 2
    c2 = write(tmp_path, "c2.json", json.dumps(cfg))
    sha2 = fingerprint(c2)
    f = write(tmp_path, "q.md", doc(sha2, [row(quote='"one two three"')]))
    rc, out, _ = run([f, "--config", c2])
    assert rc == 1 and "limit is 2" in out


def test_reliability_override_with_reason_ok_and_too_far_rejected(tmp_path, sha):
    ok = write(tmp_path, "ok.md", doc(sha, [row(rel="medium (override: preprint, unreviewed)")]))
    assert run([ok])[0] == 0
    far = write(tmp_path, "far.md", doc(sha, [row(url="https://example.org/x", rel="high (override: because)")]))
    rc, out, _ = run([far])
    assert rc == 1 and "F07" in out


def test_w08_duplicate_url_is_warning_and_strict_makes_it_error(tmp_path, sha):
    rows = [row(), row(id="RH-R-S2")]
    f = write(tmp_path, "d.md", doc(sha, rows))
    rc, out, _ = run([f])
    assert rc == 0 and "W08" in out
    rc, out, _ = run([f, "--strict"])
    assert rc == 1


def test_w12_arxiv_year_mismatch_is_warning(tmp_path, sha):
    f = write(tmp_path, "y.md", doc(sha, [row(url="https://arxiv.org/abs/2606.11217", year="2025")]))
    rc, out, _ = run([f])
    assert rc == 0 and "W12" in out


def test_arxiv_url_without_id_rejected(tmp_path, sha):
    f = write(tmp_path, "a.md", doc(sha, [row(url="https://arxiv.org/list/cs.AI/recent")]))
    rc, out, _ = run([f])
    assert rc == 1 and "F04" in out


def test_gaps_line_satisfies_f10(tmp_path, sha):
    f = write(tmp_path, "g.md", doc(sha, [row(stance="for")], gaps="S1: no evidence found within budget"))
    assert run([f])[0] == 0


def test_duplicate_ids_across_files(tmp_path, sha):
    a = write(tmp_path, "a.md", doc(sha))
    b = write(tmp_path, "b.md", doc(sha))
    rc, out, _ = run([a, b])
    assert rc == 1 and "F13" in out


# ---------------------------------------------------------------- plan mode
def plan(tmp_path, text):
    return write(tmp_path, "plan.md", text)


def test_plan_clean(tmp_path, sha):
    f = write(tmp_path, "f.md", doc(sha, [row(quote='"31% of pushes"', ver="VERIFIED")]))
    p = plan(tmp_path, "Config pushes were 31% of triggers (verified) [src:RH-R-S1]\n")
    rc, out, _ = run([f, "--plan", p])
    assert rc == 0, out


@pytest.mark.parametrize("text,code", [
    ("A claim [src:RH-R-S9]", "P01"),
    ("This is verified [src:RH-R-S1]", "P02"),
    ("Config caused 70% of outages [src:RH-R-S1]", "P03"),
])
def test_plan_rules(tmp_path, sha, text, code):
    f = write(tmp_path, "f.md", doc(sha, [row(quote='"31% of pushes"')]))
    rc, out, _ = run([f, "--plan", plan(tmp_path, text + "\n")])
    assert rc == 1 and code in out, out


def test_plan_cites_title_only_p04(tmp_path, sha):
    f = write(tmp_path, "f.md", doc(sha, [row(read="TITLE-ONLY", quote="-")], verdicts=["| S1 | qualified | - | RH-R-S1 |"]))
    rc, out, _ = run([f, "--plan", plan(tmp_path, "A decision [src:RH-R-S1]\n")])
    assert rc == 1 and "P04" in out


def test_plan_measured_tag_skips_number_check(tmp_path, sha):
    f = write(tmp_path, "f.md", doc(sha))
    rc, out, _ = run([f, "--plan", plan(tmp_path, "We saw 12 hits [measured:run7] [src:RH-R-S1]\n")])
    assert rc == 0, out


# ---------------------------------------------------------------- config / usage
def test_invalid_config_exit_2(tmp_path, sha):
    c = write(tmp_path, "bad.json", "{not json")
    f = write(tmp_path, "g.md", doc(sha))
    assert run([f, "--config", c])[0] == 2


def test_missing_file_exit_2(tmp_path):
    assert run([tmp_path / "nope.md"])[0] == 2


def test_no_args_exit_2():
    assert run([])[0] == 2


def test_config_missing_key_exit_2(tmp_path, sha):
    cfg = json.loads(CONFIG.read_text())
    del cfg["quote_max_words"]
    c = write(tmp_path, "c.json", json.dumps(cfg))
    f = write(tmp_path, "g.md", doc(fingerprint(c)))
    assert run([f, "--config", c])[0] == 2


def test_real_config_has_no_secret_looking_values():
    rx = [__import__("re").compile(p) for p in json.loads(CONFIG.read_text())["secret_patterns"].values()]
    # the patterns themselves are config; the config must not trip its own scanner on non-pattern lines
    lines = [ln for ln in CONFIG.read_text().splitlines() if '"secret_patterns"' not in ln and "-----BEGIN" not in ln
             and "\\\\" not in ln]
    assert not [ln for ln in lines if any(r.search(ln) for r in rx)]


# ---------------------------------------------------------------- agents
def agent(tmp_path, name, model="opus", tools="Read", extra="disallowedTools: Agent\nmaxTurns: 25\n", body="body"):
    (tmp_path / f"{name}.md").write_text(f"---\nname: {name}\ndescription: d\ntools: {tools}\nmodel: {model}\n{extra}---\n{body}\n")


def make_agents(tmp_path, **overrides):
    cfg = json.loads(CONFIG.read_text())
    for name, key in cfg["agent_files"].items():
        kw = {"model": cfg["model_tiers"][key], "tools": ", ".join(cfg["allowed_tools"][key])}
        if cfg["may_spawn"][key]:
            kw["extra"] = "maxTurns: 25\n"
            kw["body"] = " ".join(cfg["spawner_required_body_keys"])
        kw.update(overrides.get(name, {}))
        agent(tmp_path, name, **kw)


def test_agents_ok(tmp_path):
    make_agents(tmp_path)
    rc, out, _ = run(["--check-agents", tmp_path])
    assert rc == 0, out


def test_agents_model_mismatch_and_leaf_with_agent_tool(tmp_path):
    make_agents(tmp_path, researcher={"model": "opus"},
                synthesizer={"tools": "Read, Agent"})
    rc, out, _ = run(["--check-agents", tmp_path])
    assert rc == 1 and "A01" in out and "A02" in out and "leaf agent lists" in out


def test_agents_leaf_must_disallow_agent(tmp_path):
    make_agents(tmp_path, synthesizer={"extra": "maxTurns: 25\n"})
    rc, out, _ = run(["--check-agents", tmp_path])
    assert rc == 1 and "disallowedTools should include 'Agent'" in out


def test_agents_tools_must_match_config(tmp_path):
    make_agents(tmp_path, synthesizer={"tools": "Read, Grep, Glob"})
    rc, out, _ = run(["--check-agents", tmp_path])
    assert rc == 1 and "allowed_tools.synthesizer" in out


def test_agents_spawner_ok_has_agent_tool(tmp_path):
    make_agents(tmp_path)
    assert "Agent" in (tmp_path / "researcher.md").read_text().split("---")[1]
    rc, out, _ = run(["--check-agents", tmp_path])
    assert rc == 0, out


def test_agents_spawner_forbidden_tool(tmp_path):
    cfg = json.loads(CONFIG.read_text())
    tools = ", ".join(cfg["allowed_tools"]["researcher"] + ["Write"])
    make_agents(tmp_path, researcher={"tools": tools})
    rc, out, _ = run(["--check-agents", tmp_path])
    assert rc == 1 and "forbidden tool 'Write'" in out


def test_agents_spawner_missing_body_keys(tmp_path):
    make_agents(tmp_path, researcher={"body": "no keys here"})
    rc, out, _ = run(["--check-agents", tmp_path])
    assert rc == 1 and "recursion.max_depth" in out and "A02" in out


def test_agents_spawner_without_agent_tool(tmp_path):
    cfg = json.loads(CONFIG.read_text())
    tools = ", ".join(t for t in cfg["allowed_tools"]["researcher"] if t != "Agent")
    make_agents(tmp_path, researcher={"tools": tools})
    rc, out, _ = run(["--check-agents", tmp_path])
    assert rc == 1 and "does not list" in out


# ---------------------------------------------------------------- legacy
def test_legacy_counts_and_never_fails_on_content(tmp_path):
    t = tmp_path / "old.md"
    t.write_text("| # | URL | Title | Read/Search |\n|---|---|---|---|\n"
                 "| 1 | https://a.example/x | A | FETCHED (read, via summarizer) |\n"
                 "| 2 | https://b.example/x ; https://c.example/y | B | SEARCH SUMMARY only |\n"
                 "| 3 | https://d.example/x | C | TITLE ONLY |\n"
                 "| 4 | https://e.example/x | D | FETCH FAILED |\n")
    rc, out, _ = run([t, "--legacy"])
    assert rc == 0
    assert "read=1" in out and "summary=1" in out and "title_only=1" in out and "failed=1" in out and "multi_url_rows=1" in out


def test_legacy_still_flags_secrets(tmp_path):
    t = tmp_path / "old.md"
    t.write_text("| # | URL | Read |\n|---|---|---|\n| 1 | https://a.example/x | READ |\n\npassword = hunter2hunter2hunter2\n")
    rc, out, _ = run([t, "--legacy"])
    assert rc == 1 and "S01" in out and "hunter2" not in out


def test_strict_on_legacy_file_fails_f01(tmp_path):
    t = tmp_path / "old.md"
    t.write_text("# old\n| # | URL | Read |\n|---|---|---|\n| 1 | https://a.example/x | READ |\n")
    rc, out, _ = run([t])
    assert rc == 1 and "F01" in out


def test_import_level_check_matches_subprocess(tmp_path, sha):
    f = write(tmp_path, "good.md", doc(sha))
    assert crl.main([str(f)]) == 0


def test_arxiv_help_page_needs_no_id_but_other_paths_still_do(tmp_path, sha):
    ok = write(tmp_path, "h.md", doc(sha, [row(url="https://info.arxiv.org/help/api/tou.html", year="2023")]))
    rc, out, _ = run([ok])
    assert "no valid arXiv id" not in out
    bad = write(tmp_path, "b.md", doc(sha, [row(url="https://info.arxiv.org/other/page.html")]))
    rc2, out2, _ = run([bad])
    assert rc2 == 1 and "F04" in out2


def test_second_source_table_is_error_and_its_rows_are_validated(tmp_path, sha):
    # Job 16 (item 4): a VERIFIED row hidden in a second table is subject to the same rules.
    bad = row(id="RH-R-S2", read="READ-VERBATIM", quote="-", ver="VERIFIED")
    text = doc(sha) + "\n## More\n" + COLS + "\n" + SEP + "\n" + bad + "\n"
    f = write(tmp_path, "two.md", text)
    rc, out, _ = run([f])
    assert rc == 1
    assert "more than one source table" in out
    assert "F06" in codes(out)  # the second table's row was validated (READ-VERBATIM with no quote)


def test_second_table_bad_id_pattern_flagged(tmp_path, sha):
    text = doc(sha) + "\n" + COLS + "\n" + SEP + "\n" + row(id="WRONG-R-S1", ver="VERIFIED") + "\n"
    rc, out, _ = run([write(tmp_path, "two.md", text)])
    assert rc == 1 and "F13" in codes(out)


def test_header_date_with_trailing_newline_rejected(tmp_path, sha):
    f = write(tmp_path, "d.md", doc(sha, hdr=header(sha, date="2026-10-02\n")))
    rc, out, _ = run([f])
    assert rc == 1 and "header date" in out


def test_patterns_are_full_match_not_prefix(tmp_path, sha):
    # a trailing newline or trailing text after a valid-looking value must not pass
    for r in (row(id="RH-R-S1x"), row(year="2023x"), row(rel="high (override: why)\n x")):
        rc, out, _ = run([write(tmp_path, "p.md", doc(sha, [r]))])
        assert rc == 1, out
    assert crl.re.fullmatch(r"^[0-9]{4}$", "2023\n") is None


# ---------------------------------------------------------------- v5 rules (#95): F12, F14, R01
GOOD_GAP = '- S1: searched `mttr critique` on 2026-10-09, searched-none-found'


def v5doc(sha, rows, gaps="", cv="5", role="adversarial-researcher", angle="against"):
    verdicts = ["| S1 | qualified | - | RH-A-S1 |"]
    return doc(sha, rows=rows, gaps=gaps, verdicts=verdicts,
               hdr=header(sha, role=role, angle=angle, config_version=cv))


def test_f12_fulltext_antagonistic_row_passes(tmp_path, sha):
    f = write(tmp_path, "ok.md", v5doc(sha, [row(id="RH-A-S1", read="READ-VERBATIM")]))
    rc, out, _ = run([f])
    assert rc == 0, out


def test_f12_summary_only_antagonistic_fails(tmp_path, sha):
    r = row(id="RH-A-S1", read="SUMMARY", quote='~"paraphrase"', url="https://example.com/a", rel="low")
    f = write(tmp_path, "bad.md", v5doc(sha, [r], gaps="- BUDGET REACHED 4/4."))
    rc, out, _ = run([f])
    assert rc == 1 and "F12" in out, out


def test_f12_dated_gap_line_satisfies(tmp_path, sha):
    r = row(id="RH-A-S1", read="SUMMARY", quote='~"paraphrase"', url="https://example.com/a", rel="low")
    f = write(tmp_path, "gap.md", v5doc(sha, [r], gaps=GOOD_GAP))
    rc, out, _ = run([f])
    assert rc == 0, out


def test_f12_supporting_fulltext_row_does_not_count(tmp_path, sha):
    r = row(id="RH-A-S1", read="READ-VERBATIM", stance="for")
    f = write(tmp_path, "for.md", v5doc(sha, [r]))
    rc, out, _ = run([f])
    assert "F12" in out and "F10" in out


@pytest.mark.parametrize("gap,missing", [
    ("- S1: no evidence found within budget", "query"),
    ('- S1: searched `x` , searched-none-found', "date"),
    ('- S1: searched `x` on 2026-10-09', "reason"),
    ('- S1: searched `x` on 2026-10-09, because', "reason"),
])
def test_f14_malformed_gap_line(tmp_path, sha, gap, missing):
    r = row(id="RH-A-S1", read="READ-VERBATIM")
    f = write(tmp_path, "g.md", v5doc(sha, [r], gaps=gap))
    rc, out, _ = run([f])
    assert rc == 1 and "F14" in out and missing in out, out


def test_f14_quoted_query_and_budget_exhausted_ok(tmp_path, sha):
    r = row(id="RH-A-S1", read="READ-VERBATIM")
    gap = '- S1: query "mttr critique" 2026-10-09 budget-exhausted'
    f = write(tmp_path, "g.md", v5doc(sha, [r], gaps=gap))
    assert run([f])[0] == 0


def test_f14_unlabelled_gap_lines_are_not_checked(tmp_path, sha):
    r = row(id="RH-A-S1", read="READ-VERBATIM")
    f = write(tmp_path, "g.md", v5doc(sha, [r], gaps="- BUDGET REACHED 4/4. Unknown: S1 evidence"))
    assert run([f])[0] == 0


def test_v4_files_unaffected_by_v5_rules(tmp_path, sha):
    r = row(id="RH-A-S1", read="SUMMARY", quote='~"paraphrase"', url="https://example.com/a", rel="low")
    f = write(tmp_path, "v4.md", v5doc(sha, [r], gaps="- S1: no evidence found within budget", cv="4"))
    rc, out, _ = run([f])
    assert rc == 0 and "F12" not in out and "F14" not in out, out


def test_v5_rules_skip_researcher_files(tmp_path, sha):
    r = row(id="RH-R-S1", read="SUMMARY", quote='~"paraphrase"', url="https://example.com/a", rel="low")
    f = write(tmp_path, "r.md", v5doc(sha, [r], gaps="- S1: no evidence", role="researcher", angle="support"))
    rc, out, _ = run([f])
    assert "F12" not in out and "F14" not in out


def test_v5_rules_skipped_when_config_lacks_keys(tmp_path):
    cfg = json.loads(CONFIG.read_text())
    for k in ("min_fulltext_antagonistic_per_subclaim", "gap_reasons"):
        del cfg[k]
    cp = tmp_path / "old.json"
    cp.write_text(json.dumps(cfg))
    fp = crl.load_config(cp)[1]
    r = row(id="RH-A-S1", read="SUMMARY", quote='~"paraphrase"', url="https://example.com/a", rel="low")
    f = write(tmp_path, "x.md", v5doc(fp, [r], gaps="- S1: nothing"))
    rc, out, _ = run([f, "--config", cp])
    assert "F12" not in out and "F14" not in out, out


def test_real_config_is_v6_and_v5_snapshot_has_new_keys():
    cfg = crl.load_config(CONFIG)[0]
    assert cfg["config_version"] == "6"
    v5 = json.loads((HERE / "research_config.v5.json").read_text())
    assert v5["config_version"] == "5"
    for k in ("min_fulltext_antagonistic_per_subclaim", "gaps_require_dated_queries", "red_team_file_required_for_profile", "gap_reasons"):
        assert k in v5 and k in cfg, k
    assert cfg["min_fulltext_antagonistic_per_subclaim"] == 1
    assert cfg["gaps_require_dated_queries"] is True
    assert cfg["red_team_file_required_for_profile"] == ["standard"]
    v4 = json.loads((HERE / "research_config.v4.json").read_text())
    assert v4["config_version"] == "4" and "gap_reasons" not in v4


def _run_dir(tmp_path, with_t, profile="standard"):
    d = tmp_path / "scratch"
    d.mkdir()
    s = fingerprint()
    (d / "RH-spec.json").write_text(json.dumps({"profile": profile}))
    (d / "RH-researcher-1.md").write_text(doc(s), encoding="utf-8")
    if with_t:
        (d / "RH-redteam-1.md").write_text(
            doc(s, rows=[row(id="RH-Ta-S1")], hdr=header(s, role="adversarial-researcher", angle="red_team")),
            encoding="utf-8")
    return d


def test_r01_standard_run_without_t_file_fails(tmp_path):
    d = _run_dir(tmp_path, with_t=False)
    rc, out, err = run(["--run", "RH", "--findings-dir", d])
    assert rc == 1 and "R01" in out and "red_team_file=no" in err, (out, err)


def test_r01_standard_run_with_t_file_passes(tmp_path):
    d = _run_dir(tmp_path, with_t=True)
    rc, out, err = run(["--run", "RH", "--findings-dir", d])
    assert rc == 0 and "red_team_file=yes" in err, (out, err)


def test_r01_rapid_profile_not_required(tmp_path):
    d = _run_dir(tmp_path, with_t=False, profile="rapid")
    assert run(["--run", "RH", "--findings-dir", d])[0] == 0


def test_r01_profile_flag_overrides_and_unknown_profile_is_usage_error(tmp_path):
    d = _run_dir(tmp_path, with_t=False, profile="rapid")
    assert run(["--run", "RH", "--findings-dir", d, "--profile", "standard"])[0] == 1
    (d / "RH-spec.json").unlink()
    rc, _, err = run(["--run", "RH", "--findings-dir", d])
    assert rc == 2 and "profile unknown" in err


def test_run_without_findings_dir_is_usage_error(tmp_path):
    assert run(["--run", "RH"])[0] == 2


def test_latest_version_prefers_highest_vN(tmp_path):
    for n in ("a.md", "a.md.v1", "a.md.v2", "a.md.v10", "b.md"):
        (tmp_path / n).write_text("x")
    assert crl.latest_version(tmp_path / "a.md").name == "a.md.v10"
    assert crl.latest_version(tmp_path / "b.md").name == "b.md"


# ---------------------------------------------------------------- #96 (b): F11 sites, L01, F06 contradiction
def test_f11_each_emit_site_by_message(tmp_path, sha):
    cases = {
        "no '## Verdicts' table": doc(sha).split("## Verdicts")[0],
        "unknown sub-claim": doc(sha, verdicts=["| S1 | survived | RH-R-S1 | - |", "| S7 | qualified | - | - |"]),
        "not in ['survived'": doc(sha, verdicts=["| S1 | maybe | RH-R-S1 | - |"]),
        "not in the source table": doc(sha, verdicts=["| S1 | survived | RH-R-S99 | - |"]),  # S99 = deliberate unknown id
        "rests only on": doc(sha, [row(read="SUMMARY", quote="-")]),
        "cites no supporting": doc(sha, verdicts=["| S1 | survived | - | - |"]),
        "has no verdict row": doc(sha, verdicts=["| S9 | qualified | - | - |"]),
        "expected 4": doc(sha, verdicts=["| S1 | survived |"]),
    }
    for needle, text in cases.items():
        f = write(tmp_path, "v.md", text)
        rc, out, _ = run([f])
        assert rc == 1 and "F11" in out and needle in out, (needle, out)


def test_l01_multi_url_row_warns_and_never_fails(tmp_path):
    t = tmp_path / "old.md"
    t.write_text("| # | URL | Title | Read/Search |\n|---|---|---|---|\n"
                 "| 2 | https://b.example/x ; https://c.example/y | B | SEARCH SUMMARY only |\n")
    rc, out, _ = run([t, "--legacy"])
    assert rc == 0 and "L01 (warning)" in out
    rc, out, _ = run([t, "--legacy", "--strict"])
    assert rc == 1 and "L01" in out


def test_f06_verbatim_row_with_tilde_quote_is_contradiction(tmp_path, sha):
    f = write(tmp_path, "q.md", doc(sha, [row(read="READ-VERBATIM", quote='~"paraphrase"')]))
    rc, out, _ = run([f])
    assert rc == 1 and "F06" in out and "must not carry" in out


def test_f06_quote_marks_without_tilde_on_nonverbatim_row(tmp_path, sha):
    f = write(tmp_path, "q.md", doc(sha, [row(read="READ-SUMMARIZER", quote='"x"')]))
    rc, out, _ = run([f])
    assert rc == 1 and "F06" in out and "must start with" in out


# ---------------------------------------------------------------- #96 (b): v6 form rules (staged config)
STAGED = HERE / "research_config.v6-staged.json"


def v6(sha6, name="RH-researcher-1.md", ids=("RH-R-S1",), role="researcher", angle="support", cv="6", **hover):
    rows = [row(id=i, stance="against") for i in ids]
    verdicts = ["| S1 | qualified | - | " + ids[0] + " |"]
    return name, doc(sha6, rows, verdicts=verdicts,
                     hdr=header(sha6, role=role, angle=angle, config_version=cv, **hover))


def run6(tmp_path, name, text):
    f = write(tmp_path, name, text)
    return run([f, "--config", STAGED])


def sha6():
    return fingerprint(STAGED)


def test_staged_config_is_v6_and_live_untouched():
    cfg = crl.load_config(STAGED)[0]
    assert cfg["config_version"] == "6" and "children" in cfg["lint"]["header_optional_keys"]
    live = crl.load_config(CONFIG)[0]
    assert live["config_version"] in ("5", "6")


def test_v6_clean_files_pass_for_each_role_kind(tmp_path):
    s = sha6()
    for args in [
        dict(),
        dict(name="RH-adversarial-1.md", ids=("RH-A-S1",), role="adversarial-researcher", angle="against"),
        dict(name="RH-redteam-1.md", ids=("RH-Ta-S1",), role="adversarial-researcher", angle="red_team"),
        dict(name="RH-redteam-1.md", ids=("RH-Ta-S1",), role="redteam-researcher", angle="red_team"),
    ]:
        name, text = v6(s, **args)
        rc, out, _ = run6(tmp_path, name, text)
        assert rc == 0, (args, out)


def test_f15_unknown_header_key_error_but_children_declared(tmp_path):
    s = sha6()
    name, text = v6(s, children=0)
    rc, out, _ = run6(tmp_path, name, text)
    assert rc == 0, out
    name, text = v6(s, bogus=1)
    rc, out, _ = run6(tmp_path, name, text)
    assert rc == 1 and "F15" in out and "bogus" in out


def test_f15_f16_not_applied_to_v5_file_header(tmp_path):
    s = sha6()
    name, text = v6(s, cv="5", bogus=1, role="adversarial-researcher", angle="red_team")
    rc, out, _ = run6(tmp_path, name, text)  # researcher file name + id, wrong role: ignored for v5 header
    assert "F15" not in out and "F16" not in out, out


def test_f15_f16_not_applied_when_config_is_older(tmp_path):
    cfg = json.loads(STAGED.read_text())
    cfg["config_version"] = "5"
    cp = write(tmp_path, "c5.json", json.dumps(cfg))
    s = fingerprint(cp)
    name, text = v6(s, bogus=1, role="adversarial-researcher")
    f = write(tmp_path, name, text)
    rc, out, _ = run([f, "--config", cp])
    assert "F15" not in out and "F16" not in out, out


@pytest.mark.parametrize("kw,needle", [
    (dict(role="adversarial-researcher"), "file name kind 'researcher'"),                # researcher file, A role
    (dict(name="RH-redteam-1.md", ids=("RH-Ta-S1",), role="researcher", angle="red_team"), "expects header role"),
    (dict(name="RH-redteam-1.md", ids=("RH-Ta-S1",), role="adversarial-researcher", angle="against"), "angle"),
    (dict(ids=("RH-Ta-S1",)), "source id role code 'T'"),                                # T id in researcher file
    (dict(name="RH-adversarial-1.md", ids=("RH-R-S1",), role="adversarial-researcher", angle="against"), "role code 'R'"),
    (dict(name="RH-researcher-1.md", ids=("RH-R-S1", "RH-A-S2")), "mix role codes"),
])
def test_f16_role_consistency(tmp_path, kw, needle):
    s = sha6()
    name, text = v6(s, **kw)
    rc, out, _ = run6(tmp_path, name, text)
    assert rc == 1 and "F16" in out and needle in out, out


# ---------------------------------------------------------------- #96 (a): context-aware P03 numbers
def _numbers(text):
    return crl.claim_numbers(text)


@pytest.mark.parametrize("text", [
    "Follow R1-1 and D-028 here", "H3a, W2b and S2 are labels", "see CWE-209 and gpt-4.1", "per section 7.2 and §3",
    "Sections 3 and 4 apply", "dated 2026-10-09 in 2026", "run `pytest -k 12345`", "see [x](https://a.example/p/4567)",
    "due by 10-07", "(1) first, (2) second", "retry on 4xx and 5xx", "RFC 7231 and ISO 31000", "Use 30 questions [design: eval size]",
    "1. Item text",
])
def test_p03_context_exempt(text):
    assert _numbers(text) == [], text


@pytest.mark.parametrize("text,want", [
    ("72% of pushes", ["72"]), ("we use 30 questions", ["30"]), ("4.99M tokens", ["4.99"]),
    ("2000 questions were asked", ["2000"]), ("took 2026ms", ["2026"]), ("Table 3 shows 31% in S2", ["31"]),
    ("R1-1 gives 27% and 12 hits", ["27", "12"]), ("top-5%", ["5"]),
    ("Use 30 questions [design: n]. But 72% held", ["72"]),
])
def test_p03_real_numbers_still_flagged(text, want):
    assert _numbers(text) == want, text


def test_p03_plan_mode_end_to_end(tmp_path, sha):
    f = write(tmp_path, "f.md", doc(sha, [row(quote='"31% of pushes"')]))
    ok = plan(tmp_path, "Per D-028 and section 2 (2026-10-09) pushes were 31% [src:RH-R-S1]\n")
    assert run([f, "--plan", ok])[0] == 0
    bad = plan(tmp_path, "Per D-028 and section 2 pushes were 13% [src:RH-R-S1]\n")
    rc, out, _ = run([f, "--plan", bad])
    assert rc == 1 and "P03" in out and "'13'" in out
    des = plan(tmp_path, "Use 30 questions [design: eval size] [src:RH-R-S1]\n")
    assert run([f, "--plan", des])[0] == 0


# ---------------------------------------------------------------- PR-96-01 C1: F17 numeric rows need a full-text read
def _f17_doc(s, read, quote, strength, cv="6"):
    r = row(id="RH-R-S1", read=read, quote=quote, strength=strength, stance="against")
    return "RH-researcher-1.md", doc(s, [r], verdicts=["| S1 | qualified | - | RH-R-S1 |"],
                                     hdr=header(s, role="researcher", angle="support", config_version=cv))


@pytest.mark.parametrize("read,quote,strength,fails", [
    ("SUMMARY", '~"Of 259 studies, 13 disseminated practices"', "survey", True),       # number in snippet row
    ("SUMMARY", '"short quote"', "measured", True),                                    # measured in snippet row
    ("TITLE-ONLY", '~"72% improved"', "survey", True),
    ("FETCH-FAILED", '~"4.99M users"', "survey", True),
    ("READ-SUMMARIZER", '~"Of 259 studies, 13 disseminated"', "measured", False),     # full-text read ok
    ("READ-VERBATIM", '"Of 259 studies"', "measured", False),
    ("SUMMARY", '~"published in 2023 by the group"', "survey", False),                 # bare year only
    ("SUMMARY", '~"see RFC 7231 and D-028 and R1-1"', "survey", False),               # ids/labels not counted
    ("SUMMARY", '~"no figures here"', "survey", False),
])
def test_f17_numeric_or_measured_rows_need_fulltext(tmp_path, read, quote, strength, fails):
    s = sha6()
    name, text = _f17_doc(s, read, quote, strength)
    rc, out, _ = run6(tmp_path, name, text)
    assert ("F17" in out) is fails, out
    if fails:
        assert rc == 1 and "fetch the page" in out and "RH-R-S1" in out


def test_f17_not_applied_to_v5_header_or_v5_config(tmp_path):
    s = sha6()
    name, text = _f17_doc(s, "SUMMARY", '~"Of 259 studies"', "measured", cv="5")
    rc, out, _ = run6(tmp_path, name, text)
    assert "F17" not in out, out
    cfg = json.loads(STAGED.read_text())
    cfg["config_version"] = "5"
    cp = write(tmp_path, "c5.json", json.dumps(cfg))
    name, text = _f17_doc(fingerprint(cp), "SUMMARY", '~"Of 259 studies"', "measured")
    f = write(tmp_path, name, text)
    rc, out, _ = run([f, "--config", cp])
    assert "F17" not in out, out


def test_f17_off_when_config_key_false(tmp_path):
    cfg = json.loads(STAGED.read_text())
    cfg["lint"]["numeric_rows_require_fulltext"] = False
    cp = write(tmp_path, "c6off.json", json.dumps(cfg))
    name, text = _f17_doc(fingerprint(cp), "SUMMARY", '~"Of 259 studies"', "measured")
    f = write(tmp_path, name, text)
    rc, out, _ = run([f, "--config", cp])
    assert "F17" not in out, out


def test_numeric_row_reason_helper():
    cfg = crl.load_config(STAGED)[0]
    assert crl.numeric_row_reason({"quote": '~"in 2023"', "strength": "survey"}, cfg) is None
    assert "259" in crl.numeric_row_reason({"quote": '~"259 studies"', "strength": "survey"}, cfg)
    assert "measured" in crl.numeric_row_reason({"quote": '"x"', "strength": "measured"}, cfg)
