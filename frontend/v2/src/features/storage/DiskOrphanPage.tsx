"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, Navigate } from "react-router-dom";

import { errorMessage } from "@/shared/api/client";
import type {
  DiskOrphanItemResponse,
  QueueItemResponse,
  QueueSnapshotResponse,
  QueueState,
} from "@/shared/api/types";
import { useAuth } from "@/shared/auth/hooks";
import { BatchFailureDialog, type BatchFailure } from "@/shared/components/BatchFailureDialog";
import { DialogShell } from "@/shared/components/DialogShell";
import { EmptyState } from "@/shared/components/EmptyState";
import { Icon, type IconName } from "@/shared/components/Icon";
import { PageHeader } from "@/shared/components/PageHeader";
import { PaginationControls } from "@/shared/components/PaginationControls";
import { ToolbarSearchInput } from "@/shared/components/Toolbar";
import { useToast } from "@/shared/components/toast-context";
import { useSelection } from "@/shared/hooks/useSelection";
import { formatAdminBytes, formatAdminDateTime } from "@/shared/lib/format";
import { requestModalClose } from "@/shared/ui/modalClose";

import {
  deleteDiskOrphans,
  enqueueDiskOrphans,
  fetchAdmissionQueue,
  fetchDiskOrphans,
  startAdmissionQueue,
  stopAdmissionQueue,
} from "./api";

const PAGE_SIZE = 20;
const SEARCH_DEBOUNCE_MS = 300;
const QUEUE_POLL_INTERVAL_MS = 2000;
const EMPTY_PATHS: string[] = [];

const QUEUE_STATE_TEXT: Record<QueueState, string> = {
  queued: "排队中",
  running: "处理中",
  done: "已入库",
  failed: "失败",
};

const QUEUE_STATE_ICON: Record<QueueState, IconName> = {
  queued: "clock",
  running: "refresh",
  done: "check",
  failed: "alert",
};

const QUEUE_STATE_CLASS: Record<QueueState, string> = {
  queued: "status-badge",
  running: "status-badge status-active",
  done: "status-badge status-success",
  failed: "status-badge status-danger",
};

type DeleteTarget = {
  storagePath: string;
  size: number;
  state: QueueState | null;
};

function progressPercent(item: QueueItemResponse): number {
  if (item.size <= 0) return 0;
  return Math.min(100, Math.round((item.processed_bytes / item.size) * 100));
}

/** 立即释放字节与「已受理、稍后完成」是两种结果，不能合并成一句「已删除」。 */
function deleteToastText(immediate: number, deferred: number, freedBytes: number): string {
  const parts: string[] = [];
  if (immediate > 0) parts.push(`已删除 ${immediate} 项，释放 ${formatAdminBytes(freedBytes)}`);
  if (deferred > 0) parts.push(`已受理 ${deferred} 项删除，稍后完成`);
  return parts.join("；");
}

