"""Tests for application state dataclasses."""

from tongs.state.app_state import MRFilter, ReviewDraft


class TestMRFilter:
    def test_defaults(self):
        f = MRFilter()
        assert f.state == "open"
        assert f.author == ""
        assert f.search == ""

    def test_default_classmethod_returns_fresh_instance(self):
        a = MRFilter.default()
        b = MRFilter.default()
        assert a is not b
        assert a.state == "open"
        assert b.state == "open"


class TestReviewDraft:
    def test_defaults(self):
        d = ReviewDraft()
        assert d.repo_path == ""
        assert d.mr_number == 0
        assert d.verdict is None
        assert d.body == ""
        assert d.inline_comments == []

    def test_inline_comments_independent_per_instance(self):
        a = ReviewDraft()
        b = ReviewDraft()
        a.inline_comments.append({"file": "a.py", "line": 1, "body": "nit"})
        assert b.inline_comments == []
