"""M4 T02+T03: repositories/task/ 包边界测试（spec §3.2）。

AST-level guards:

1. ``app/repositories/task/downloads.py`` 存在且定义所有 global_downloads 相关函数。
2. ``app/repositories/task/user_tasks.py`` 存在且定义所有 user_tasks 相关函数。
3. ``app/repositories/downloads.py`` 不再定义已迁移的函数。
"""

from __future__ import annotations

import ast
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parents[1] / "app"

MIGRATED_FUNCTIONS = {
    "active_physical_commitment_bytes",
    "get_active_physical_commitment_bytes",
    "get_global_by_resource_key",
    "find_live_global_download_by_resource_key",
    "find_latest_completed_global_download_by_resource_key",
    "get_global_download_by_id",
    "get_global_download_by_gid",
    "list_active_global_downloads",
    "list_active_like_http_downloads",
    "list_tracked_global_downloads",
    "get_global_download_status_snapshot",
    "get_global_download_for_generation",
    "list_inconsistent_completed_download_ids",
    "list_completed_downloads_without_file",
    "list_stale_queued_download_ids",
    "create_global_download",
    "create_global_download_attempt",
    "reset_active_accounting_for_startup",
    "reconcile_download_size",
    "assign_submitted_gid",
    "claim_submitted_gid_for_failure",
    "update_global_download",
    "guarded_update_global_download",
    "guarded_update_download_and_active_user_tasks",
    "complete_attempt",
    "replace_terminal_download_gid",
    "clear_terminal_download_gid",
    "list_terminal_downloads_with_residual_gid",
    "list_terminal_download_ids",
    "list_completed_downloads_pending_index",
    "reopen_completed_download_for_index_repair",
    "restore_incomplete_completed_download",
    "claim_attempt_terminal",
    "claim_terminal_reclaim",
}

MIGRATED_PRIVATE_HELPERS = {
    "_lock_active_download",
    "_terminate_active_task_row",
    "_fail_active_task_row",
    "_fail_download_rows",
    "_remaining_disk_bytes",
    "_complete_user_task_with_file",
}

MIGRATED_CLASSES = {"SizeReconcileResult"}

MIGRATED_SHARED_HELPERS = {
    "_reconcile_download_size_locked",
    "_strict_adjust_usage_reserved",
    "_resize_active_task",
    "_disk_resize_fits",
    "_resize_subscribers",
    "_cancel_download_without_subscribers",
    "refreshable_user_task_display_name_condition",
    "now_ms",
}

USER_TASKS_FUNCTIONS = {
    "get_representative_active_owner_id",
    "get_user_task",
    "get_user_task_by_id",
    "get_user_task_by_gid",
    "list_user_tasks",
    "list_user_tasks_page",
    "list_user_tasks_for_download",
    "delete_all_terminal_user_tasks",
    "delete_terminal_user_task_by_gid",
    "delete_terminal_user_task",
    "clear_terminal_user_tasks",
    "create_user_task",
    "admit_user_task",
    "fail_user_task_submission",
    "update_active_user_tasks",
    "update_user_task",
    "attach_completed_file_to_user",
    "complete_active_user_tasks_for_stored_file",
    "repair_completed_download_with_stored_file",
    "mark_global_download_failed",
    "cancel_active_user_task",
    "cancel_user_task_and_maybe_claim_attempt",
    "count_active_user_tasks",
}

USER_TASKS_CLASSES = {"DownloadAdmissionError"}


def _defined_names(relative: str) -> set[str]:
    tree = ast.parse(
        (APP_ROOT / relative).read_text(encoding="utf-8"), filename=relative
    )
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
    return names


def test_task_downloads_package_defines_global_download_functions() -> None:
    defined = _defined_names("repositories/task/downloads.py")
    missing = (
        MIGRATED_FUNCTIONS
        | MIGRATED_PRIVATE_HELPERS
        | MIGRATED_SHARED_HELPERS
        | MIGRATED_CLASSES
    ) - defined
    assert not missing, f"repositories/task/downloads.py missing: {missing}"

    import app.repositories.task.downloads as task_downloads

    assert hasattr(task_downloads, "SizeReconcileResult")


def test_task_user_tasks_defines_user_task_functions() -> None:
    defined = _defined_names("repositories/task/user_tasks.py")
    missing = (USER_TASKS_FUNCTIONS | USER_TASKS_CLASSES) - defined
    assert not missing, f"repositories/task/user_tasks.py missing: {missing}"


def test_legacy_downloads_no_longer_defines_migrated_functions() -> None:
    legacy = APP_ROOT / "repositories/downloads.py"
    if not legacy.exists():
        return  # T04 已删除旧文件
    defined = _defined_names("repositories/downloads.py")
    leaked = (
        MIGRATED_FUNCTIONS
        | MIGRATED_PRIVATE_HELPERS
        | MIGRATED_SHARED_HELPERS
        | MIGRATED_CLASSES
        | USER_TASKS_FUNCTIONS
        | USER_TASKS_CLASSES
    ) & defined
    # now_ms 已统一归位到 app.core.time_utils，旧文件已删除
    assert not leaked, f"repositories/downloads.py still defines: {leaked}"
