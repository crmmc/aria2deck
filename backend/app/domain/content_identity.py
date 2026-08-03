from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

CONTENT_HASH_V1 = "v1"
CONTENT_HASH_V2 = "v2"
LEGACY_OBJECT_KIND = "legacy"
CONTENT_OBJECT_KINDS = ("file", "directory")
_DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class ContentIdentity:
    version: str
    object_kind: str
    digest: str

    @property
    def content_hash(self) -> str:
        if self.version == CONTENT_HASH_V2:
            return f"{self.version}:{self.object_kind}:{self.digest}"
        return self.digest


def is_v2_digest(value: str) -> bool:
    return bool(_DIGEST_PATTERN.fullmatch(value))


def v2_content_identity(object_kind: str, digest: str) -> ContentIdentity:
    if object_kind not in CONTENT_OBJECT_KINDS or not is_v2_digest(digest):
        raise ValueError("invalid v2 content identity")
    return ContentIdentity(CONTENT_HASH_V2, object_kind, digest)


def content_identity_from_content_hash(content_hash: str) -> ContentIdentity:
    parts = content_hash.split(":")
    if parts[0] == CONTENT_HASH_V2:
        if len(parts) != 3:
            raise ValueError("invalid v2 content identity")
        return v2_content_identity(parts[1], parts[2])
    return ContentIdentity(CONTENT_HASH_V1, LEGACY_OBJECT_KIND, content_hash)


def content_identity_from_row(row: dict[str, Any]) -> ContentIdentity:
    version = str(row.get("content_hash_version") or CONTENT_HASH_V1)
    kind = str(row.get("content_object_kind") or LEGACY_OBJECT_KIND)
    digest = str(row.get("content_digest") or row["content_hash"])
    identity = ContentIdentity(version, kind, digest)
    if identity.content_hash != str(row["content_hash"]):
        raise ValueError("stored content identity is invalid")
    if version == CONTENT_HASH_V2:
        return v2_content_identity(kind, digest)
    if version != CONTENT_HASH_V1 or kind != LEGACY_OBJECT_KIND:
        raise ValueError("stored content identity is invalid")
    return identity
