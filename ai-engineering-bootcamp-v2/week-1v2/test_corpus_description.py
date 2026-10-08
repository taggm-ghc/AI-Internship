"""p3m3 item #80: public corpus description, regenerated only when the corpus timestamp or count changes."""
import json
import unittest
from unittest import mock

import corpus_description as cd
import main


class CorpusDescriptionCache(unittest.TestCase):
    def setUp(self):
        main._CORPUS_SUMMARY_CACHE = None
        main._CORPUS_SUMMARY_RETRY_AT = 0.0
        self.store = {}
        self.overview = {"document_count": 3, "corpus_updated_at": "2026-10-07T00:00:00+00:00"}
        self.calls = 0

        def gen(overview):
            self.calls += 1
            return f"summary {self.calls}", ["topic"], "groq:m"
        for target, value in (("corpus_overview", lambda: dict(self.overview)),
                              ("_generate_corpus_summary", gen),
                              ("get_artifact", lambda path: self.store.get(path)),
                              ("put_artifact", lambda path, payload: self.store.__setitem__(path, payload)),
                              ("corpus_profile", lambda: {"titles": [], "published_years": [None, None]})):
            p = mock.patch.object(main, target, value)
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(lambda: setattr(main, "_CORPUS_SUMMARY_CACHE", None))

    def test_generates_once_until_the_corpus_changes(self):
        a = main._current_corpus_summary()
        b = main._current_corpus_summary()
        self.assertEqual((a["summary"], b["summary"], self.calls), ("summary 1", "summary 1", 1))
        self.overview["corpus_updated_at"] = "2026-10-08T00:00:00+00:00"
        self.assertEqual(main._current_corpus_summary()["summary"], "summary 2")
        self.overview["document_count"] = 4
        self.assertEqual(main._current_corpus_summary()["summary"], "summary 3")

    def test_prompt_version_change_regenerates(self):
        main._current_corpus_summary()
        old = json.loads(self.store[main.CORPUS_SUMMARY_ARTIFACT])
        old.pop("prompt_version")  # a description stored before prompt versioning
        self.store[main.CORPUS_SUMMARY_ARTIFACT] = json.dumps(old).encode()
        main._CORPUS_SUMMARY_CACHE = None
        self.assertEqual(main._current_corpus_summary()["summary"], "summary 2")

    def test_restart_reuses_the_stored_description(self):
        main._current_corpus_summary()
        main._CORPUS_SUMMARY_CACHE = None  # simulate a process restart; the artifact remains
        again = main._current_corpus_summary()
        self.assertEqual((again["summary"], self.calls), ("summary 1", 1))
        self.assertEqual(json.loads(self.store[main.CORPUS_SUMMARY_ARTIFACT])["summary_source"], "agentic_model")

    def test_model_failure_serves_fallback_without_caching_and_waits_before_retrying(self):
        with mock.patch.object(main, "_generate_corpus_summary", side_effect=RuntimeError("down")) as g:
            first = main._current_corpus_summary()
            second = main._current_corpus_summary()
        self.assertEqual(first["summary_source"], "deterministic_fallback")
        self.assertEqual(g.call_count, 1)  # cooldown: no retry storm
        self.assertEqual(second["summary_source"], "deterministic_fallback")
        self.assertNotIn(main.CORPUS_SUMMARY_ARTIFACT, self.store)


class Sanitise(unittest.TestCase):
    def test_plain_text_only(self):
        s = cd.sanitise("See ![x](http://e/p.png) [paper](https://x.org/a) <b>bold</b> at https://evil.example/q now")
        self.assertEqual(s, "See paper bold at now")

    def test_prompt_never_includes_document_text(self):
        msgs = cd.messages_for({"titles": ["T1"], "provenance_types": {"web": 2}, "published_years": ["2023", "2025"]}, 2)
        self.assertIn("- T1", msgs[1]["content"])
        self.assertIn("untrusted", msgs[0]["content"])


if __name__ == "__main__":
    unittest.main()
