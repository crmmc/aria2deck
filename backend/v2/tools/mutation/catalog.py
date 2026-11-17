"""变异清单：每条变异都是一处行为缺陷，应当被现有测试发现。

设计约束（来自实测教训）：

1. `rationale` 必填。审查变异时要能回答「为什么这处改动应该被测试发现」。
   普查中有 4 个变异因为改了不影响结果的位置而无效，`rationale` 是防止重犯的手段。
2. 每条变异修改后，相关测试必须变红（KILLED）。不变红说明测试有缺口。
3. 不改函数名、不改冗余防御、不改未被调用的分支——那些不改变可观测行为，杀不死没有意义。
4. `line` 可选。同一个 `old` 在文件里出现多次时必须指定行号（1-based），
   否则运行器拒绝执行该条，避免改错地方。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Mutant:
    name: str
    path: str
    old: str
    new: str
    tests: tuple[str, ...]
    rationale: str
    line: int | None = None


MUTANTS: tuple[Mutant, ...] = (
    Mutant(
        name="files分页上界放开",
        path="src/aria2deck/api/files.py",
        old="    page_size: int = Query(default=20, ge=1, le=100),",
        new="    page_size: int = Query(default=20, ge=1, le=10**9),",
        tests=("tests/api/test_files.py",),
        rationale=(
            "分页上界是输入校验：放宽后 page_size=101 应被拒绝却会被接受。"
            "同文件出现两处相同文本，必须按行号定位到 list_files。"
        ),
        line=98,
    ),
    Mutant(
        name="文件搜索truncated反转",
        path="src/aria2deck/api/files.py",
        old="            truncated=truncated,",
        new="            truncated=not truncated,",
        tests=("tests/api/test_files.py",),
        rationale="搜索结果截断标记反转会误导前端分页，测试断言了 truncated is False。",
    ),
    Mutant(
        name="匿名放行",
        path="src/aria2deck/api/dependencies.py",
        old='        raise DomainError(ErrorCode.UNAUTHENTICATED, "请先登录")',
        new="        return None  # type: ignore[return-value]",
        tests=("tests/api/test_auth.py", "tests/api/test_files.py"),
        rationale=(
            "认证守卫放行匿名是越权风险最高的一处��删除后未认证请求应返回 401 却会进入业务逻辑。"
        ),
    ),
    Mutant(
        name="请求体上限放宽10倍",
        path="src/aria2deck/api/request_body_limit.py",
        old="MAX_HTTP_REQUEST_BODY_BYTES = 16 * 1024 * 1024",
        new="MAX_HTTP_REQUEST_BODY_BYTES = 160 * 1024 * 1024",
        tests=("tests/api/test_request_body_limit.py",),
        rationale="16MiB 上限是资源保护边界，放宽后超限请求会被接受而非 413。",
    ),
    Mutant(
        name="分享密码校验恒真",
        path="src/aria2deck/domain/shares.py",
        old="    if not verify_share_password(share_code, password, expected_hash):",
        new="    if False:",
        tests=("tests/domain/test_shares.py", "tests/api/test_public_shares.py"),
        rationale="密码校验失效会让任何人访问受保护分享，错误密码应返回 401 却会放行。",
    ),
    Mutant(
        name="分享过期判定恒假",
        path="src/aria2deck/domain/shares.py",
        old=(
            "    if share.expires_at_ms is not None and now_ms >= share.expires_at_ms:\n"
            "        return ShareStatus.EXPIRED"
        ),
        new="    if False:\n        return ShareStatus.EXPIRED",
        tests=("tests/domain/test_shares.py", "tests/api/test_public_shares.py"),
        rationale="过期分享应拒绝访问；判定失效后过期链接永久有效。",
    ),
    Mutant(
        name="配额不足不拦截",
        path="src/aria2deck/infrastructure/sqlite/repositories/quota.py",
        old='            raise DomainError(ErrorCode.QUOTA_EXCEEDED, "用户配额不足")',
        new="            pass",
        tests=("tests/application", "tests/api"),
        rationale="配额是资源公平性边界；不拦截会导致无限占用磁盘。",
    ),
    Mutant(
        name="取消失败仍标记确认",
        path="src/aria2deck/application/downloads/commands.py",
        old=(
            "                        except DomainError:\n"
            "                            stop_confirmed = False"
        ),
        new=(
            "                        except DomainError:\n"
            "                            stop_confirmed = True"
        ),
        tests=("tests/application/test_download_command_edges.py", "tests/api/test_downloads.py"),
        rationale=(
            "停止失败必须如实上报；谎报确认会让上层误以为资源已释放，"
            "测试断言了 result.stop_confirmed is False。"
        ),
    ),
    Mutant(
        name="lifecycle停止失败不写uncertain",
        path="src/aria2deck/application/downloads/lifecycle.py",
        old="                await self.repository_factory(uow).mark_stop_uncertain(attempt.id)",
        new="                pass",
        tests=("tests/application/test_download_lifecycle.py",),
        rationale=(
            "lifecycle 的 _stop_failed 没有 cancel_subscription 兜底，"
            "删除后 attempt 会停在错误状态，恢复逻辑将无法重试。"
        ),
    ),
    Mutant(
        name="改名同名冲突不检测",
        path="src/aria2deck/infrastructure/sqlite/repositories/content.py",
        old='        raise DomainError(ErrorCode.DUPLICATE_RESOURCE, "同名文件或活动任务已存在")',
        new="            pass",
        tests=("tests/api/test_files.py", "tests/infrastructure/sqlite/test_sqlite_content.py"),
        rationale="同名冲突放行会造成用户文件重名，后续按名访问产生歧义。",
    ),
    Mutant(
        name="分页状态过滤漏掉终态分支",
        path="src/aria2deck/infrastructure/sqlite/repositories/downloads.py",
        old="                _EFFECTIVE_TASK_STATUS.in_([status.value for status in statuses])",
        new="                task_subscriptions.c.status.in_([status.value for status in statuses])",
        tests=("tests/infrastructure/sqlite/test_sqlite_downloads.py",),
        rationale=(
            "分页下推用有效状态（订阅终态 + attempt 终态映射）过滤；"
            "只用订阅态会漏掉 attempt 已终态但订阅态未同步的任务，"
            "与全量投影后过滤的语义不一致。"
        ),
    ),
    Mutant(
        name="分页total多算一条",
        path="src/aria2deck/infrastructure/sqlite/repositories/downloads.py",
        old="        return [self._task(row) for row in result.mappings().all()], total",
        new="        return [self._task(row) for row in result.mappings().all()], total + 1",
        tests=("tests/infrastructure/sqlite/test_sqlite_downloads.py",),
        rationale="分页 total 直接影响前端页数；多算一条会让最后一页出现空页。",
    ),
)

# 已排除的变异（普查中验证为无效，不收录）。保留��明��免下一个人重新踩坑。
EXCLUDED: tuple[tuple[str, str], ...] = (
    ("改函数名", "只改名字未改行为，测试自然不受影响"),
    (
        "路径穿越 any(part in {'', '.', '..'}) → if False",
        "同函数 path.startswith('/')、反斜杠、NUL 检查仍在拦截，属冗余防御",
    ),
    (
        "admit_size 的 TASK_TOO_LARGE → INVALID_INPUT",
        "同函数比较逻辑未变，且测试断言 HTTP 413 + error.code，另一处路径生效",
    ),
    ("给 page_tasks 的 user_id 加默认值", "未改变任何调用行为"),
    (
        "commands.py 的 mark_stop_uncertain 删除",
        "cancel_subscription 内部已写 submission_state='uncertain'，该调用是冗余写入，"
        "删除不改变可观测行为。真正不可替代的调用点在 lifecycle.py，已单列为独立变异",
    ),
)
