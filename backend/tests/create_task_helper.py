"""旧 create_user_download 测试 helper 的 Task Core 迁移。

原 ``create_user_download(user_id, quota_bytes, uri, resource_key,
resource_kind, display_name, total_bytes, aria2_client=...)`` 签名已随
download_service 旧链删除。本 helper 以同样的固定参数构造
``task_service.register_and_submit`` 调用，fake client 通过
``set_task_backend_override`` 注入，避免与真实 aria2 交互。
"""

from __future__ import annotations

from typing import Any

from app.modules.backend.aria2_adapter import Aria2BackendAdapter
from app.modules.task_core.register import ResourceSpec
from app.services import task_service


async def create_download_task(
    *,
    user_id: int,
    quota_bytes: int,
    uri: str,
    resource_key: str,
    resource_kind: str,
    display_name: str,
    total_bytes: int,
    aria2_client: Any,
) -> dict:
    """以固定 resource 参数走新 Task Core 创建路径，返回 REST 任务 payload。"""
    task_service.set_task_backend_override(Aria2BackendAdapter(aria2_client))
    try:
        return await task_service.register_and_submit(
            user_id=user_id,
            quota_bytes=quota_bytes,
            resource=ResourceSpec(
                resource_key=resource_key,
                source_uri=uri,
                resource_kind=resource_kind,
                display_name=display_name,
                size_bytes=total_bytes,
                size_known=bool(total_bytes),
            ),
        )
    finally:
        task_service.set_task_backend_override(None)


def global_download_id_of(task: dict) -> int:
    """旧 payload 的 ``global_download_id`` 在 REST 投影中改名为 ``task_id``。"""
    return int(task["task_id"])
