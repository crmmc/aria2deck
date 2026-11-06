import type { TaskResponse } from "@/shared/api/types";
import { StatusBadge } from "@/shared/components/ui";
import { formatBytes, formatDateTime, formatPercent, formatSpeed } from "@/shared/lib/format";
import type { TaskSort, TaskSortKey } from "./api";

const IN_FLIGHT_STATUSES = ["queued", "active", "waiting", "paused"];

/** 进行中/终态外的任务不提供重试；不可重试的终态给出口径化原因（T14）。 */
function retryBlockedReason(task: TaskResponse): string | null {
  if (task.retryable) return null;
  if (task.status === "completed") return "已完成的任务不可重试";
  if (task.status === "expired") return "已过期的任务不可重试";
  if (task.status === "failed" || task.status === "cancelled") return "该任务当前不可重试";
  return null;
}

/** 进度条（T16）：active 条纹动画、error 红、completed 满；reduced-motion 由全局样式关动画。 */
function TaskProgress({ task, label }: { task: TaskResponse; label: string }) {
  const percent =
    task.total_bytes !== null && task.total_bytes > 0
      ? Math.min(100, Math.floor((task.completed_bytes / task.total_bytes) * 100))
      : 0;
  const tone =
    task.status === "failed"
      ? "error"
      : task.status === "active"
        ? "active"
        : task.status === "completed"
          ? "full"
          : "idle";
  return (
    <div
      className="progress-track task-progress"
      role="progressbar"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={percent}
    >
      <div className={`progress-fill progress-${tone}`} style={{ width: `${percent}%` }} />
      <span className="task-progress-text">{formatPercent(task.completed_bytes, task.total_bytes)}</span>
    </div>
  );
}

/** 行操作按钮组：桌面表格与移动卡片复用（T19）。 */
function TaskActions({
  task,
  removable,
  onRequestCancel,
  onRequestDelete,
  onCopy,
  onRetry,
}: {
  task: TaskResponse;
  removable: boolean;
  onRequestCancel: (task: TaskResponse) => void;
  onRequestDelete: (task: TaskResponse) => void;
  onCopy: (uri: string) => void;
  onRetry: (id: number) => void;
}) {
  const blockedReason = retryBlockedReason(task);
  return (
    <>
      {task.uri !== null && (
        <button
          type="button"
          className="link-button"
          title="复制链接"
          onClick={() => onCopy(task.uri as string)}
        >
          复制
        </button>
      )}
      {task.retryable ? (
        <button type="button" className="link-button" onClick={() => onRetry(task.id)}>
          重试
        </button>
      ) : (
        blockedReason !== null && (
          <button type="button" className="link-button" disabled title={blockedReason}>
            重试
          </button>
        )
      )}
      {IN_FLIGHT_STATUSES.includes(task.status) && (
        <button type="button" className="link-button" onClick={() => onRequestCancel(task)}>
          取消
        </button>
      )}
      {removable && (
        <button type="button" className="link-button danger-link" onClick={() => onRequestDelete(task)}>
          删除
        </button>
      )}
    </>
  );
}

function SortHeader({
  label,
  sortKey,
  sort,
  onSort,
}: {
  label: string;
  sortKey: TaskSortKey;
  sort: TaskSort | null;
  onSort: (key: TaskSortKey) => void;
}) {
  const active = sort?.key === sortKey;
  const indicator = active ? (sort.order === "asc" ? " ↑" : " ↓") : "";
  return (
    <th
      scope="col"
      aria-sort={active ? (sort.order === "asc" ? "ascending" : "descending") : "none"}
    >
      <button
        type="button"
        className="sort-button"
        aria-label={`${label}排序${active ? (sort.order === "asc" ? "（升序）" : "（降序）") : ""}`}
        onClick={() => onSort(sortKey)}
      >
        {label}
        <span aria-hidden="true">{indicator}</span>
      </button>
    </th>
  );
}

