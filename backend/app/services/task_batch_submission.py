"""批量任务提交 saga。

提供纯原语（planned GID）之上的批量 orchestration：
逐项校验/register、submission candidate 收集、一次 multicall、
success/fault/CAS/guarded global failure 映射，以及启动恢复
``recover_planned_submissions``。

不新建 manager/queue/schema；复用 Task Core register、
``assign_submitted_gid`` CAS、``claim_attempt_terminal`` guarded failure
和 lifecycle repair 的 HTTP ownership helper。
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from app.core.config import get_credential_pepper, get_internal_base_url
from app.domain.task_policy import legacy_rest_status
from app.modules.backend.aria2_adapter import SubmissionCall, build_submission_call
from app.modules.task_core.register import (
    RegisterError,
    RegisterResult,
    ResourceSpec,
    register,
)
from app.modules.task_core.states import (
    ERROR_ADMISSION_PAUSED,
    ERROR_METADATA_ADMISSION_PAUSED,
)
from app.modules.task_core.submit import resolve_submit_uri
from app.repositories.task.downloads import (
    assign_submitted_gid,
    claim_attempt_terminal,
    get_global_download_by_id,
    list_pending_submission_candidates,
)
from app.services import tracker_list_service
from app.services.gateway import http_resource_identity, source_request_options
from app.services.hash import (
    extract_info_hash_from_magnet,
    get_uri_hash,
    is_http_url,
    is_magnet_link,
)
from app.services.lifecycle.repair import _has_only_internal_gateway_uris
from app.services.storage import get_task_download_dir

logger = logging.getLogger(__name__)

_PLANNED_GID_DOMAIN = "aria2deck:planned-gid:v1"

# aria2 client MulticallOutcome 为结构兼容类型：仅约定 ok/result/fault_message
# 属性（services 层禁止直接 import app.aria2.client，也不得定义 Protocol 类）。
_MULTICALL_OUTCOME = Any


def derive_planned_gid(tid: int) -> str:
    """由 tid 确定性派生 aria2 planned GID（16 位小写 hex）。

    使用项目 credential pepper 的 domain-separated HMAC-SHA256，
    message 只含十进制 tid；崩溃后可由持久 tid 重新计算，无需迁移。
    """
    key = f"{_PLANNED_GID_DOMAIN}:{get_credential_pepper()}".encode()
    digest = hmac.new(key, str(tid).encode("ascii"), hashlib.sha256).hexdigest()
    return digest[:16]

__all__ = [
    "BatchAllowanceDeniedError",
    "BatchCreateResult",
    "BatchSubmissionUndeterminedError",
    "BatchTaskItem",
    "BatchTaskItemResult",
    "batch_create_tasks",
    "confirm_planned_submission",
    "derive_planned_gid",
    "list_pending_submission_candidates",
    "recover_planned_submissions",
]

SUBMISSION_FAILED_MESSAGE = "任务提交失败"
UNDETERMINED_MESSAGE = "aria2 批量提交结果暂无法确认"


class BatchSubmissionUndeterminedError(Exception):
    """两次 multicall 传输均失败：结果不可确认，候选保留给恢复/清理。"""


class BatchAllowanceDeniedError(Exception):
    """router 注入的 per-item create_task 限流拒绝（HTTP 429）。"""


RATE_LIMITED_MESSAGE = "操作过于频繁，请稍后再试"


@dataclass(frozen=True)
class BatchTaskItem:
    uri: str
    options: dict[str, Any] | None = None


@dataclass
class BatchTaskItemResult:
    input_index: int
    accepted: bool = False
    status: str | None = None
    task_id: int | None = None
    global_download_id: int | None = None
    error_code: str | None = None
    error_message: str | None = None


@dataclass(frozen=True)
class BatchCreateResult:
    results: list[BatchTaskItemResult] = field(default_factory=list)

    @property
    def accepted_count(self) -> int:
        return sum(1 for item in self.results if item.accepted)

    @property
    def failed_count(self) -> int:
        return sum(1 for item in self.results if not item.accepted)


# ---------------------------------------------------------------------------
# 逐项校验与 ResourceSpec 构建


# gateway/adapter 真实支持的用户级 HTTP 选项；bt-tracker / select-file
# 是服务端专属选项，不允许出现在请求 options 中。
# max-connection-per-server 在 adapter _ALLOWED_USER_OPTIONS 中：magnet
# 路径透传给 aria2；HTTP 路径由 gateway 固定覆盖。
_ALLOWED_REQUEST_OPTIONS = frozenset(
    {
        "out",
        "header",
        "http-user",
        "http-passwd",
        "mirrors",
        "max-connection-per-server",
    }
)
_SERVER_ONLY_OPTIONS = {
    "bt-tracker": "不允许使用 bt-tracker 选项",
    "select-file": "不允许使用 select-file 选项",
}


def _invalid_uri(message: str) -> tuple[str, str]:
    return ("invalid_uri", message)


def _validate_item(
    uri: str, options: dict[str, Any] | None
) -> tuple[str, str] | None:
    """返回 (error_code, 中文 message) 或 None；不做任何网络 probe。"""
    if not uri:
        return _invalid_uri("下载链接不能为空")
    if is_magnet_link(uri):
        if not extract_info_hash_from_magnet(uri):
            return _invalid_uri("无效的磁力链接")
    elif not is_http_url(uri):
        return _invalid_uri("仅支持磁力链接和 HTTP(S) 下载链接")
    if options:
        for key in options:
            if key in _SERVER_ONLY_OPTIONS:
                return ("invalid_option", _SERVER_ONLY_OPTIONS[key])
        unknown = sorted(k for k in options if k not in _ALLOWED_REQUEST_OPTIONS)
        if unknown:
            return ("invalid_option", f"不支持的选项: {unknown[0]}")
        if "out" in options:
            out = str(options["out"])
            if not out or out in {".", ".."} or "/" in out or "\\" in out:
                return ("invalid_option", "无效的 out 选项：必须是不含路径分隔符的文件名")
        try:
            source_request_options(options)
        except ValueError as exc:
            message = str(exc) or "HTTP 选项无效"
            return ("invalid_option", message if _is_chinese(message) else "HTTP 选项无效")
    return None


def _is_chinese(message: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in message)


def _display_name(uri: str) -> str | None:
    if is_http_url(uri):
        path = uri.split("?", 1)[0].rsplit("/", 1)[-1]
        return path or None
    return None


def _build_resource_spec(uri: str, options: dict[str, Any] | None) -> ResourceSpec:
    if is_magnet_link(uri):
        info_hash = extract_info_hash_from_magnet(uri) or ""
        canonical = f"magnet:?xt=urn:btih:{info_hash}"
        return ResourceSpec(
            resource_key=info_hash,
            source_uri=canonical,
            resource_kind="magnet",
            source_payload=canonical,
            source_options=options,
        )
    source_opts = source_request_options(options)
    resource_key = http_resource_identity(get_uri_hash(uri) or uri, source_opts)
    return ResourceSpec(
        resource_key=resource_key,
        source_uri=uri,
        resource_kind="http",
        display_name=_display_name(uri),
        source_payload=uri,
        source_options=options,
    )


# ---------------------------------------------------------------------------
# Planned GID ownership 验证（Spec §5.3）


def _is_missing_gid_fault(outcome: _MULTICALL_OUTCOME) -> bool:
    return "not found" in (outcome.fault_message or "").lower()


def _uris_contain_magnet(uris: Any, info_hash: str) -> bool:
    if not isinstance(uris, list) or not uris:
        return False
    for item in uris:
        uri = item.get("uri") if isinstance(item, dict) else None
        if isinstance(uri, str) and extract_info_hash_from_magnet(uri) == info_hash:
            return True
    return False


def _classify_magnet_ownership(
    *,
    tid: int,
    info_hash: str,
    status_value: Any,
    uris_value: Any,
) -> bool:
    if not isinstance(status_value, dict):
        return False
    if str(status_value.get("dir") or "") != str(get_task_download_dir(tid)):
        return False
    top_hash = str(status_value.get("infoHash") or "")
    if top_hash:
        return top_hash.lower() == info_hash.lower()
    followed = status_value.get("followedBy")
    if followed:
        return False  # 证据矛盾：已生成 payload 但顶层无 hash
    # 明确 metadata_not_ready：还必须 getUris 含 canonical magnet
    return _uris_contain_magnet(uris_value, info_hash)


def _classify_http_ownership(
    *,
    tid: int,
    uris_value: Any,
) -> bool:
    return _has_only_internal_gateway_uris(
        uris_value,
        internal_base=get_internal_base_url(),
        download_id=tid,
    )


async def _candidate_info_hash(download: Mapping[str, Any]) -> str:
    payload = await resolve_submit_uri(dict(download))
    return extract_info_hash_from_magnet(payload) or ""


def _initial_confirm_state(resource_kind: str) -> tuple[str, str | None]:
    if resource_kind == "magnet":
        return "paused", ERROR_METADATA_ADMISSION_PAUSED
    return "paused", ERROR_ADMISSION_PAUSED


async def confirm_planned_submission(download: Mapping[str, Any]) -> bool:
    """把 ownership 已验证的 planned GID 绑定到 queued/gid NULL 尝试。

    CAS 失败时重读：另一并发请求已绑定同一 gid 视为幂等成功。
    """
    tid = int(download["id"])
    gid = derive_planned_gid(tid)
    status, error_code = _initial_confirm_state(
        str(download.get("resource_kind") or "")
    )
    row = await assign_submitted_gid(
        download_id=tid, gid=gid, status=status, error_code=error_code
    )
    if row is not None:
        logger.info(
            "planned submission confirmed tid=%s gid=%s kind=%s",
            tid,
            gid,
            download.get("resource_kind"),
        )
        return True
    current = await get_global_download_by_id(tid)
    return bool(current and current.get("aria2_gid") == gid)


async def _remove_gid_best_effort(client: Any, gid: str) -> None:
    try:
        await client.remove(gid)
    except Exception:
        logger.debug("best-effort remove planned gid 失败 gid=%s", gid, exc_info=True)


async def _fail_pending_submission_attempt(tid: int) -> str:
    """Guarded global failure：仅在 queued+gid NULL 下终结并释放订阅者。

    返回 "failed"（已终结）或重读后的 "accepted"/"failed"（CAS 不匹配）。
    """
    claim = await claim_attempt_terminal(
        attempt_id=tid,
        expected_gid=None,
        terminal_status="failed",
        error_code="submission_failed",
        error_message=SUBMISSION_FAILED_MESSAGE,
        expected_statuses=("queued",),
    )
    if claim is not None:
        return "failed"
    current = await get_global_download_by_id(tid)
    if current and current.get("aria2_gid"):
        return "accepted"
    return "failed"


# ---------------------------------------------------------------------------
# 批量 saga


@dataclass
class _Candidate:
    tid: int
    planned_gid: str
    options: dict[str, Any]
    indexes: list[int]
    download: dict[str, Any] | None = None
    call: SubmissionCall | None = None


def _reconcile_calls(candidates: list[_Candidate]) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []
    for candidate in candidates:
        calls.append(
            {
                "methodName": "aria2.tellStatus",
                "params": [candidate.planned_gid],
            }
        )
        calls.append(
            {
                "methodName": "aria2.getUris",
                "params": [candidate.planned_gid],
            }
        )
    return calls


async def _resolve_by_reconciliation(
    *,
    client: Any,
    candidates: list[_Candidate],
    results: list[BatchTaskItemResult],
) -> None:
    """一次 reconciliation multicall：owned 确认 / 明确缺失终结。

    任何候选仍保留 queued/gid NULL（非缺失 fault、ownership 不足、
    CAS 未确认）即结果不可确认：整个请求 502，DB 保持原状。
    """
    outcomes = await client.multicall(_reconcile_calls(candidates))
    undetermined = False
    for index, candidate in enumerate(candidates):
        if 2 * index + 1 >= len(outcomes):
            undetermined = True  # shape 不符：fail closed 保留
            break
        status_o, uris_o = outcomes[2 * index], outcomes[2 * index + 1]
        verdict = await _reconcile_candidate(
            candidate, status_o=status_o, uris_o=uris_o
        )
        if verdict == "kept":
            undetermined = True
        _apply_candidate_verdict(candidate, results, verdict)
    if undetermined:
        raise BatchSubmissionUndeterminedError(UNDETERMINED_MESSAGE)


async def _reconcile_candidate(
    candidate: _Candidate,
    *,
    status_o: _MULTICALL_OUTCOME,
    uris_o: _MULTICALL_OUTCOME,
    fail_on_missing: bool = True,
) -> str:
    download = candidate.download
    if download is None:
        raise RuntimeError("批量提交候选缺少下载记录")
    if not status_o.ok:
        if _is_missing_gid_fault(status_o):
            if not fail_on_missing:
                # 启动恢复：明确 not found 不终结，保留 queued/gid NULL
                # 交给 300 秒 submit_timeout stale cleanup（防 fencing 竞态）。
                return "kept"
            outcome = await _fail_pending_submission_attempt(candidate.tid)
            return "failed" if outcome == "failed" else "accepted"
        # 非明确缺失：不可确认，保留给恢复/清理
        return "kept"
    kind = str(download.get("resource_kind") or "")
    if kind == "magnet":
        info_hash = await _candidate_info_hash(download)
        owned = _classify_magnet_ownership(
            tid=candidate.tid,
            info_hash=info_hash,
            status_value=status_o.result,
            uris_value=uris_o.result if uris_o.ok else None,
        )
    else:
        owned = _classify_http_ownership(
            tid=candidate.tid,
            uris_value=uris_o.result if uris_o.ok else None,
        )
    if owned:
        return "accepted" if await confirm_planned_submission(download) else "kept"
    logger.warning(
        "planned gid ownership rejected tid=%s gid=%s kind=%s",
        candidate.tid,
        candidate.planned_gid,
        kind,
    )
    return "kept"


def _apply_candidate_verdict(
    candidate: _Candidate,
    results: list[BatchTaskItemResult],
    verdict: str,
) -> None:
    if verdict == "accepted":
        for index in candidate.indexes:
            item = results[index]
            item.accepted = True
            item.status = "paused"
        return
    if verdict == "failed":
        for index in candidate.indexes:
            item = results[index]
            item.error_code = item.error_code or "submission_failed"
            item.error_message = item.error_message or SUBMISSION_FAILED_MESSAGE
            item.status = "failed"
        return
    # kept：不确定，保留 queued/gid NULL 给恢复/清理；逐项报告提交失败
    for index in candidate.indexes:
        item = results[index]
        item.error_code = item.error_code or "submission_failed"
        item.error_message = item.error_message or SUBMISSION_FAILED_MESSAGE


async def batch_create_tasks(
    *,
    user_id: int,
    quota_bytes: int,
    items: list[BatchTaskItem],
    client: Any,
    allow_create_task: Callable[[], Awaitable[None]] | None = None,
) -> BatchCreateResult:
    results: list[BatchTaskItemResult] = []
    seen_uris: dict[str, int] = {}
    candidates_by_tid: dict[int, _Candidate] = {}

    for raw_index, item in enumerate(items):
        uri = item.uri.strip()
        # 去重键只按 trim 后完整 URI 字符串；相同 infohash 的不同
        # magnet 文本不在此折叠，后续 register 可返回 duplicate。
        if uri in seen_uris:
            continue  # 去重：结果只含首次项，首次 options 生效
        seen_uris[uri] = len(results)
        results.append(BatchTaskItemResult(input_index=raw_index))
        result_index = len(results) - 1

        if allow_create_task is not None:
            try:
                await allow_create_task()
            except BatchAllowanceDeniedError:
                current = results[result_index]
                current.error_code = "rate_limited"
                current.error_message = RATE_LIMITED_MESSAGE
                continue

        error = _validate_item(uri, item.options)
        if error is not None:
            current = results[result_index]
            current.error_code = error[0]
            current.error_message = error[1]
            continue

        try:
            register_result = await register(
                user_id=user_id,
                quota_bytes=quota_bytes,
                resource=_build_resource_spec(uri, item.options),
            )
        except RegisterError as exc:
            current = results[result_index]
            current.error_code = exc.code
            current.error_message = str(exc)
            continue

        _collect_outcome(
            register_result,
            item.options or {},
            results,
            result_index,
            candidates_by_tid,
        )

    await _submit_candidates(client, list(candidates_by_tid.values()), results)
    return BatchCreateResult(results=results)


def _collect_outcome(
    register_result: RegisterResult,
    options: dict[str, Any],
    results: list[BatchTaskItemResult],
    result_index: int,
    candidates_by_tid: dict[int, _Candidate],
) -> None:
    item = results[result_index]
    item.task_id = register_result.pid
    item.global_download_id = register_result.tid
    outcome = register_result.outcome
    if outcome == "attached_completed":
        item.accepted = True
        item.status = legacy_rest_status(register_result.status)
        return
    if outcome == "joined_live":
        # gid 已绑定的 live global 无需 RPC（在提交阶段读取行后分流）
        item.status = register_result.status
    candidate = candidates_by_tid.get(register_result.tid)
    if candidate is None:
        candidate = _Candidate(
            tid=register_result.tid,
            planned_gid=derive_planned_gid(register_result.tid),
            options=dict(options),
            indexes=[],
        )
        candidates_by_tid[register_result.tid] = candidate
    candidate.indexes.append(result_index)


async def _submit_candidates(
    client: Any,
    candidates: list[_Candidate],
    results: list[BatchTaskItemResult],
) -> None:
    if not candidates:
        return
    calls: list[dict[str, Any]] = []
    for candidate in candidates:
        download = await get_global_download_by_id(candidate.tid)
        if download is None:
            _apply_candidate_verdict(candidate, results, "failed")
            continue
        candidate.download = download
        if download.get("aria2_gid"):
            # joined_live + gid：现有 global 已可靠提交，直接 accepted
            _apply_candidate_verdict(candidate, results, "accepted")
            for index in candidate.indexes:
                results[index].status = str(download["status"])
            continue
        submit_options = dict(candidate.options)
        if str(download.get("resource_kind") or "") in ("magnet", "torrent"):
            bt_tracker = tracker_list_service.get_bt_tracker_option()
            if bt_tracker is not None:
                submit_options["bt-tracker"] = bt_tracker
        uri = await resolve_submit_uri(download)
        candidate.call = build_submission_call(
            download,
            uri=uri,
            options=submit_options,
            planned_gid=candidate.planned_gid,
        )
        calls.append(
            {"methodName": candidate.call.method, "params": candidate.call.params}
        )

    if not calls:
        return
    try:
        outcomes = await client.multicall(calls)
    except Exception as exc:  # noqa: BLE001  # external boundary preserves failure isolation
        logger.warning(
            "multicall 传输失败 call_count=%s error_type=%s",
            len(calls),
            type(exc).__name__,
        )
        try:
            await _resolve_by_reconciliation(
                client=client, candidates=candidates, results=results
            )
        except Exception:  # noqa: BLE001  # external boundary preserves failure isolation
            logger.warning("reconciliation multicall 失败，结果不可确认")
            raise BatchSubmissionUndeterminedError(UNDETERMINED_MESSAGE) from exc
        return

    faulted: list[_Candidate] = []
    submitted = [
        (candidate, candidate.call)
        for candidate in candidates
        if candidate.call is not None
    ]
    for (candidate, call), outcome in zip(submitted, outcomes):
        if outcome.ok:
            returned_gid = str(outcome.result)
            if returned_gid == candidate.planned_gid:
                await _confirm_success(client, candidate, call, results)
            else:
                logger.warning(
                    "gid mismatch tid=%s planned=%s returned=%s",
                    candidate.tid,
                    candidate.planned_gid,
                    returned_gid,
                )
                await _remove_gid_best_effort(client, returned_gid)
                for index in candidate.indexes:
                    results[index].error_code = "gid_mismatch"
                    results[index].error_message = "aria2 返回的 GID 与预期不一致"
            continue
        faulted.append(candidate)

    if faulted:
        try:
            await _resolve_by_reconciliation(
                client=client, candidates=faulted, results=results
            )
        except Exception:  # noqa: BLE001  # external boundary preserves failure isolation
            raise BatchSubmissionUndeterminedError(UNDETERMINED_MESSAGE)


async def _confirm_success(
    client: Any,
    candidate: _Candidate,
    call: SubmissionCall,
    results: list[BatchTaskItemResult],
) -> None:
    row = await assign_submitted_gid(
        download_id=candidate.tid,
        gid=candidate.planned_gid,
        status=call.status,
        error_code=call.error_code,
    )
    if row is not None:
        _apply_candidate_verdict(candidate, results, "accepted")
        for index in candidate.indexes:
            results[index].status = call.status
        return
    current = await get_global_download_by_id(candidate.tid)
    if current and current.get("aria2_gid") == candidate.planned_gid:
        _apply_candidate_verdict(candidate, results, "accepted")
        for index in candidate.indexes:
            results[index].status = call.status
        return
    # CAS 被取消/终态抢先：best-effort remove planned gid，保留先到终态
    await _remove_gid_best_effort(client, candidate.planned_gid)
    for index in candidate.indexes:
        results[index].error_code = results[index].error_code or "cancelled"
        results[index].error_message = results[index].error_message or "任务已取消"
    _apply_candidate_verdict(candidate, results, "failed")


# ---------------------------------------------------------------------------
# 启动恢复（Spec §5.3）


async def recover_planned_submissions(client: Any) -> set[str]:
    """启动期批量核对 queued/gid NULL 候选；返回本轮 unresolved planned GID。

    不抛出：传输错误时 fail closed，全部候选保持 unresolved。
    明确 not found / ownership rejected 的候选不在此终结，交给 300 秒
    ``submit_timeout`` stale cleanup。
    """
    rows = await list_pending_submission_candidates()
    unresolved = {derive_planned_gid(int(row["id"])) for row in rows}
    if not rows:
        return unresolved
    candidates = [
        _Candidate(
            tid=int(row["id"]),
            planned_gid=derive_planned_gid(int(row["id"])),
            options={},
            indexes=[],
            download=dict(row),
        )
        for row in rows
    ]
    try:
        outcomes = await client.multicall(_reconcile_calls(candidates))
    except Exception:
        logger.warning(
            "startup planned recovery transport failed candidates=%s",
            len(candidates),
            exc_info=True,
        )
        return unresolved
    recovered = 0
    for index, candidate in enumerate(candidates):
        if 2 * index + 1 >= len(outcomes):
            break  # shape 不符：fail closed 保留
        verdict = await _reconcile_candidate(
            candidate,
            status_o=outcomes[2 * index],
            uris_o=outcomes[2 * index + 1],
            fail_on_missing=False,
        )
        if verdict == "accepted":
            unresolved.discard(candidate.planned_gid)
            recovered += 1
    logger.info(
        "startup planned recovery candidates=%s recovered=%s unresolved=%s",
        len(candidates),
        recovered,
        len(unresolved),
    )
    return unresolved
