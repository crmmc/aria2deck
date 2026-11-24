from __future__ import annotations

import fcntl
import os
from pathlib import Path

import pytest

from aria2deck.domain.errors import DomainError, ErrorCode
from aria2deck.infrastructure.instance_lock import InstanceLock
from aria2deck.infrastructure.security import CredentialManager


def test_credentials_have_unique_raw_values_and_purpose_specific_digests() -> None:
    manager = CredentialManager(pepper=None, debug=True)

    first_session = manager.new_session_id()
    second_session = manager.new_session_id()
    token = manager.new_api_token()
    rpc_secret = manager.new_rpc_secret()
    assert first_session != second_session
    assert token.startswith("aria2_")
    assert rpc_secret.startswith("rpc_")
    assert manager.digest_session(first_session) == manager.digest_session(first_session)
    assert manager.digest_session(first_session) != manager.digest_session(second_session)
    assert manager.digest_session(token) != manager.digest_api_token(token)
    assert token not in manager.digest_api_token(token)


def test_password_hashes_verify_and_malformed_storage_fails_closed() -> None:
    manager = CredentialManager(pepper=None, debug=True)
    stored = manager.hash_password("ab" * 32)

    assert stored.startswith("pbkdf2_sha256$120000$")
    assert manager.verify_password("ab" * 32, stored)
    assert not manager.verify_password("cd" * 32, stored)
    for malformed in (
        "bad",
        "other$120000$00$00",
        "pbkdf2_sha256$bad$00$00",
        "pbkdf2_sha256$120000$not-hex$00",
    ):
        assert not manager.verify_password("ab" * 32, malformed)
    assert not manager.verify_password("不是十六进制", stored)


def test_rpc_secret_encryption_round_trip_and_tamper_detection() -> None:
    manager = CredentialManager(pepper=None, debug=True)
    ciphertext = manager.encrypt("rpc-secret")

    assert ciphertext != "rpc-secret"
    assert manager.decrypt(ciphertext) == "rpc-secret"
    with pytest.raises(DomainError) as caught:
        manager.decrypt(ciphertext[:-1] + ("A" if ciphertext[-1] != "A" else "B"))
    assert caught.value.code is ErrorCode.INVALID_STATE
    with pytest.raises(DomainError):
        manager.decrypt("不是密文")


@pytest.mark.parametrize("pepper", [None, "", "short"])
def test_production_credential_manager_requires_a_real_pepper(pepper: str | None) -> None:
    with pytest.raises(DomainError) as caught:
        CredentialManager(pepper=pepper, debug=False)
    assert caught.value.code is ErrorCode.CONFIGURATION_INVALID


def test_instance_lock_excludes_another_instance_and_releases_cleanly(tmp_path: Path) -> None:
    path = tmp_path / "v2.lock"
    first = InstanceLock(path)
    second = InstanceLock(path)

    first.acquire()
    first.acquire()
    with pytest.raises(DomainError) as caught:
        second.acquire()
    assert caught.value.code is ErrorCode.INVALID_STATE
    first.release()
    first.release()
    second.acquire()
    second.release()


def test_instance_lock_closes_descriptor_when_locking_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = InstanceLock(tmp_path / "v2.lock")
    closed: list[int] = []
    original_close = os.close

    def track_close(descriptor: int) -> None:
        closed.append(descriptor)
        original_close(descriptor)

    def fail_lock(*args: object) -> None:
        raise OSError("simulated lock failure")

    monkeypatch.setattr(os, "close", track_close)
    monkeypatch.setattr(fcntl, "flock", fail_lock)
    with pytest.raises(OSError):
        lock.acquire()

    assert lock._descriptor is None
    assert len(closed) == 1


def test_instance_lock_closes_descriptor_even_when_unlock_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock = InstanceLock(tmp_path / "v2.lock")
    other = InstanceLock(tmp_path / "v2.lock")
    lock.acquire()
    descriptor = lock._descriptor
    original_flock = fcntl.flock

    def fail_unlock(fd: int, operation: int) -> None:
        if operation == fcntl.LOCK_UN:
            raise OSError("simulated unlock failure")
        original_flock(fd, operation)

    acquired_other = False
    try:
        monkeypatch.setattr(fcntl, "flock", fail_unlock)
        with pytest.raises(OSError):
            lock.release()
        monkeypatch.setattr(fcntl, "flock", original_flock)
        other.acquire()
        acquired_other = True
    finally:
        if acquired_other:
            other.release()
        elif descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
