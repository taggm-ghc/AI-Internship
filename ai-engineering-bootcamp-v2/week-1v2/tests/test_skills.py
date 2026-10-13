"""Contract tests for the project skills (5.d2): positive, negative, degradation.

Deterministic, stdlib plus pytest, no network, no LLM. Fixtures are synthetic and
use obvious placeholders only (no key-like values; the config fingerprint is
computed at run time, never written to a file).
Spec: Agent Skills overview (platform.claude.com/docs/en/agents-and-tools/agent-skills/overview).
"""
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = ROOT / ".claude" / "skills"
RIP = SKILLS_DIR / "research-informed-planning"
LINT = RIP / "check_research_log.py"
CONFIG = RIP / "research_config.json"
MAX_NAME, MAX_DESC = 64, 1024
NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
SKILL_DIRS = sorted(p for p in SKILLS_DIR.iterdir() if (p / "SKILL.md").is_file())


# ------------------------------------------------------------ lint helpers
def parse_frontmatter(text):
    """Return (dict, body). Only flat 'key: value' lines (all these skills use)."""
    if not text.startswith("---\n"):
        raise ValueError("SKILL.md must start with a '---' frontmatter fence")
    end = text.find("\n---", 4)
    if end < 0:
        raise ValueError("frontmatter is not closed with '---'")
    fm = {}
    for ln in text[4:end].splitlines():
        if not ln.strip():
            continue
        if ":" not in ln:
            raise ValueError(f"frontmatter line is not 'key: value': {ln[:40]!r}")
        k, v = ln.split(":", 1)
        fm[k.strip()] = v.strip()
    return fm, text[end + 4:]


def lint_skill_text(text, dirname=None):
    """Return a list of problems (empty list means valid)."""
    try:
        fm, _ = parse_frontmatter(text)
    except ValueError as e:
        return [str(e)]
    problems = []
    name, desc = fm.get("name", ""), fm.get("description", "")
    if not name:
        problems.append("name is missing")
    elif len(name) > MAX_NAME:
        problems.append(f"name is {len(name)} chars (max {MAX_NAME})")
    elif not NAME_RE.match(name):
        problems.append("name must be lowercase letters, digits and single hyphens")
    if name and dirname and name != dirname:
        problems.append(f"name {name!r} differs from directory {dirname!r}")
    if not desc:
        problems.append("description is missing or empty")
    elif len(desc) > MAX_DESC:
        problems.append(f"description is {len(desc)} chars (max {MAX_DESC})")
    if re.search(r"<[^>]+>", desc):
        problems.append("description contains an XML-like tag")
    return problems


def backticked_files(text):
    return sorted(set(re.findall(r"`([A-Za-z0-9_./-]+\.(?:py|json|md))`", text)))


def resolve_reference(ref, skill_dir):
    """A reference counts as existing if it resolves against the skill dir, the
    week-1v2 root (or its scripts/), or the AI-Internship root (p3m3/ lives there)."""
    for base in (skill_dir, ROOT, ROOT / "scripts", ROOT.parent.parent, ROOT.parent.parent / "p3m3"):
        if (base / ref).exists():
            return True
    return False


