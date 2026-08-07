"""Contract tests for M3 lifecycle result and claim types (T02)."""

from __future__ import annotations

import dataclasses

import pytest

from app.domain.lifecycle import (
    RepairClaim,
    ReconcileResult,
    TerminalizationClaim,
    make_repair_claim,
    make_terminalization_claim,
)

REQUIRED_RESULT_VALUES = frozenset(
    {"changed", "stale", "waiting", "completed", "terminalized", "ignored"}
)


class TestReconcileResult:
    def test_contains_required_spec_6_1_values(self):
        actual = {r.value for r in ReconcileResult}
        assert REQUIRED_RESULT_VALUES <= actual

    def test_values_are_unique(self):
        values = [r.value for r in ReconcileResult]
        assert len(values) == len(set(values))

    def test_comparable_by_equality(self):
        assert ReconcileResult.CHANGED == ReconcileResult.CHANGED
        assert ReconcileResult.CHANGED != ReconcileResult.STALE

    def test_str_value_matches_enum_value(self):
        assert str(ReconcileResult.STALE) == "stale"

    def test_extra_values_are_spec_consistent(self):
        extras = {
            ReconcileResult.ALREADY_TERMINAL,
            ReconcileResult.RECOVERY_PENDING,
            ReconcileResult.ALREADY_ACTIVE,
            ReconcileResult.ALREADY_COMPLETE,
            ReconcileResult.CLEANUP_PENDING,
        }
        actual = set(ReconcileResult)
        assert extras <= actual


class TestNormalFailureClaim:
    def test_single_gid_claim_fields(self):
        claim = make_terminalization_claim(
            attempt_id=42,
            expected_current_gid="abc123",
            writer_gids=("abc123",),
            result_gids=("abc123",),
            terminal_status="failed",
            claim_timestamp=1700000000000,
            error_code="gid_missing",
            error_message="GID not found in aria2",
        )
        assert claim.attempt_id == 42
        assert claim.expected_current_gid == "abc123"
        assert claim.writer_gids == ("abc123",)
        assert claim.result_gids == ("abc123",)
        assert claim.terminal_status == "failed"
        assert claim.claim_timestamp == 1700000000000
        assert claim.error_code == "gid_missing"
        assert claim.error_message == "GID not found in aria2"

    def test_null_expected_gid_for_queued_submit_failure(self):
        claim = make_terminalization_claim(
            attempt_id=1,
            expected_current_gid=None,
            writer_gids=(),
            result_gids=(),
            terminal_status="failed",
            claim_timestamp=100,
            error_code="submit_failed",
        )
        assert claim.expected_current_gid is None
        assert claim.writer_gids == ()

    def test_error_fields_are_optional(self):
        claim = make_terminalization_claim(
            attempt_id=1,
            expected_current_gid="g",
            writer_gids=("g",),
            result_gids=("g",),
            terminal_status="cancelled",
            claim_timestamp=0,
        )
        assert claim.error_code is None
        assert claim.error_message is None


class TestHandoffDoubleGidClaim:
    def test_writer_gids_include_source_and_payload(self):
        claim = make_terminalization_claim(
            attempt_id=99,
            expected_current_gid="source_gid",
            writer_gids=("source_gid", "payload_gid"),
            result_gids=("source_gid", "payload_gid"),
            terminal_status="failed",
            claim_timestamp=1700000000001,
            error_code="handoff_unknown_size",
            error_message="payload size unknown during handoff",
        )
        assert claim.writer_gids == ("source_gid", "payload_gid")
        assert claim.result_gids == ("source_gid", "payload_gid")
        assert claim.expected_current_gid == "source_gid"

    def test_result_gids_can_be_narrower_than_writer_gids(self):
        claim = make_terminalization_claim(
            attempt_id=99,
            expected_current_gid="source_gid",
            writer_gids=("source_gid", "payload_gid"),
            result_gids=("source_gid",),
            terminal_status="failed",
            claim_timestamp=1700000000001,
        )
        assert claim.writer_gids == ("source_gid", "payload_gid")
        assert claim.result_gids == ("source_gid",)


class TestRepairClaim:
    def test_repair_claim_fields(self):
        claim = make_repair_claim(
            attempt_id=7,
            expected_current_gid="residual_gid",
            writer_gids=("residual_gid",),
            result_gids=("residual_gid",),
            terminal_status="cancelled",
            claim_timestamp=1700000000002,
        )
        assert claim.attempt_id == 7
        assert claim.expected_current_gid == "residual_gid"
        assert claim.writer_gids == ("residual_gid",)
        assert claim.result_gids == ("residual_gid",)
        assert claim.terminal_status == "cancelled"
        assert claim.claim_timestamp == 1700000000002

    def test_repair_claim_has_no_error_fields(self):
        """RepairClaim must not carry error_code/error_message — it does not
        change business terminal state, only grants physical reclaim."""
        field_names = {f.name for f in dataclasses.fields(RepairClaim)}
        assert "error_code" not in field_names
        assert "error_message" not in field_names

    def test_repair_claim_carries_existing_terminal_status(self):
        for status in ("failed", "cancelled"):
            claim = make_repair_claim(
                attempt_id=1,
                expected_current_gid="g",
                writer_gids=("g",),
                result_gids=("g",),
                terminal_status=status,
                claim_timestamp=0,
            )
            assert claim.terminal_status == status


class TestImmutability:
    def test_terminalization_claim_is_frozen(self):
        claim = make_terminalization_claim(
            attempt_id=1,
            expected_current_gid="g",
            writer_gids=("g",),
            result_gids=("g",),
            terminal_status="failed",
            claim_timestamp=0,
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            claim.error_code = "tampered"  # type: ignore[misc]

    def test_repair_claim_is_frozen(self):
        claim = make_repair_claim(
            attempt_id=1,
            expected_current_gid="g",
            writer_gids=("g",),
            result_gids=("g",),
            terminal_status="failed",
            claim_timestamp=0,
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            claim.terminal_status = "completed"  # type: ignore[misc]

    def test_factory_normalises_list_to_tuple(self):
        claim = make_terminalization_claim(
            attempt_id=1,
            expected_current_gid="g",
            writer_gids=["g1", "g2"],
            result_gids=["g1"],
            terminal_status="failed",
            claim_timestamp=0,
        )
        assert isinstance(claim.writer_gids, tuple)
        assert claim.writer_gids == ("g1", "g2")
        assert isinstance(claim.result_gids, tuple)
