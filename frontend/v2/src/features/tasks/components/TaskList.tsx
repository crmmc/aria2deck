import type { TaskResponse } from "@/shared/api/types";
import { EmptyState } from "@/shared/components/EmptyState";
import { TaskCard } from "./TaskCard";

type TaskListProps = {
  filteredTasks: TaskResponse[];
  selectedTasks: Set<number>;
  operatingTaskIds: Set<number>;
  onToggleSelection: (id: number) => void;
  onToggleSelectAll: () => void;
  onCancel: (id: number) => void;
  onCopyUri: (uri: string) => void;
  onRetry: (task: TaskResponse) => void;
};

export function TaskList({
  filteredTasks,
  selectedTasks,
  operatingTaskIds,
  onToggleSelection,
  onToggleSelectAll,
  onCancel,
  onCopyUri,
  onRetry,
}: TaskListProps) {
  return (
    <>
      <div className="list-head">
        <input
          type="checkbox"
          aria-label="选择全部可见任务"
          checked={filteredTasks.length > 0 && selectedTasks.size === filteredTasks.length}
          onChange={onToggleSelectAll}
        />
        <span>任务名称</span><span>进度</span><span>状态</span><span>速度</span><span className="text-right">操作</span>
      </div>
      {filteredTasks.length === 0 ? (
        <EmptyState
          icon={
            <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
              <path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z" />
              <polyline points="13 2 13 9 20 9" />
            </svg>
          }
          title="暂无活动任务"
          description="添加新任务开始下载，已完成的文件请前往文件页面查看"
        />
      ) : (
        <div className="task-card-container task-list">
          {filteredTasks.map((task) => (
            <TaskCard
              key={task.id}
              task={task}
              isSelected={selectedTasks.has(task.id)}
              isOperating={operatingTaskIds.has(task.id)}
              onToggleSelection={onToggleSelection}
              onCancel={onCancel}
              onCopyUri={onCopyUri}
              onRetry={onRetry}
            />
          ))}
        </div>
      )}
    </>
  );
}