export function TaskTable({
  tasks,
  selected,
  allSelected,
  removableIds,
  sort,
  onToggle,
  onToggleAll,
  onRequestCancel,
  onRequestDelete,
  onCopy,
  onRetry,
  onSort,
}: {
  tasks: TaskResponse[];
  selected: Set<number>;
  allSelected: boolean;
  removableIds: Set<number>;
  sort: TaskSort | null;
  onToggle: (id: number) => void;
  onToggleAll: (checked: boolean) => void;
  onRequestCancel: (task: TaskResponse) => void;
  onRequestDelete: (task: TaskResponse) => void;
  onCopy: (uri: string) => void;
  onRetry: (id: number) => void;
  onSort: (key: TaskSortKey) => void;
}) {
  return (
    <>
      <table className="data-table desktop-only task-table">
        <thead>
          <tr>
            <th scope="col" className="select-col">
              <input
                type="checkbox"
                aria-label="全选任务"
                checked={allSelected}
                onChange={(event) => onToggleAll(event.target.checked)}
              />
            </th>
            <SortHeader label="名称" sortKey="name" sort={sort} onSort={onSort} />
            <th scope="col">状态</th>
            <th scope="col">进度</th>
            <SortHeader label="大小" sortKey="size" sort={sort} onSort={onSort} />
            <th scope="col">速度</th>
            <SortHeader label="创建时间" sortKey="created_at" sort={sort} onSort={onSort} />
            <th scope="col">操作</th>
          </tr>
        </thead>
        <tbody>
          {tasks.map((task) => (
            <tr key={task.id} data-testid={`task-row-${task.id}`}>
              <td className="select-col">
                <input
                  type="checkbox"
                  aria-label={`选择 ${task.name}`}
                  checked={selected.has(task.id)}
                  onChange={() => onToggle(task.id)}
                />
              </td>
              <td className="name-cell" title={task.name}>
                {task.name}
              </td>
              <td>
                <StatusBadge status={task.status} label={task.status_label} />
              </td>
              <td className="task-progress-cell">
                <TaskProgress task={task} label={`${task.name} 下载进度`} />
                <span className="progress-detail">
                  {formatBytes(task.completed_bytes)} / {formatBytes(task.total_bytes)}
                </span>
              </td>
              <td>{task.total_bytes === null ? "—" : formatBytes(task.total_bytes)}</td>
              <td>{task.status === "active" ? formatSpeed(task.download_speed) : "—"}</td>
              <td>{formatDateTime(task.created_at_ms)}</td>
              <td className="actions-cell">
                <TaskActions
                  task={task}
                  removable={removableIds.has(task.id)}
                  onRequestCancel={onRequestCancel}
                  onRequestDelete={onRequestDelete}
                  onCopy={onCopy}
                  onRetry={onRetry}
                />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <TaskCardList
        tasks={tasks}
        selected={selected}
        removableIds={removableIds}
        onToggle={onToggle}
        onRequestCancel={onRequestCancel}
        onRequestDelete={onRequestDelete}
        onCopy={onCopy}
        onRetry={onRetry}
      />
    </>
  );
}

/** 移动端卡片变体（T19）：<1024 由 CSS 显示；卡片主体点击复制链接。 */
export function TaskCardList({
  tasks,
  selected,
  removableIds,
  onToggle,
  onRequestCancel,
  onRequestDelete,
  onCopy,
  onRetry,
}: {
  tasks: TaskResponse[];
  selected: Set<number>;
  removableIds: Set<number>;
  onToggle: (id: number) => void;
  onRequestCancel: (task: TaskResponse) => void;
  onRequestDelete: (task: TaskResponse) => void;
  onCopy: (uri: string) => void;
  onRetry: (id: number) => void;
}) {
  return (
    <ul className="task-card-list mobile-only" aria-label="任务卡片列表">
      {tasks.map((task) => (
        <li key={task.id} className="task-card" data-testid={`task-card-${task.id}`}>
          <div className="task-card-header">
            <input
              type="checkbox"
              aria-label={`选择 ${task.name}`}
              checked={selected.has(task.id)}
              onChange={() => onToggle(task.id)}
            />
            {task.uri !== null ? (
              <button
                type="button"
                className="task-card-copy-target"
                title="复制链接"
                onClick={() => onCopy(task.uri as string)}
              >
                <span className="task-card-name" title={task.name}>
                  {task.name}
                </span>
              </button>
            ) : (
              <span className="task-card-name" title={task.name}>
                {task.name}
              </span>
            )}
            <StatusBadge status={task.status} label={task.status_label} />
          </div>
          <TaskProgress task={task} label={`${task.name} 下载进度`} />
          <div className="task-card-meta">
            <span>
              {formatBytes(task.completed_bytes)} / {formatBytes(task.total_bytes)}
            </span>
            <span>{task.status === "active" ? formatSpeed(task.download_speed) : "—"}</span>
          </div>
          <div className="task-card-actions">
            <TaskActions
              task={task}
              removable={removableIds.has(task.id)}
              onRequestCancel={onRequestCancel}
              onRequestDelete={onRequestDelete}
              onCopy={onCopy}
              onRetry={onRetry}
            />
          </div>
        </li>
      ))}
    </ul>
  );
}
