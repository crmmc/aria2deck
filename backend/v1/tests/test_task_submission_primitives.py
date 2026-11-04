"""Planned GID primitive tests (M24 Task 1)."""

from __future__ import annotations

import hashlib
import hmac
import re

import pytest

import app.services.task_batch_submission as mod
from app.services.task_batch_submission import derive_planned_gid


def _pepper(monkeypatch, value="a" * 32):
    monkeypatch.setattr(mod, "get_credential_pepper", lambda: value)


class TestDerivePlannedGid:
    def test_stable_and_16_lowercase_hex(self, monkeypatch):
        _pepper(monkeypatch)
        g1 = derive_planned_gid(42)
        g2 = derive_planned_gid(42)
        assert g1 == g2
        assert re.fullmatch(r"[0-9a-f]{16}", g1)

    def test_different_tids_differ(self, monkeypatch):
        _pepper(monkeypatch)
        assert derive_planned_gid(1) != derive_planned_gid(2)

    def test_domain_separated_from_naive_hmac(self, monkeypatch):
        pepper = "a" * 32
        _pepper(monkeypatch, pepper)
        naive = hmac.new(
            pepper.encode(), str(7).encode(), hashlib.sha256
        ).hexdigest()[:16]
        assert derive_planned_gid(7) != naive

    def test_secret_change_changes_output(self, monkeypatch):
        _pepper(monkeypatch, "a" * 32)
        first = derive_planned_gid(9)
        _pepper(monkeypatch, "b" * 32)
        assert derive_planned_gid(9) != first

    def test_output_contains_no_secret_material(self, monkeypatch):
        pepper = "super-secret-pepper-value-32-bytes-ok!"
        _pepper(monkeypatch, pepper)
        assert pepper not in derive_planned_gid(3)