# ------------------------------------------------------- lint log fixtures
def run_lint(args):
    p = subprocess.run([sys.executable, str(LINT), *map(str, args)], capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def _crl():
    sys.path.insert(0, str(RIP))
    import check_research_log as crl
    return crl


def good_log(tmp_path):
    crl = _crl()
    sha = crl.load_config(CONFIG)[1]
    cols = "| " + " | ".join(crl.SOURCE_COLUMNS) + " |"
    sep = "|" + "---|" * len(crl.SOURCE_COLUMNS)
    hdr = {"research_id": "RH", "plan_ref": "plan.md", "role": "researcher", "angle": "against",
           "model": "sonnet", "parent": "main", "depth": 1, "config_version": "1",
           "config_sha256": sha, "budget_limit": 4, "budget_used": 4, "date": "2026-10-02",
           "subclaims": ["S1"]}
    r = ["RH-R-S1", "https://arxiv.org/abs/2310.01798", "Title", "Org", "2023", "arXiv", "high",
         "READ-VERBATIM", '"short quote"', "S1", "against", "measured", "-"]
    lines = ["<!-- research-header " + json.dumps(hdr) + " -->", "", "## Sources", cols, sep,
             "| " + " | ".join(r) + " |", "", "## Gaps", "", "## Verdicts",
             "| sub_claim | verdict | supporting ids | antagonistic ids |", "|---|---|---|---|",
             "| S1 | survived | RH-R-S1 | - |"]
    f = tmp_path / "good.md"
    f.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return f


# ================================================================ POSITIVE
def test_skills_found():
    assert {p.name for p in SKILL_DIRS} >= {"rag-scaffold", "research-informed-planning"}


@pytest.mark.parametrize("d", SKILL_DIRS, ids=lambda p: p.name)
def test_frontmatter_valid(d):
    text = (d / "SKILL.md").read_text(encoding="utf-8")
    assert lint_skill_text(text, d.name) == []


@pytest.mark.parametrize("d", SKILL_DIRS, ids=lambda p: p.name)
def test_name_and_description_limits(d):
    fm, _ = parse_frontmatter((d / "SKILL.md").read_text(encoding="utf-8"))
    assert 0 < len(fm["name"]) <= MAX_NAME and NAME_RE.match(fm["name"])
    assert 0 < len(fm["description"].strip()) <= MAX_DESC


@pytest.mark.parametrize("d", SKILL_DIRS, ids=lambda p: p.name)
def test_referenced_files_exist(d):
    text = (d / "SKILL.md").read_text(encoding="utf-8")
    missing = [r for r in backticked_files(text) if not resolve_reference(r, d)]
    assert not missing, f"{d.name} references files that do not exist: {missing}"


def test_bundled_files_exist():
    for name in ("check_research_log.py", "research_config.json", "test_check_research_log.py"):
        assert (RIP / name).is_file(), name


def test_stale_programme_end_date_gone():
    text = (RIP / "SKILL.md").read_text(encoding="utf-8")
    assert "programme end date (2026-10-12)" not in text
    assert "2026-10-25" in text


def test_lint_passes_good_fixture(tmp_path):
    rc, out, err = run_lint([good_log(tmp_path)])
    assert rc == 0, out + err


# ================================================================ NEGATIVE
def test_malformed_log_fails_loudly(tmp_path):
    bad = tmp_path / "bad.md"
    bad.write_text("this is not a research findings file\n| a | b |\n", encoding="utf-8")
    rc, out, err = run_lint([bad])
    assert rc != 0
    assert out.strip() or err.strip(), "failure must be reported, not silent"


def test_log_with_dropped_header_fails(tmp_path):
    good = good_log(tmp_path).read_text(encoding="utf-8")
    bad = tmp_path / "nohdr.md"
    bad.write_text("\n".join(good.splitlines()[1:]) + "\n", encoding="utf-8")
    rc, out, _ = run_lint([bad])
    assert rc != 0 and "errors=" in out


def test_overlong_description_caught():
    text = "---\nname: example-skill\ndescription: " + "x" * (MAX_DESC + 1) + "\n---\nbody\n"
    problems = lint_skill_text(text, "example-skill")
    assert any("description is" in p and str(MAX_DESC) in p for p in problems)


def test_description_at_limit_accepted():
    text = "---\nname: example-skill\ndescription: " + "x" * MAX_DESC + "\n---\nbody\n"
    assert lint_skill_text(text, "example-skill") == []


@pytest.mark.parametrize("text,needle", [
    ("no frontmatter here\n", "must start with"),
    ("---\nname: example-skill\ndescription: ok\n", "not closed"),
    ("---\nname: Bad_Name\ndescription: ok\n---\n", "lowercase"),
    ("---\nname: " + "a" * 65 + "\ndescription: ok\n---\n", "max 64"),
    ("---\nname: example-skill\ndescription:\n---\n", "empty"),
    ("---\nname: other-name\ndescription: ok\n---\n", "differs from directory"),
    ("---\nname: example-skill\ndescription: has <b>tag</b>\n---\n", "XML"),
])
def test_lint_catches_bad_frontmatter(text, needle):
    problems = lint_skill_text(text, "example-skill")
    assert any(needle in p for p in problems), problems


def test_missing_reference_detected():
    assert not resolve_reference("EXAMPLE_VALUE_does_not_exist.py", RIP)


# ============================================================== DEGRADATION
def test_missing_config_is_usage_error_not_crash(tmp_path):
    rc, out, err = run_lint([good_log(tmp_path), "--config", tmp_path / "absent.json"])
    assert rc == 2
    assert "cannot read config" in err and "Traceback" not in err


def test_invalid_config_is_usage_error(tmp_path):
    cfg = tmp_path / "cfg.json"
    cfg.write_text("{not json", encoding="utf-8")
    rc, _, err = run_lint([good_log(tmp_path), "--config", cfg])
    assert rc == 2 and "not valid JSON" in err and "Traceback" not in err


def test_config_missing_required_key_is_usage_error(tmp_path):
    cfg = tmp_path / "cfg.json"
    cfg.write_text("{}", encoding="utf-8")
    rc, _, err = run_lint([good_log(tmp_path), "--config", cfg])
    assert rc in (2, 3) and "Traceback" not in err


def test_missing_findings_file_is_reported(tmp_path):
    rc, _, err = run_lint([tmp_path / "nope.md"])
    assert rc == 2 and "Traceback" not in err


def test_lint_runs_without_optional_dependencies(tmp_path):
    """Stdlib only: block third-party imports and check the lint still passes."""
    f = good_log(tmp_path)
    code = ("import sys, runpy\n"
            "class Block:\n"
            "    def find_spec(self, name, path=None, target=None):\n"
            "        if name.split('.')[0] in {'yaml', 'requests', 'numpy', 'pandas', 'dotenv'}:\n"
            "            raise ImportError('blocked: ' + name)\n"
            "sys.meta_path.insert(0, Block())\n"
            f"sys.argv = [{str(LINT)!r}, {str(f)!r}]\n"
            "runpy.run_path(sys.argv[0], run_name='__main__')\n")
    p = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert p.returncode == 0, p.stdout + p.stderr


def test_skill_parse_does_not_need_yaml():
    # The frontmatter helper above is stdlib only; a missing yaml package must not matter.
    fm, _ = parse_frontmatter("---\nname: a\ndescription: b\n---\nx")
    assert fm == {"name": "a", "description": "b"}
