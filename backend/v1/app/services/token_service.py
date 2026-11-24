from __future__ import annotations

import logging
import secrets

from app.core.security import credential_digest, credential_prefix
from app.core.time_utils import ms_to_iso
from app.domain.errors import BadRequestError, NotFoundError
from app.repositories import auth as auth_repo

logger = logging.getLogger(__name__)


def generate_api_token() -> str:
    return "aria2_" + secrets.token_urlsafe(32)


async def list_tokens(user_id: int) -> list[dict]:
    rows = await auth_repo.list_api_tokens(user_id)
    # Arguments are user ID and row count only; no token material is logged.
    # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
    logger.debug("查询Token列表 user_id=%s count=%s", user_id, len(rows))
    return [
        {
            "id": row["id"],
            "name": row["name"],
            "prefix": row["token_prefix"],
            "created_at": ms_to_iso(row["created_at_ms"]),
            "last_used_at": ms_to_iso(row["last_used_at_ms"]),
        }
        for row in rows
    ]


async def create_token(user_id: int, name: str | None) -> dict:
    for _ in range(20):
        token = generate_api_token()
        try:
            row = await auth_repo.create_api_token(
                user_id,
                credential_digest("api-token", token),
                credential_prefix(token),
                name,
            )
        except auth_repo.DuplicateCredentialError:
            continue
        # Arguments are IDs and the display name; the generated token and digest are not logged.
        # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
        logger.info(
            "创建API Token user_id=%s token_id=%s token_name=%s",
            user_id,
            row["id"],
            name,
        )
        return {
            "id": row["id"],
            "name": row["name"],
            "prefix": row["token_prefix"],
            "token": token,
            "created_at": ms_to_iso(row["created_at_ms"]),
            "last_used_at": None,
        }
    raise BadRequestError("生成 API Token 失败，请稍后重试")


async def delete_token(user_id: int, token_id: int) -> dict:
    deleted = await auth_repo.delete_api_token(user_id, token_id)
    if not deleted:
        # Arguments are database IDs and a fixed reason; no token material is logged.
        # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
        logger.warning(
            "删除Token失败 user_id=%s token_id=%s reason=not_found_or_forbidden",
            user_id,
            token_id,
        )
        raise NotFoundError("Token 不存在")

    # Arguments are database IDs only; no token material is logged.
    # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
    logger.info("删除Token成功 user_id=%s token_id=%s", user_id, token_id)
    return {"ok": True}


async def invalidate_all_credential_digests(*, actor_id: int) -> dict:
    counts = await auth_repo.invalidate_all_credential_digests()
    # Arguments are the actor ID and deletion counts; no credential digest is logged.
    # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
    logger.warning(
        "管理员作废全部凭证摘要 actor_id=%s api_token_count=%s rpc_secret_count=%s",
        actor_id,
        counts["api_token_count"],
        counts["rpc_secret_count"],
    )
    return {
        "ok": True,
        "api_token_count": counts["api_token_count"],
        "rpc_secret_count": counts["rpc_secret_count"],
    }
