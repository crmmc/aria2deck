import type { TaskResponse } from "@/shared/api/types";
import { StatusBadge } from "@/shared/components/ui";
import { formatBytes, formatDateTime, formatPercent, formatSpeed } from "@/shared/lib/format";
import type { TaskSort, TaskSortKey } from "./api";

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
  sort,
  onToggle,
  onToggleAll,
  onCancel,
  onRetry,
  onSort,
}: {
  tasks: TaskResponse[];
  selected: Set<number>;
  sort: TaskSort | null;
  onToggle: (id: number) => void;
  onToggleAll: (checked: boolean) => void;
  onCancel: (id: number) => void;
  onRetry: (id: number) => void;
  onSort: (key: TaskSortKey) => void;
}) {
  const allSelected = tasks.length > 0 && tasks.every((task) => selected.has(task.id));
  return (
    <table className="data-table">
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
            <td>
              <span>{formatBytes(task.completed_bytes)}</span>
              <span className="progress-detail">
                {" / "}
                {formatBytes(task.total_bytes)}
                {" · "}
                {formatPercent(task.completed_bytes, task.total_bytes)}
              </span>
            </td>
            <td>{task.total_bytes === null ? "—" : formatBytes(task.total_bytes)}</td>
            <td>{task.status === "active" ? formatSpeed(task.download_speed) : "—"}</td>
            <td>{formatDateTime(task.created_at_ms)}</td>
            <td className="actions-cell">
              {task.retryable && (
                <button type="button" className="link-button" onClick={() => onRetry(task.id)}>
                  重试
                </button>
              )}
              {["queued", "active", "waiting", "paused"].includes(task.status) && (
                <button type="button" className="link-button" onClick={() => onCancel(task.id)}>
                  取消
                </button>
              )}
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}
