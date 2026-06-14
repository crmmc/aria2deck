import type { Task } from "@/types";
import { EmptyState } from "@/components/ui/EmptyState";
import { TaskCard } from "./TaskCard";

type TaskListProps = {
  filteredTasks: Task[];
  selectedTasks: Set<number>;
  operatingTaskIds: Set<number>;
  onToggleSelection: (id: number) => void;
  onCancel: (id: number) => void;
  onCopyUri: (uri: string) => void;
  onRetry: (task: Task) => void;
};

export function TaskList({
  filteredTasks,
  selectedTasks,
  operatingTaskIds,
  onToggleSelection,
  onCancel,
  onCopyUri,
  onRetry,
}: TaskListProps) {
  if (filteredTasks.length === 0) {
    return (
      <EmptyState
        icon={
          <svg
            width="48"
            height="48"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.5"
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <path d="M13 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V9z" />
            <polyline points="13 2 13 9 20 9" />
          </svg>
        }
        title="暂无活动任务"
        description="添加新任务开始下载，已完成的文件请前往文件页面查看"
      />
    );
  }

  return (
    <div className="card task-card-container">
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
  );
}
