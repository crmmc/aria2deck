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
        logger.warning(
            "删除Token失败 user_id=%s token_id=%s reason=not_found_or_forbidden",
            user_id,
            token_id,
        )
        raise NotFoundError("Token 不存在")

    logger.info("删除Token成功 user_id=%s token_id=%s", user_id, token_id)
    return {"ok": True}
