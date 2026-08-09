"""M3 T05: sync 与 reconcile 职责边界锁定。

- ``app/modules/task_core/sync.py`` 只写读投影 + 进度/policy，
  不得调用 ``claim_attempt_terminal`` / ``cleanup_with_claim`` /
  ``fail_download_and_reclaim``（这些属 reconcile/终态）。
- 生命周期模块（``app/services/lifecycle/``）不写
  ``task_backend_snapshots`` 投影（投影写归 sync）。
"""

from __future__ import annotations

import re
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
SYNC_PATH = BACKEND_ROOT / "app" / "modules" / "task_core" / "sync.py"
LIFECYCLE_DIR = BACKEND_ROOT / "app" / "services" / "lifecycle"

_RECONCILE_ONLY_CALLS = (
    "claim_attempt_terminal",
    "cleanup_with_claim",
    "fail_download_and_reclaim",
)


def _strip_comments(source: str) -> str:
    source = re.sub(r"#.*", "", source)
    source = re.sub(r'"""(?:.|\n)*?"""', "", source)
    return source


def test_sync_module_has_no_reconcile_terminal_calls() -> None:
    """task_core/sync.py 不得 import 或调用 reconcile/终态专用函数。"""
    source = _strip_comments(SYNC_PATH.read_text(encoding="utf-8"))
    for name in _RECONCILE_ONLY_CALLS:
        assert name not in source, (
            f"{SYNC_PATH.name} 引用了 {name}，终态/清理职责归 reconcile_attempt_signal"
        )


def test_sync_module_does_not_import_lifecycle_service() -> None:
    """sync 不应依赖 lifecycle service（reconcile 方向相反）。"""
    source = _strip_comments(SYNC_PATH.read_text(encoding="utf-8"))
    assert "services.lifecycle" not in source
    assert "reconcile_attempt_signal" not in source


def test_lifecycle_service_does_not_write_projection() -> None:
    """lifecycle 模块不得写 task_backend_snapshots（投影写归 sync）。"""
    for path in sorted(LIFECYCLE_DIR.glob("*.py")):
        source = _strip_comments(path.read_text(encoding="utf-8"))
        assert "upsert_snapshot" not in source, (
            f"{path.name} 引用了 upsert_snapshot，投影写入归 task_core/sync"
        )
        assert "backend_snapshots" not in source