export function DiskOrphanPage() {
  const { user } = useAuth();
  const { showToast } = useToast();

  const [queue, setQueue] = useState<QueueSnapshotResponse | null>(null);
  const [queueBusy, setQueueBusy] = useState(false);

  const [page, setPage] = useState(1);
  const [orphans, setOrphans] = useState<DiskOrphanItemResponse[]>([]);
  const [orphanTotal, setOrphanTotal] = useState(0);
  const [unrecognizedCount, setUnrecognizedCount] = useState(0);
  const [scanning, setScanning] = useState(false);
  const [hasScanned, setHasScanned] = useState(false);

  const [searchKeyword, setSearchKeyword] = useState("");

  const { selected, setItemSelected, selectAll, clear } = useSelection<string>(EMPTY_PATHS);
  const [deleteTargets, setDeleteTargets] = useState<DeleteTarget[] | null>(null);
  const [deleting, setDeleting] = useState(false);
  const [enqueueing, setEnqueueing] = useState(false);
  const [batchFailures, setBatchFailures] = useState<BatchFailure[]>([]);

  const mountedRef = useRef(true);
  const scanRequestIdRef = useRef(0);
  const queueRequestIdRef = useRef(0);
  const donePathsRef = useRef<Set<string>>(new Set());
  // 扫描参数与「是否扫描过」各自保存在 ref 里，供事件处理器读到最新值，
  // 免得把整段加载逻辑绑在一堆 state 依赖上反复重建。
  const hasScannedRef = useRef(false);
  const pageRef = useRef(1);
  const keywordRef = useRef("");
  const selectAllRef = useRef<HTMLInputElement>(null);
  const deleteReturnRef = useRef<HTMLButtonElement>(null);

  const queueStateByPath = useMemo(() => {
    const map = new Map<string, QueueState>();
    queue?.items.forEach((item) => map.set(item.storage_path, item.state));
    return map;
  }, [queue]);

  hasScannedRef.current = hasScanned;
  pageRef.current = page;

  const currentPageSelectedCount = orphans.reduce(
    (count, entry) => count + (selected.has(entry.storage_path) ? 1 : 0),
    0,
  );
  const allCurrentPageSelected = orphans.length > 0 && currentPageSelectedCount === orphans.length;

  /** 按给定页码与关键词读一次磁盘清单；调用方负责在此之前把页码与关键词落到状态里。 */
  const runScan = useCallback(
    async (targetPage: number, keyword: string) => {
      const requestId = ++scanRequestIdRef.current;
      setScanning(true);
      try {
        const res = await fetchDiskOrphans(targetPage, PAGE_SIZE, keyword);
        if (!mountedRef.current || requestId !== scanRequestIdRef.current) return;
        const lastPage = Math.max(1, Math.ceil(res.total / res.page_size));
        // 当前页超出末页：回到实际末页重读，列表不能停在一页空白上。
        if (res.page > lastPage) {
          setPage(lastPage);
          void runScan(lastPage, keyword);
          return;
        }
        setOrphans(res.items);
        setOrphanTotal(res.total);
        setUnrecognizedCount(res.unrecognized_count);
        setHasScanned(true);
        clear();
      } catch (caught) {
        if (!mountedRef.current || requestId !== scanRequestIdRef.current) return;
        showToast(errorMessage(caught, "扫描未登记文件失败"), "error");
      } finally {
        if (mountedRef.current && requestId === scanRequestIdRef.current) setScanning(false);
      }
    },
    [clear, showToast],
  );

  /** 用当前页码与关键词重读：删除后与入库成功后都要让清单反映新的磁盘状态。 */
  const refreshOrphans = useCallback(() => {
    if (!hasScannedRef.current) return;
    void runScan(pageRef.current, keywordRef.current);
  }, [runScan]);

  const applySnapshot = useCallback(
    (snapshot: QueueSnapshotResponse) => {
      setQueue(snapshot);
      const donePaths = new Set(
        snapshot.items.filter((item) => item.state === "done").map((item) => item.storage_path),
      );
      const hasNewDone = [...donePaths].some((path) => !donePathsRef.current.has(path));
      donePathsRef.current = donePaths;
      // 入库成功的条目已不再是「未登记」，清单必须重读磁盘才能让它消失。
      if (hasNewDone) refreshOrphans();
    },
    [refreshOrphans],
  );

  const loadQueue = useCallback(
    async (silent = false) => {
      const requestId = ++queueRequestIdRef.current;
      try {
        const snapshot = await fetchAdmissionQueue();
        if (!mountedRef.current || requestId !== queueRequestIdRef.current) return;
        applySnapshot(snapshot);
      } catch (caught) {
        if (!mountedRef.current || requestId !== queueRequestIdRef.current || silent) return;
        showToast(errorMessage(caught, "加载入库队列失败"), "error");
      }
    },
    [applySnapshot, showToast],
  );

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      scanRequestIdRef.current += 1;
      queueRequestIdRef.current += 1;
    };
  }, []);

  useEffect(() => {
    if (user?.is_admin) void loadQueue();
  }, [user, loadQueue]);

  useEffect(() => {
    const timer = setTimeout(() => {
      const next = searchKeyword.trim();
      // 只对已扫描过的会话生效：没扫描过的页面不该因为输入就偷偷读磁盘。
      if (!hasScannedRef.current || keywordRef.current === next) return;
      keywordRef.current = next;
      setPage(1);
      setOrphans([]);
      void runScan(1, next);
    }, SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [searchKeyword, runScan]);

  useEffect(() => {
    if (selectAllRef.current) {
      selectAllRef.current.indeterminate = currentPageSelectedCount > 0 && !allCurrentPageSelected;
    }
  }, [allCurrentPageSelected, currentPageSelectedCount]);

  useEffect(() => {
    if (deleteTargets !== null) deleteReturnRef.current?.focus();
  }, [deleteTargets]);

  const queueRunning = queue?.running === true;
  useEffect(() => {
    if (!queueRunning || !user?.is_admin) return undefined;
    const interval = setInterval(() => {
      if (!document.hidden) void loadQueue(true);
    }, QUEUE_POLL_INTERVAL_MS);
    return () => clearInterval(interval);
  }, [queueRunning, user, loadQueue]);

  const handleSearchChange = (value: string) => {
    clear();
    setSearchKeyword(value);
    // 页码回到第 1 页由防抖后的扫描完成；这里只清选择，避免旧页的选择带到新条件上。
    if (!hasScannedRef.current) return;
    pageRef.current = 1;
    setPage(1);
  };

  const handleScan = () => {
    clear();
    const keyword = searchKeyword.trim();
    keywordRef.current = keyword;
    pageRef.current = 1;
    setPage(1);
    void runScan(1, keyword);
  };

  const handlePageChange = (nextPage: number) => {
    clear();
    pageRef.current = nextPage;
    setPage(nextPage);
    void runScan(nextPage, keywordRef.current);
  };

  const handleSelectAll = (checked: boolean) => {
    if (checked) {
      selectAll(orphans.map((entry) => entry.storage_path));
      return;
    }
    clear();
  };

  const openDeleteForSelection = () => {
    setDeleteTargets(
      orphans
        .filter((entry) => selected.has(entry.storage_path))
        .map((entry) => ({
          storagePath: entry.storage_path,
          size: entry.size,
          state: queueStateByPath.get(entry.storage_path) ?? null,
        })),
    );
  };

  const openDeleteForQueueItem = (item: QueueItemResponse) => {
    setDeleteTargets([{ storagePath: item.storage_path, size: item.size, state: item.state }]);
  };

  const handleDelete = async (trigger: Element, targets: DeleteTarget[]) => {
    setDeleting(true);
    try {
      const results = await deleteDiskOrphans(targets.map((target) => target.storagePath));
      if (!mountedRef.current) return;
      const failures = results
        .filter((result) => !result.accepted)
        .map((result) => ({
          label: result.storage_path,
          reason: result.error?.message ?? "未知错误",
        }));
      const acceptedResults = results.filter((result) => result.accepted);
      if (acceptedResults.length === 0) {
        setBatchFailures(failures.length > 0 ? failures : [{ label: "删除", reason: "删除失败" }]);
        showToast(failures[0]?.reason ?? "删除失败", "error");
        return;
      }
      const immediateCount = acceptedResults.filter((item) => item.mode === "immediate").length;
      const deferredCount = acceptedResults.filter((item) => item.mode === "deferred").length;
      const freedBytes = acceptedResults.reduce((sum, item) => sum + item.freed_bytes, 0);
      showToast(deleteToastText(immediateCount, deferredCount, freedBytes), "success");
      if (failures.length > 0) setBatchFailures(failures);
      clear();
      requestModalClose(trigger);
      refreshOrphans();
      void loadQueue(true);
    } catch (caught) {
      if (!mountedRef.current) return;
      showToast(errorMessage(caught, "删除失败"), "error");
    } finally {
      if (mountedRef.current) setDeleting(false);
    }
  };

  const handleEnqueue = async () => {
    // 按钮在零选中时已禁用，这里不必再挡一次空列表。
    const storagePaths = orphans
      .filter((entry) => selected.has(entry.storage_path))
      .map((entry) => entry.storage_path);
    setEnqueueing(true);
    try {
      const results = await enqueueDiskOrphans(storagePaths);
      if (!mountedRef.current) return;
      const acceptedCount = results.filter((result) => result.accepted).length;
      const failures = results
        .filter((result) => !result.accepted)
        .map((result) => ({
          label: result.storage_path,
          reason: result.error?.message ?? "未知错误",
        }));
      if (acceptedCount === 0) {
        setBatchFailures(failures);
        showToast(failures[0]?.reason ?? "加入队列失败", "error");
        return;
      }
      showToast(`已加入队列 ${acceptedCount} 项`, "success");
      if (failures.length > 0) setBatchFailures(failures);
      clear();
      void loadQueue(true);
    } catch (caught) {
      if (!mountedRef.current) return;
      showToast(errorMessage(caught, "加入队列失败"), "error");
    } finally {
      if (mountedRef.current) setEnqueueing(false);
    }
  };

  const handleToggleQueue = async () => {
    const nextRunning = !queueRunning;
    setQueueBusy(true);
    try {
      const snapshot = nextRunning ? await startAdmissionQueue() : await stopAdmissionQueue();
      if (!mountedRef.current) return;
      applySnapshot(snapshot);
      showToast(nextRunning ? "已开始处理入库队列" : "已停止处理，当前条目退回排队中", "info");
    } catch (caught) {
      if (!mountedRef.current) return;
      showToast(errorMessage(caught, nextRunning ? "开始处理失败" : "停止处理失败"), "error");
    } finally {
      if (mountedRef.current) setQueueBusy(false);
    }
  };

  if (user !== null && !user.is_admin) {
    return <Navigate to="/tasks" replace />;
  }
  if (!user?.is_admin) return null;

  const runningItem = queue?.items.find((item) => item.state === "running") ?? null;
  const pendingTargets = deleteTargets ?? [];
  const deleteImmediateBytes = pendingTargets
    .filter((target) => target.state !== "done")
    .reduce((sum, target) => sum + target.size, 0);
  const deleteDeferredCount = pendingTargets.filter((target) => target.state === "done").length;

  return (
    <>
      <div className="glass-frame">
        <PageHeader
          title="未登记文件"
          description="扫描下载目录中未登记进库的规范存储文件，按需校验入库"
        />
        <p className="muted mb-4">
          「未登记文件」是磁盘上有、库里没记录的对象；「孤立文件」是库里已登记但没有用户引用，可在
          <Link to="/storage">存储管理</Link> 里查看。
        </p>

        <section className="panel admin-table-panel mb-4" aria-label="入库队列">
          <div className="panel-head">
            <div>
              <h2>入库队列</h2>
              <p className="muted">开始处理后才逐条校验入库，一次一条，按加入顺序</p>
            </div>
            <div className="flex items-center gap-2">
              <span className="admin-result-count">共 {queue?.items.length ?? 0} 条</span>
              <span className="admin-result-count">排队中 {queue?.queued_count ?? 0}</span>
              <span className="admin-result-count">处理中 {queue?.running_count ?? 0}</span>
              <span className="admin-result-count">已入库 {queue?.done_count ?? 0}</span>
              <span className="admin-result-count">失败 {queue?.failed_count ?? 0}</span>
              <span className="admin-result-count">
                安排中 {formatAdminBytes(queue?.pending_bytes ?? 0)}
              </span>
              <button
                type="button"
                className={`button btn-sm ${queueRunning ? "secondary" : "primary"}`}
                disabled={queue === null || queueBusy}
                onClick={() => void handleToggleQueue()}
              >
                <Icon name={queueRunning ? "x" : "download"} className="icon-sm" />
                {queueRunning ? "停止处理" : "开始处理"}
              </button>
            </div>
          </div>

          {runningItem !== null && (
            <div className="panel-body">
              <div className="progress-meta">
                <span>正在校验 {runningItem.storage_path}</span>
                <span>
                  {formatAdminBytes(runningItem.processed_bytes)} /{" "}
                  {formatAdminBytes(runningItem.size)}
                </span>
              </div>
              <div
                className="progress-container progress-track"
                role="progressbar"
                aria-label="当前条目校验进度"
                aria-valuemin={0}
                aria-valuemax={runningItem.size}
                aria-valuenow={runningItem.processed_bytes}
              >
                <div
                  className="progress-bar progress-bar-primary"
                  style={{ width: `${progressPercent(runningItem)}%` }}
                />
              </div>
            </div>
          )}

          <div className="admin-table-scroll">
            <table className="admin-table admin-orphan-queue-table">
              <thead>
                <tr>
                  <th>路径</th>
                  <th>状态</th>
                  <th>大小</th>
                  <th>说明</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                {queue?.items.map((item) => (
                  <tr key={item.storage_path} data-testid={`queue-row-${item.storage_path}`}>
                    <td className="admin-name-cell" title={item.storage_path}>
                      {item.storage_path}
                    </td>
                    <td>
                      <span className={QUEUE_STATE_CLASS[item.state]}>
                        <Icon name={QUEUE_STATE_ICON[item.state]} className="icon-sm" />
                        {QUEUE_STATE_TEXT[item.state]}
                      </span>
                    </td>
                    <td>{formatAdminBytes(item.size)}</td>
                    <td>
                      {item.state === "failed"
                        ? (item.error?.message ?? "未知错误")
                        : item.state === "done" && item.content_id !== null
                          ? `已登记为内容 #${item.content_id}`
                          : "—"}
                    </td>
                    <td>
                      <button
                        type="button"
                        className="button danger btn-sm"
                        disabled={item.state === "running" || deleting}
                        title={item.state === "running" ? "正在校验中，请先停止处理" : undefined}
                        aria-label={`删除 ${item.storage_path}`}
                        onClick={() => openDeleteForQueueItem(item)}
                      >
                        <Icon name="trash" className="icon-sm" />删除
                      </button>
                    </td>
                  </tr>
                ))}
                {queue === null && (
                  <tr>
                    <td colSpan={5}>
                      <p className="muted" role="status">加载中...</p>
                    </td>
                  </tr>
                )}
                {queue !== null && queue.items.length === 0 && (
                  <tr>
                    <td colSpan={5}>
                      <EmptyState
                        icon={<Icon name="tasks" className="icon-lg" />}
                        title="入库队列为空"
                        description="在下方磁盘清单里勾选条目后点「加入队列」。"
                      />
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </section>

        <section className="panel admin-table-panel" aria-label="磁盘未登记文件清单">
          <div className="panel-head">
            <div>
              <h2>磁盘未登记文件</h2>
              <p className="muted">只读取目录项与文件元数据，不读取内容、不计算哈希</p>
            </div>
          </div>

          <div className="toolbar admin-storage-toolbar">
            <label className="toolbar-label" htmlFor="disk-orphan-search">按存储路径过滤</label>
            <ToolbarSearchInput
              id="disk-orphan-search"
              value={searchKeyword}
              placeholder="按存储路径过滤"
              ariaLabel="按存储路径过滤"
              clearAriaLabel="清空存储路径过滤"
              onChange={handleSearchChange}
            />
            <div className="toolbar-spacer" />
            <button
              type="button"
              className="button secondary btn-sm"
              disabled={scanning}
              onClick={handleScan}
            >
              <Icon name="refresh" className="icon-sm" />
              {scanning ? "扫描中..." : "扫描"}
            </button>
            <button
              type="button"
              className="button secondary btn-sm"
              disabled={currentPageSelectedCount === 0 || enqueueing}
              onClick={() => void handleEnqueue()}
            >
              <Icon name="plus" className="icon-sm" />加入队列({currentPageSelectedCount})
            </button>
            <button
              type="button"
              className="button danger btn-sm"
              disabled={currentPageSelectedCount === 0 || deleting}
              onClick={openDeleteForSelection}
            >
              <Icon name="trash" className="icon-sm" />删除选中({currentPageSelectedCount})
            </button>
          </div>

          <div className="admin-table-scroll">
            <table className="admin-table admin-orphan-table">
              <thead>
                <tr>
                  <th className="admin-check-cell">
                    <input
                      ref={selectAllRef}
                      type="checkbox"
                      checked={allCurrentPageSelected}
                      disabled={orphans.length === 0}
                      onChange={(event) => handleSelectAll(event.target.checked)}
                      aria-label="选择当前页未登记文件"
                    />
                  </th>
                  <th>存储路径</th>
                  <th>类型</th>
                  <th>大小</th>
                  <th>修改时间</th>
                  <th>队列状态</th>
                </tr>
              </thead>
              <tbody>
                {orphans.map((entry) => {
                  const queueState = queueStateByPath.get(entry.storage_path) ?? null;
                  return (
                    <tr
                      key={entry.storage_path}
                      className={selected.has(entry.storage_path) ? "selected" : undefined}
                      data-testid={`orphan-row-${entry.storage_path}`}
                    >
                      <td className="admin-check-cell">
                        <input
                          type="checkbox"
                          checked={selected.has(entry.storage_path)}
                          onChange={(event) =>
                            setItemSelected(entry.storage_path, event.target.checked)
                          }
                          aria-label={`选择 ${entry.storage_path}`}
                        />
                      </td>
                      <td className="admin-name-cell" title={entry.storage_path}>
                        {entry.storage_path}
                      </td>
                      <td>
                        <span className="status-badge">
                          <Icon
                            name={entry.kind === "directory" ? "folder" : "file"}
                            className="icon-sm"
                          />
                          {entry.kind === "directory" ? "目录" : "文件"}
                        </span>
                      </td>
                      <td>{formatAdminBytes(entry.size)}</td>
                      <td suppressHydrationWarning>{formatAdminDateTime(entry.modified_at_ms)}</td>
                      <td>
                        {queueState === null ? (
                          "—"
                        ) : (
                          <span className={QUEUE_STATE_CLASS[queueState]}>
                            <Icon name={QUEUE_STATE_ICON[queueState]} className="icon-sm" />
                            {QUEUE_STATE_TEXT[queueState]}
                          </span>
                        )}
                      </td>
                    </tr>
                  );
                })}
                {orphans.length === 0 && (
                  <tr>
                    <td colSpan={6}>
                      {scanning ? (
                        <EmptyState
                          icon={<Icon name="refresh" className="icon-lg" />}
                          title="扫描中"
                          description="正在读取目录项与文件元数据，请稍候。"
                        />
                      ) : !hasScanned ? (
                        <EmptyState
                          icon={<Icon name="search" className="icon-lg" />}
                          title="尚未扫描"
                          description="点「扫描」读取下载目录中未登记进库的存储文件。"
                        />
                      ) : (
                        <EmptyState
                          icon={<Icon name="check" className="icon-lg" />}
                          title="没有未登记文件"
                          description={
                            unrecognizedCount > 0
                              ? `规范存储文件都已登记入库；另有 ${unrecognizedCount} 个不符合命名规则的对象，未计入清单。`
                              : "下载目录中的规范存储文件都已登记入库。"
                          }
                        />
                      )}
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>

          <div className="admin-table-footer admin-storage-footer">
            <span className="admin-result-count">
              {scanning
                ? "正在扫描..."
                : hasScanned
                  ? `共 ${orphanTotal} 个未登记文件`
                  : "尚未扫描"}
            </span>
            {/* 空清单时未识别数量由空态承载，这里不重复同一句话。 */}
            {!scanning && orphans.length > 0 && unrecognizedCount > 0 && (
              <span>另有 {unrecognizedCount} 个不符合命名规则的对象未计入</span>
            )}
            <PaginationControls
              currentPage={page}
              pageSize={PAGE_SIZE}
              totalFiles={orphanTotal}
              onPageChange={handlePageChange}
            />
          </div>
        </section>
      </div>

      {deleteTargets !== null && (
        <DialogShell
          onClose={() => setDeleteTargets(null)}
          ariaLabel="删除未登记文件"
          contentClassName="modal-content admin-dialog-content"
          closeDisabled={deleting}
        >
          <div className="dialog-shell">
            <div className="dialog-head">
              <h2>删除未登记文件</h2>
              <button
                className="icon-btn"
                type="button"
                aria-label="关闭对话框"
                disabled={deleting}
                onClick={(event) => requestModalClose(event.currentTarget)}
              >
                <Icon name="x" />
              </button>
            </div>
            <div className="dialog-body">
              <p className="dialog-message">确定删除选中的 {deleteTargets.length} 项吗？</p>
              {deleteImmediateBytes > 0 && (
                <p className="dialog-message">
                  将立即释放 {formatAdminBytes(deleteImmediateBytes)} 磁盘空间。
                </p>
              )}
              {deleteDeferredCount > 0 && (
                <p className="dialog-message">
                  其中 {deleteDeferredCount} 项已入库，删除只受理，稍后由后台任务完成。
                </p>
              )}
            </div>
            <div className="dialog-actions">
              <button
                type="button"
                className="button secondary"
                ref={deleteReturnRef}
                disabled={deleting}
                onClick={(event) => requestModalClose(event.currentTarget)}
              >
                返回
              </button>
              <button
                type="button"
                className="button danger"
                disabled={deleting}
                onClick={(event) => void handleDelete(event.currentTarget, deleteTargets)}
              >
                {deleting ? "删除中..." : "删除"}
              </button>
            </div>
          </div>
        </DialogShell>
      )}
      <BatchFailureDialog failures={batchFailures} onClose={() => setBatchFailures([])} />
    </>
  );
}
