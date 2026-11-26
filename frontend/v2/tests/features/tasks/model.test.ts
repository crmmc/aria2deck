import {
  filterInFlightTasks,
  isCurrentVisibleStatus,
  isInFlightStatus,
  progressOf,
  searchTasks,
  sortTasks,
} from "@/features/tasks/model";
import type { TaskResponse, TaskStatus } from "@/shared/api/types";

function task(overrides: Partial<TaskResponse> & { id: number }): TaskResponse {
  return {
    status: "active",
    status_label: "",
    name: `task-${overrides.id}`,
    uri: null,
    total_bytes: 100,
    completed_bytes: 0,
    download_speed: 0,
    upload_speed: 0,
    peer_count: 0,
    connection_count: 0,
    files: [],
    observation_fresh: true,
    reserved_bytes: 0,
    error_code: null,
    error_message: "",
    retryable: false,
    created_at_ms: overrides.id,
    updated_at_ms: overrides.id,
    finished_at_ms: null,
    ...overrides,
  };
}

describe("isCurrentVisibleStatus（会话内可见性：管线 + 失败）", () => {
  it.each<[TaskStatus, boolean]>([
    ["queued", true],
    ["active", true],
    ["waiting", true],
    ["paused", true],
    ["failed", true],
    ["completed", false],
    ["cancelled", false],
    ["expired", false],
  ])("%s -> %s", (status, expected) => {
    expect(isCurrentVisibleStatus(status)).toBe(expected);
  });
});

describe("isInFlightStatus（V1 进行中筛选）", () => {
  it.each<[TaskStatus, boolean]>([
    ["active", true],
    ["queued", true],
    ["waiting", true],
    ["paused", true],
    ["failed", false],
    ["completed", false],
  ])("%s -> %s", (status, expected) => {
    expect(isInFlightStatus(status)).toBe(expected);
  });
});

describe("filterInFlightTasks", () => {
  it("排除 failed", () => {
    const tasks = [task({ id: 1, status: "failed" }), task({ id: 2, status: "paused" })];
    expect(filterInFlightTasks(tasks).map((t) => t.id)).toEqual([2]);
  });
});

describe("searchTasks", () => {
  it("大小写不敏感包含匹配，空关键词原样返回", () => {
    const tasks = [task({ id: 1, name: "Ubuntu ISO" }), task({ id: 2, name: "debian" })];
    expect(searchTasks(tasks, "ubuntu").map((t) => t.id)).toEqual([1]);
    expect(searchTasks(tasks, "  ").map((t) => t.id)).toEqual([1, 2]);
  });

  it("空名称任务不匹配", () => {
    const tasks = [task({ id: 1, name: "" })];
    expect(searchTasks(tasks, "a")).toEqual([]);
  });
});

describe("progressOf", () => {
  it("total_bytes 为 null 或 0 时进度为 0", () => {
    expect(progressOf(task({ id: 1, total_bytes: null, completed_bytes: 50 }))).toBe(0);
    expect(progressOf(task({ id: 2, total_bytes: 0, completed_bytes: 50 }))).toBe(0);
    expect(progressOf(task({ id: 3, total_bytes: 200, completed_bytes: 50 }))).toBe(0.25);
  });
});

describe("sortTasks（V1 本地排序）", () => {
  it("default 保持输入顺序（最近更新=后端默认序）", () => {
    const tasks = [task({ id: 1 }), task({ id: 2 }), task({ id: 3 })];
    expect(sortTasks(tasks, "default").map((t) => t.id)).toEqual([1, 2, 3]);
  });

  it("speed 按下载速度降序", () => {
    const tasks = [
      task({ id: 1, download_speed: 10 }),
      task({ id: 2, download_speed: 90 }),
      task({ id: 3, download_speed: 40 }),
    ];
    expect(sortTasks(tasks, "speed").map((t) => t.id)).toEqual([2, 3, 1]);
  });

  it("progress 按完成度降序，total 为空视为 0", () => {
    const tasks = [
      task({ id: 1, total_bytes: 100, completed_bytes: 50 }),
      task({ id: 2, total_bytes: null, completed_bytes: 0 }),
      task({ id: 3, total_bytes: 100, completed_bytes: 90 }),
    ];
    expect(sortTasks(tasks, "progress").map((t) => t.id)).toEqual([3, 1, 2]);
  });

  it("同值保持原顺序（稳定排序）", () => {
    const tasks = [task({ id: 1, download_speed: 5 }), task({ id: 2, download_speed: 5 })];
    expect(sortTasks(tasks, "speed").map((t) => t.id)).toEqual([1, 2]);
  });
});
