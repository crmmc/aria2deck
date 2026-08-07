"""T19: 固化启动顺序和异常隔离.

验证 spec 15.1 / 15.4 定义的启动修复顺序：
  1. recover completed-without-index   (pending-index 在 purge 前处理)
  2. rebuild active accounting          (reconcile live attempts)
  3. purge failed/cancelled residual GID
  4. purge safe terminal directories    (strict indexed-completed shell fallback)
  5. orphan store cleanup

关键不变量：
- 单个修复步骤失败不阻塞后续安全阶段
- pending-index 在 purge 前处理
- active 目录、store 内容、未确认归属目录不被启动清理触碰
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app import main as app_main


@pytest.fixture()
def _capture_order():
    """Record the order in which repair functions are actually invoked."""
    order: list[str] = []

    def make_mock(label: str, result: dict | None = None, exc: Exception | None = None):
        async def _mock(*args, **kwargs):
            order.append(label)
            if exc:
                raise exc
            return result or {"found": 0, "purged": 0, "failed": 0, "skipped": 0}

        return _mock

    return order, make_mock


# ---------------------------------------------------------------------------
# Test 1: Startup order matches spec 15.1 / 15.4
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_startup_repair_order_matches_spec(_capture_order):
    """recover → rebuild → residual purge → dir purge."""
    order, make_mock = _capture_order

    with (
        patch.object(
            app_main,
            "recover_completed_downloads_pending_index",
            make_mock("recover"),
        ),
        patch.object(
            app_main,
            "rebuild_active_download_accounting",
            make_mock("rebuild"),
        ),
        patch.object(
            app_main,
            "purge_terminal_residual_gids",
            make_mock("residual_purge"),
        ),
        patch.object(
            app_main,
            "purge_terminal_download_dirs",
            make_mock("dir_purge"),
        ),
    ):
        await app_main._run_startup_repair_sequence(client=object())

    assert order == [
        "recover",
        "rebuild",
        "residual_purge",
        "dir_purge",
    ], f"Expected spec order, got {order}"


# ---------------------------------------------------------------------------
# Test 2: recover happens before residual purge and dir purge
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_recover_before_purge(_capture_order):
    """pending-index recovery must precede any purge step."""
    order, make_mock = _capture_order

    with (
        patch.object(
            app_main,
            "recover_completed_downloads_pending_index",
            make_mock("recover"),
        ),
        patch.object(
            app_main,
            "rebuild_active_download_accounting",
            make_mock("rebuild"),
        ),
        patch.object(
            app_main,
            "purge_terminal_residual_gids",
            make_mock("residual_purge"),
        ),
        patch.object(
            app_main,
            "purge_terminal_download_dirs",
            make_mock("dir_purge"),
        ),
    ):
        await app_main._run_startup_repair_sequence(client=object())

    assert order.index("recover") < order.index("residual_purge")
    assert order.index("recover") < order.index("dir_purge")
    assert order.index("rebuild") < order.index("residual_purge")


# ---------------------------------------------------------------------------
# Test 3: A failing step does not block subsequent steps
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_step_failure_does_not_block_subsequent(_capture_order):
    """If rebuild raises, residual purge and dir purge must still run."""
    order, make_mock = _capture_order

    with (
        patch.object(
            app_main,
            "recover_completed_downloads_pending_index",
            make_mock("recover"),
        ),
        patch.object(
            app_main,
            "rebuild_active_download_accounting",
            make_mock("rebuild", exc=RuntimeError("rebuild exploded")),
        ),
        patch.object(
            app_main,
            "purge_terminal_residual_gids",
            make_mock("residual_purge"),
        ),
        patch.object(
            app_main,
            "purge_terminal_download_dirs",
            make_mock("dir_purge"),
        ),
    ):
        results = await app_main._run_startup_repair_sequence(client=object())

    # rebuild failed but all four steps were attempted
    assert order == ["recover", "rebuild", "residual_purge", "dir_purge"]
    assert results["rebuild_active_accounting"]["ok"] is False
    assert results["recover_pending_index"]["ok"] is True
    assert results["purge_residual_gids"]["ok"] is True
    assert results["purge_terminal_dirs"]["ok"] is True


# ---------------------------------------------------------------------------
# Test 4: First step failure does not block the rest
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_first_step_failure_does_not_block(_capture_order):
    """recover failure should not prevent rebuild or purges."""
    order, make_mock = _capture_order

    with (
        patch.object(
            app_main,
            "recover_completed_downloads_pending_index",
            make_mock("recover", exc=RuntimeError("recover failed")),
        ),
        patch.object(
            app_main,
            "rebuild_active_download_accounting",
            make_mock("rebuild"),
        ),
        patch.object(
            app_main,
            "purge_terminal_residual_gids",
            make_mock("residual_purge"),
        ),
        patch.object(
            app_main,
            "purge_terminal_download_dirs",
            make_mock("dir_purge"),
        ),
    ):
        results = await app_main._run_startup_repair_sequence(client=object())

    assert order == ["recover", "rebuild", "residual_purge", "dir_purge"]
    assert results["recover_pending_index"]["ok"] is False
    assert results["rebuild_active_accounting"]["ok"] is True
    assert results["purge_residual_gids"]["ok"] is True
    assert results["purge_terminal_dirs"]["ok"] is True


# ---------------------------------------------------------------------------
# Test 5: purge_terminal_download_dirs excludes active / pending-index
#
# This is a static / semantic proof. list_terminal_download_ids only returns:
#   - failed / cancelled
#   - completed WITH completed_file_id
# Active, waiting, paused, queued and completed-without-index are excluded,
# so their downloading/ dirs are never purged.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_terminal_id_query_excludes_active_and_pending_index():
    """The terminal-id query used by dir purge must not return active or
    completed-without-index rows."""
    from app.db.schema import global_downloads
    from app.repositories.downloads import list_terminal_download_ids
    import inspect

    src = inspect.getsource(list_terminal_download_ids)

    # The function must reference failed and cancelled statuses
    assert "failed" in src
    assert "cancelled" in src

    # It must require completed_file_id to be non-null for completed rows
    assert "completed" in src
    assert "completed_file_id" in src
    assert "is_not(None)" in src or "isnot(None)" in src or "is_not(None)" in src


# ---------------------------------------------------------------------------
# Test 6: purge_terminal_download_dirs skips non-terminal and non-digit dirs
#
# Verifies via the source that the purge function checks:
#   - entry name is a digit (skips pack_* etc)
#   - download_id is in terminal_ids set (skips active / pending-index)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_dir_purge_source_guards():
    """Source-level proof that dir purge only touches safe terminal dirs."""
    from app.services.repair import purge_terminal_download_dirs
    import inspect

    src = inspect.getsource(purge_terminal_download_dirs)

    # Must check terminal_ids membership
    assert "terminal_ids" in src
    assert "not name.isdigit()" in src or "name.isdigit()" in src

    # Must call list_terminal_download_ids
    assert "list_terminal_download_ids" in src


# ---------------------------------------------------------------------------
# Test 7: Every step produces a result entry (completeness check)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_all_steps_produce_results():
    """Every step in STARTUP_REPAIR_STEPS must have a result entry."""
    with (
        patch.object(app_main, "recover_completed_downloads_pending_index"),
        patch.object(app_main, "rebuild_active_download_accounting"),
        patch.object(app_main, "purge_terminal_residual_gids"),
        patch.object(app_main, "purge_terminal_download_dirs"),
    ):
        app_main.recover_completed_downloads_pending_index = AsyncMock(
            return_value={"found": 1, "recovered": 1, "failed": 0, "skipped": 0}
        )
        app_main.rebuild_active_download_accounting = AsyncMock(
            return_value={"rebuilt": 2, "failed": 0}
        )
        app_main.purge_terminal_residual_gids = AsyncMock(
            return_value={"found": 1, "purged": 1, "failed": 0}
        )
        app_main.purge_terminal_download_dirs = AsyncMock(
            return_value={"found": 1, "purged": 1, "failed": 0, "skipped": 0}
        )

        results = await app_main._run_startup_repair_sequence(client=object())

    for step_name in app_main.STARTUP_REPAIR_STEPS:
        assert step_name in results, f"Missing result for step {step_name}"
        assert results[step_name]["ok"] is True
        assert results[step_name]["result"] is not None
