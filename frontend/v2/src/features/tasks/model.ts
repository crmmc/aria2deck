/**
 * T1 任务功能模型：归档参考实现的当前视图集合、进行中筛选、本地排序与进度规则。
 * 纯函数，表驱动测试；api.ts 只负责请求，不触碰这些规则。
 */
import type {
  TaskResponse,
  TaskStatus,
  TorrentFilePreviewResponse,
} from "@/shared/api/types";

/**
 * 当前任务页展示集合：只含在下载管线的任务。
 *
 * 依据归档参考后端 `status_filter=current` 的定义
 * （`(~user_terminal) & (~global_terminal) & active`）——终态一律不在当前视图，失败任务只在历史页。
 * 归档参考前端的 isCurrentVisibleStatus 虽含 error，但仅用于 WebSocket 单帧增删判断，
 * 初始列表拿到的是已由后端过滤过的集合。
 */
export const CURRENT_VIEW_STATUSES: readonly TaskStatus[] = [
  "queued",
  "active",
  "waiting",
  "paused",
];

/** 「进行中」筛选：active/queued/waiting/paused（不含失败）。 */
export const IN_FLIGHT_STATUSES: readonly TaskStatus[] = [
  "active",
  "queued",
  "waiting",
  "paused",
];

export type LegacySortKey = "default" | "speed" | "progress";

export function isCurrentVisibleStatus(status: TaskStatus): boolean {
  return (CURRENT_VIEW_STATUSES as readonly TaskStatus[]).includes(status);
}

export function isInFlightStatus(status: TaskStatus): boolean {
  return (IN_FLIGHT_STATUSES as readonly TaskStatus[]).includes(status);
}

/** 当前任务视图过滤：只保留在下载管线的任务（失败项不进当前视图，只进历史页）。 */
export function filterCurrentTasks(tasks: readonly TaskResponse[]): TaskResponse[] {
  return tasks.filter((task) => isCurrentVisibleStatus(task.status));
}

/** 「进行中」过滤（与当前视图同口径，均不含终态）。 */
export function filterInFlightTasks(tasks: readonly TaskResponse[]): TaskResponse[] {
  return tasks.filter((task) => isInFlightStatus(task.status));
}

/** 归档参考 任务搜索：名称大小写不敏感包含；无名任务不匹配。 */
export function searchTasks(
  tasks: readonly TaskResponse[],
  keyword: string,
): TaskResponse[] {
  const needle = keyword.trim().toLowerCase();
  if (needle === "") return [...tasks];
  return tasks.filter(
    (task) => task.name !== "" && task.name.toLowerCase().includes(needle),
  );
}

/** 归档参考 进度口径：total_bytes 为 null/0 时进度为 0。 */
export function progressOf(task: TaskResponse): number {
  if (task.total_bytes === null || task.total_bytes <= 0) return 0;
  return task.completed_bytes / task.total_bytes;
}

/**
 * 归档参考 本地排序（C22：V2 服务端不支持速度/进度排序，聚合后本地排）。
 * default=最近更新：保持输入（后端默认 created_at desc 顺序）；
 * speed/progress 均降序；用的稳定化比较（index 兜底）保持同值顺序。
 */
export function sortTasks(
  tasks: readonly TaskResponse[],
  key: LegacySortKey,
): TaskResponse[] {
  if (key === "default") return [...tasks];
  const indexed = tasks.map((task, index) => ({ task, index }));
  indexed.sort((a, b) => {
    let delta: number;
    if (key === "speed") {
      delta = b.task.download_speed - a.task.download_speed;
    } else {
      delta = progressOf(b.task) - progressOf(a.task);
    }
    return delta !== 0 ? delta : a.index - b.index;
  });
  return indexed.map((entry) => entry.task);
}

/* ============ 种子树（T1：V2 preview 只给平铺 files，树在客户端按插入顺序构建） ============ */

export interface TorrentTreeNode {
  type: "file" | "directory";
  name: string;
  path: string[];
  size: number;
  index?: number;
  children: TorrentTreeNode[];
}

function splitTorrentPath(path: string): string[] {
  return path.split("/").filter((segment) => segment !== "");
}

/** 归档参考 种子树顺序：按 files 插入顺序建树，目录在首次遇见的位置出现，不做目录优先排序。 */
export function buildTorrentTree(files: readonly TorrentFilePreviewResponse[]): TorrentTreeNode[] {
  const roots: TorrentTreeNode[] = [];
  for (const file of files) {
    const segments = splitTorrentPath(file.path);
    if (segments.length === 0) continue;
    let level = roots;
    let walked: string[] = [];
    for (let i = 0; i < segments.length - 1; i += 1) {
      walked = [...walked, segments[i]];
      let dir = level.find((node) => node.type === "directory" && node.name === segments[i]);
      if (!dir) {
        dir = { type: "directory", name: segments[i], path: walked, size: 0, children: [] };
        level.push(dir);
      }
      level = dir.children;
    }
    const name = segments[segments.length - 1];
    level.push({
      type: "file",
      name,
      path: [...walked, name],
      size: file.size,
      index: file.index,
      children: [],
    });
  }
  return roots;
}

/** 目录大小 = 子孙文件大小之和（建树后回填）。 */
export function fillDirectorySizes(nodes: TorrentTreeNode[]): TorrentTreeNode[] {
  const walk = (node: TorrentTreeNode): number => {
    if (node.type === "file") return node.size;
    node.size = node.children.reduce((sum, child) => sum + walk(child), 0);
    return node.size;
  };
  nodes.forEach(walk);
  return nodes;
}
