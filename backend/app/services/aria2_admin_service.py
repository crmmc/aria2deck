from __future__ import annotations

import logging

import app.aria2.gateway as aria2_gateway
from app.core.security import redact_url_for_log
from app.domain.errors import BadRequestError
from app.services import backend_connectivity, settings_service

logger = logging.getLogger(__name__)


async def get_aria2_version(admin_id: int | None) -> dict:
    aria2_rpc_url = (
        await settings_service.get_config_value("aria2_rpc_url")
        or "http://localhost:6800/jsonrpc"
    )
    aria2_rpc_secret = await settings_service.get_config_value("aria2_rpc_secret") or ""

    client = aria2_gateway.create_aria2_client(aria2_rpc_url, aria2_rpc_secret)

    try:
        version_info = await client.get_version()
        await backend_connectivity.mark_ok()
        logger.info("获取aria2版本成功 admin_id=%s", admin_id)
        return {
            "connected": True,
            "version": version_info.get("version"),
            "enabled_features": version_info.get("enabledFeatures", []),
        }
    except Exception as exc:
        await backend_connectivity.mark_fail()
        logger.warning(
            "获取aria2版本失败 admin_id=%s error_type=%s",
            admin_id,
            type(exc).__name__,
        )
        return {
            "connected": False,
            "error": "无法连接到 aria2 服务",
        }


async def test_aria2_connection(
    *,
    admin_id: int | None,
    aria2_rpc_url: str,
    aria2_rpc_secret: str | None,
) -> dict:
    if not aria2_rpc_url:
        raise BadRequestError("aria2 RPC URL 不能为空")

    secret = aria2_rpc_secret
    if secret is None or (isinstance(secret, str) and secret.startswith("*")):
        secret = await settings_service.get_config_value("aria2_rpc_secret") or ""

    client = aria2_gateway.create_aria2_client(aria2_rpc_url, secret)

    try:
        version_info = await client.get_version()
        await backend_connectivity.mark_ok()
        logger.info(
            "测试aria2连接成功 admin_id=%s url=%s",
            admin_id,
            redact_url_for_log(aria2_rpc_url),
        )
        return {
            "connected": True,
            "version": version_info.get("version"),
            "enabled_features": version_info.get("enabledFeatures", []),
        }
    except Exception as exc:
        await backend_connectivity.mark_fail()
        logger.warning(
            "测试aria2连接失败 admin_id=%s url=%s error_type=%s",
            admin_id,
            redact_url_for_log(aria2_rpc_url),
            type(exc).__name__,
        )
        return {
            "connected": False,
            "error": "无法连接到 aria2 服务",
        }
