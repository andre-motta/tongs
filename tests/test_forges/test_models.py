"""Tests for forge data models."""

from __future__ import annotations

from dataclasses import fields

from tongs.forges.models import CIStatus, MRDetail, ReviewDecision


def test_mr_detail_revision_fields_are_appended_for_positional_compatibility():
    names = [field.name for field in fields(MRDetail)]
    assert names[-3:] == ["head_sha", "base_sha", "start_sha"]


class TestForgeModels:
    def test_ci_status_values(self):
        assert CIStatus.SUCCESS.value == "success"
        assert CIStatus.FAILED.value == "failed"

    def test_review_decision_values(self):
        assert ReviewDecision.APPROVED.value == "approved"
        assert ReviewDecision.CHANGES_REQUESTED.value == "changes_requested"
