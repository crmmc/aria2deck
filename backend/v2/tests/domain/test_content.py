from __future__ import annotations

import pytest

from aria2deck.domain.content import (
    ContentDeletionState,
    ContentKind,
    content_identity,
    join_relative_path,
    normalize_relative_path,
    plan_reference_removal,
    require_physical_deletable,
    validate_display_name,
)
from aria2deck.domain.errors import DomainError, ErrorCode


@pytest.mark.parametrize("kind", list(ContentKind))
def test_content_identity_is_scoped_by_object_kind(kind: ContentKind) -> None:
    digest = "ab" * 32
    identity = content_identity(kind, digest)
    assert identity.key == f"{kind.value}:{digest}"
    assert identity.digest == digest


def test_file_and_directory_with_same_digest_do_not_collide() -> None:
    digest = "cd" * 32
    assert content_identity(ContentKind.FILE, digest) != content_identity(
        ContentKind.DIRECTORY, digest.upper()
    )


@pytest.mark.parametrize("digest", ["", "ab", "g" * 64, "a" * 65])
def test_content_identity_rejects_non_sha256_digest(digest: str) -> None:
    with pytest.raises(DomainError) as caught:
        content_identity(ContentKind.FILE, digest)
    assert caught.value.code is ErrorCode.INVALID_INPUT


@pytest.mark.parametrize("name", ["文件.iso", "a" * 200, "name with spaces"])
def test_display_name_accepts_unicode_and_real_boundaries(name: str) -> None:
    assert validate_display_name(name) == name


@pytest.mark.parametrize(
    "name", ["", "a" * 201, ".", "..", "a/b", r"a\b", "a\x00b"]
)
def test_display_name_rejects_paths_and_invalid_names(name: str) -> None:
    with pytest.raises(DomainError):
        validate_display_name(name)


@pytest.mark.parametrize(
    "path,allow_root,expected",
    [
        ("", True, ""),
        ("folder/file.bin", False, "folder/file.bin"),
        ("目录/子目录/文件", False, "目录/子目录/文件"),
    ],
)
def test_relative_path_normalization(path: str, allow_root: bool, expected: str) -> None:
    assert normalize_relative_path(path, allow_root=allow_root) == expected


@pytest.mark.parametrize(
    "path", ["", "/absolute", "a//b", "a/", "./a", "a/../b", r"a\b", "a\x00b"]
)
def test_relative_path_rejects_escape_and_ambiguous_layout(path: str) -> None:
    with pytest.raises(DomainError):
        normalize_relative_path(path)


def test_join_relative_path_validates_both_sides() -> None:
    assert join_relative_path("", "root") == "root"
    assert join_relative_path("root", "child") == "root/child"
    with pytest.raises(DomainError):
        join_relative_path("root", "../child")


def test_removing_one_of_many_releases_usage_without_physical_deletion() -> None:
    decision = plan_reference_removal(reference_count=2, pending_pack_sources=False)
    assert decision.used_released is True
    assert decision.schedule_physical_delete is False


def test_last_reference_schedules_delete_only_when_no_pack_source_blocks_it() -> None:
    decision = plan_reference_removal(reference_count=1, pending_pack_sources=False)
    assert decision.schedule_physical_delete is True
    with pytest.raises(DomainError) as caught:
        plan_reference_removal(reference_count=1, pending_pack_sources=True)
    assert caught.value.code is ErrorCode.CONTENT_BUSY


def test_reference_removal_rejects_impossible_count() -> None:
    with pytest.raises(DomainError):
        plan_reference_removal(reference_count=0, pending_pack_sources=False)


def test_physical_delete_requires_pending_zero_references_and_drained_leases() -> None:
    require_physical_deletable(ContentDeletionState.PENDING_DELETE, 0, 0)
    for state, references, leases, code in [
        (ContentDeletionState.ACTIVE, 0, 0, ErrorCode.INVALID_STATE),
        (ContentDeletionState.PENDING_DELETE, 1, 0, ErrorCode.CONTENT_BUSY),
        (ContentDeletionState.PENDING_DELETE, 0, 1, ErrorCode.CONTENT_BUSY),
    ]:
        with pytest.raises(DomainError) as caught:
            require_physical_deletable(state, references, leases)
        assert caught.value.code is code
