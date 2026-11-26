import { act, fireEvent, render, screen } from "@testing-library/react";
import { TaskCard } from "@/features/tasks/components/TaskCard";
import { TaskToolbar } from "@/features/tasks/components/TaskToolbar";
import { AddTaskForm } from "@/features/tasks/components/AddTaskForm";
import { TaskList } from "@/features/tasks/components/TaskList";
import { StatsWidget } from "@/features/tasks/components/StatsWidget";
import { BatchAddTasksDialog } from "@/features/tasks/components/BatchAddTasksDialog";
import { TorrentFileTree } from "@/features/tasks/components/TorrentFileTree";
import { buildTorrentTree, fillDirectorySizes } from "@/features/tasks/model";
import type { TaskResponse, TaskStatsResponse } from "@/shared/api/types";

const mockFetch = jest.fn();
global.fetch = mockFetch as unknown as typeof fetch;

function task(overrides: Partial<TaskResponse> = {}): TaskResponse {
  return {
    id: 1, status: "active", status_label: "下载中", name: "demo.bin", uri: "https://x/y",
    total_bytes: 200, completed_bytes: 100, download_speed: 50, upload_speed: 0,
    peer_count: 0, connection_count: 1, files: [], observation_fresh: true,
    reserved_bytes: 0, error_code: null, retryable: true,
    created_at_ms: 1, updated_at_ms: 1, finished_at_ms: null,
    ...overrides,
  };
}

function json(status: number, body: unknown): unknown {
  return { status, ok: status >= 200 && status < 300, json: () => Promise.resolve(body) };
}

function activeCount(active: number): TaskStatsResponse["counts"] {
  return { queued: 0, active, waiting: 0, paused: 0, completed: 0, failed: 0, cancelled: 0, expired: 0 };
}

function buildStats({
  disk = { total_bytes: 1000, used_bytes: 100, reserved_bytes: 0, available_bytes: 900, limited: false },
  counts = activeCount(0),
}: { disk?: TaskStatsResponse["disk"]; counts?: TaskStatsResponse["counts"] } = {}): TaskStatsResponse {
  return {
    counts,
    total: counts.active,
    download_speed: 0,
    upload_speed: 0,
    quota: { quota_bytes: 0, used_bytes: 0, reserved_bytes: 0, available_bytes: 0, limited: false },
    disk,
  };
}

beforeEach(() => {
  mockFetch.mockReset();
});

describe("TaskCard", () => {
  const base = {
    isSelected: false,
    isOperating: false,
    onToggleSelection: jest.fn(),
    onCancel: jest.fn(),
    onCopyUri: jest.fn(),
    onRetry: jest.fn(),
  };

  it("active 任务显示速度与进度", () => {
    render(<TaskCard task={task()} {...base} />);
    expect(screen.getByText("100.0 B / 200.0 B")).toBeInTheDocument();
    expect(screen.getByText("50.0 B/s")).toBeInTheDocument();
    expect(screen.getByText("50.0%")).toBeInTheDocument();
  });

  it("total_bytes 为空时不显示进度百分比", () => {
    render(<TaskCard task={task({ total_bytes: null })} {...base} />);
    expect(screen.queryByText(/%/)).not.toBeInTheDocument();
  });

  it("failed 任务显示错误码、重试与删除按钮", () => {
    render(<TaskCard task={task({ status: "failed", status_label: "已失败", error_code: "download_error" })} {...base} />);
    expect(screen.getByText("download_error")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "重试" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "删除" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(base.onRetry).toHaveBeenCalled();
  });

  it("queued/waiting/paused 任务显示取消按钮", () => {
    render(<TaskCard task={task({ status: "paused", status_label: "已暂停" })} {...base} />);
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(base.onCancel).toHaveBeenCalledWith(1);
  });

  it("completed 任务不渲染取消/重试/删除", () => {
    render(<TaskCard task={task({ status: "completed", status_label: "已完成" })} {...base} />);
    expect(screen.queryByRole("button", { name: "取消" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "删除" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "重试" })).not.toBeInTheDocument();
  });

  it("无名任务显示「未知文件」且无 title 属性", () => {
    render(<TaskCard task={task({ name: "" })} {...base} />);
    const heading = screen.getByRole("heading", { level: 3 });
    expect(heading.textContent).toBe("未知文件");
    expect(heading).not.toHaveAttribute("title");
  });

  it("等待中任务显示「等待中」文案", () => {
    render(<TaskCard task={task({ status: "waiting", status_label: "" })} {...base} />);
    expect(screen.getByText("等待中")).toBeInTheDocument();
  });

  it("status_label 为空时按状态回退文案", () => {
    const { rerender } = render(<TaskCard task={task({ status: "queued", status_label: "" })} {...base} />);
    expect(screen.getByText("排队中")).toBeInTheDocument();
    rerender(<TaskCard task={task({ status: "paused", status_label: "" })} {...base} />);
    expect(screen.getByText("已暂停")).toBeInTheDocument();
    rerender(<TaskCard task={task({ status: "active", status_label: "" })} {...base} />);
    expect(screen.getByText("下载中")).toBeInTheDocument();
  });

  it("total_bytes 为 null 时进度条为 0% 且不显示百分比标签", () => {
    const { container } = render(<TaskCard task={task({ total_bytes: null })} {...base} />);
    expect(container.querySelector(".progress-bar")).toHaveStyle({ width: "0%" });
    // V1 规则：total_bytes 为空时不渲染百分比文本，已下载量照常展示、总量显示 0 B。
    expect(screen.queryByText(/%/)).not.toBeInTheDocument();
    expect(document.querySelector(".task-card-meta")?.textContent).toContain("0 B");
  });

  it("操作中时取消按钮禁用并显示处理中", () => {
    render(<TaskCard task={task()} {...base} isOperating />);
    const button = screen.getByRole("button", { name: "处理中..." });
    expect(button).toBeDisabled();
    expect(button.className).toContain("opacity-60");
  });

  it("失败但无错误码时不渲染错误文本", () => {
    render(<TaskCard task={task({ status: "failed", error_code: null })} {...base} />);
    expect(document.querySelector(".task-error-text")).toBeNull();
  });

  it("可点击卡片：点击复制链接、勾选框切换选中", () => {
    render(<TaskCard task={task()} {...base} />);
    fireEvent.click(screen.getByRole("button", { name: /demo\.bin/ }));
    expect(base.onCopyUri).toHaveBeenCalledWith("https://x/y");
    fireEvent.click(screen.getByLabelText("选择 demo.bin"));
    expect(base.onToggleSelection).toHaveBeenCalledWith(1);
  });

  it("无 uri 任务不显示复制按钮", () => {
    render(<TaskCard task={task({ uri: null })} {...base} />);
    expect(screen.queryByRole("button", { name: "复制" })).not.toBeInTheDocument();
  });

  it("无 uri 时点击卡片/勾选框不触发复制", () => {
    const onCopyUri = jest.fn();
    render(<TaskCard task={task({ uri: null })} {...base} onCopyUri={onCopyUri} />);
    // 无 uri 时卡片主体不是按钮，勾选框仍可切换。
    expect(screen.queryByRole("button", { name: /demo\.bin/ })).not.toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("选择 demo.bin"));
    expect(onCopyUri).not.toHaveBeenCalled();
    expect(base.onToggleSelection).toHaveBeenCalledWith(1);
  });
});

describe("TaskToolbar", () => {
  const base = {
    selectedCount: 0,
    filteredCount: 3,
    currentCount: 3,
    activeCount: 1,
    filterStatus: "all",
    sortBy: "speed",
    searchKeyword: "",
    hasActiveTasks: false,
    isBatchOperating: false,
    onToggleSelectAll: jest.fn(),
    onBatchCancel: jest.fn(),
    onFilterStatusChange: jest.fn(),
    onSortByChange: jest.fn(),
    onSearchKeywordChange: jest.fn(),
  };

  it("筛选/排序/搜索变更并显示真实分段数量", () => {
    render(<TaskToolbar {...base} />);
    expect(screen.queryByRole("button", { name: "全选" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("tab", { name: "进行中 1" }));
    expect(base.onFilterStatusChange).toHaveBeenCalledWith("active");
    fireEvent.change(screen.getByLabelText("任务排序"), { target: { value: "progress" } });
    expect(base.onSortByChange).toHaveBeenCalledWith("progress");
    fireEvent.change(screen.getByLabelText("搜索任务"), { target: { value: "ab" } });
    expect(base.onSearchKeywordChange).toHaveBeenCalledWith("ab");
  });

  it("全选状态显示取消全选，选中后显示批量取消", () => {
    render(<TaskToolbar {...base} selectedCount={3} hasActiveTasks />);
    expect(screen.getByRole("button", { name: "取消全选" })).toBeInTheDocument();
    expect(document.querySelector(".bulk-count")?.textContent).toBe("已选 3 个任务");
    expect(screen.getByRole("button", { name: "取消下载" })).toBeInTheDocument();
    expect(document.querySelector(".toolbar-default")).toHaveAttribute("hidden");
    expect(document.querySelector(".toolbar-bulk")).not.toHaveAttribute("hidden");
    expect(document.querySelector(".filter-toolbar")).toHaveClass("is-bulk");
  });
});

describe("AddTaskForm", () => {
  it("提交与按钮组", () => {
    const onSubmit = jest.fn((e) => e.preventDefault());
    const uriRef = { current: null };
    const torrentRef = { current: null };
    render(
      <AddTaskForm
        uri="https://a"
        error={null}
        isSubmitting={false}
        uriInputRef={uriRef as never}
        torrentInputRef={torrentRef as never}
        onUriChange={jest.fn()}
        onSubmit={onSubmit}
        onTorrentUpload={jest.fn()}
        onBatchAdd={jest.fn()}
      />,
    );
    expect(screen.getByLabelText("下载链接")).toHaveValue("https://a");
    expect(screen.getAllByRole("button").map((button) => button.textContent)).toEqual([
      "批量添加",
      "上传种子",
      "+ 添加任务",
    ]);
    fireEvent.click(screen.getByRole("button", { name: "批量添加" }));
  });
});

describe("TaskList", () => {
  const base = {
    selectedTasks: new Set<number>(),
    operatingTaskIds: new Set<number>(),
    onToggleSelection: jest.fn(),
    onToggleSelectAll: jest.fn(),
    onCancel: jest.fn(),
    onCopyUri: jest.fn(),
    onRetry: jest.fn(),
  };

  it("空列表渲染空状态", () => {
    render(<TaskList filteredTasks={[]} {...base} />);
    expect(screen.getByText("暂无活动任务")).toBeInTheDocument();
  });

  it("渲染表头、卡片列表和可用的表头全选", () => {
    render(<TaskList filteredTasks={[task()]} {...base} />);
    expect(screen.getByText("demo.bin")).toBeInTheDocument();
    expect(screen.getByText("任务名称")).toBeInTheDocument();
    expect(screen.getByText("进度")).toBeInTheDocument();
    expect(screen.getByText("状态")).toBeInTheDocument();
    expect(screen.getByText("速度")).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("选择全部可见任务"));
    expect(base.onToggleSelectAll).toHaveBeenCalled();
  });
});

describe("StatsWidget", () => {
  it("用 disk 口径渲染（C26）", async () => {
    const stats: TaskStatsResponse = {
      counts: { queued: 0, active: 2, waiting: 0, paused: 0, completed: 0, failed: 0, cancelled: 0, expired: 0 },
      total: 2, download_speed: 1024, upload_speed: 0,
      quota: { quota_bytes: 999999, used_bytes: 0, reserved_bytes: 0, available_bytes: 0, limited: false },
      disk: { total_bytes: 1000, used_bytes: 400, reserved_bytes: 100, available_bytes: 500, limited: true },
    };
    mockFetch.mockResolvedValue(json(200, stats));
    const { container } = render(<StatsWidget />);
    await screen.findByRole("img", { name: /1 次真实采样/ });
    // 可用 = total-used-frozen = 500 B；quota 999999 不得出现在组件里。
    expect(container.querySelector(".storage-available strong")?.textContent).toBe("500.0 B");
    expect(screen.queryByText(/999999/)).not.toBeInTheDocument();
    // 「已使用」含在途冻结，与「可用」同一口径：400 + 100 = 500，占 50%。
    expect(screen.getByText("已使用 500.0 B（50%）")).toBeInTheDocument();
    // 冻结量仍单独标出，并以浅色分段接在实色段之后。
    expect(screen.getByText("已冻结 100.0 B")).toBeInTheDocument();
    expect((container.querySelector(".progress-bar") as HTMLElement).style.width).toBe("40%");
    expect((container.querySelector(".progress-bar-reserved") as HTMLElement).style.width).toBe("10%");
    expect(screen.getByText("活动任务").parentElement?.textContent).toContain("2");
  });

  // jsdom 的 cssstyle 会丢弃 var() 颜色，颜色档位无法从 DOM 观察；此处只断言可从 DOM 读到的进度宽度。
  it.each([
    ["高占用", 900, 1000, "90"],
    ["中占用", 600, 1000, "60"],
    ["低占用", 100, 1000, "10"],
    ["超量使用被夹到 100%", 1500, 1000, "100"],
  ])("%s时进度条宽度%s%%", async (_label, used, total, percent) => {
    mockFetch.mockResolvedValue(
      json(200, buildStats({ disk: { total_bytes: total, used_bytes: used, reserved_bytes: 0, available_bytes: 0, limited: false } })),
    );
    const { container } = render(<StatsWidget />);
    await screen.findByRole("img", { name: /1 次真实采样/ });
    const bar = container.querySelector(".progress-bar") as HTMLElement;
    expect(bar.style.width).toBe(`${percent}%`);
  });

  it("总量为 0 时进度条为 0%", async () => {
    mockFetch.mockResolvedValue(
      json(200, buildStats({ disk: { total_bytes: 0, used_bytes: 0, reserved_bytes: 0, available_bytes: 0, limited: false } })),
    );
    const { container } = render(<StatsWidget />);
    await screen.findByRole("img", { name: /1 次真实采样/ });
    expect((container.querySelector(".progress-bar") as HTMLElement).style.width).toBe("0%");
  });

  it("用量为溢出数值时进度条回到 0%", async () => {
    // 1e999 经 JSON 解析得到 Infinity，模拟后端返回溢出数值。
    const stats = JSON.parse(
      '{"counts":{"queued":0,"active":0,"waiting":0,"paused":0,"completed":0,"failed":0,"cancelled":0,"expired":0},' +
        '"total":0,"download_speed":0,"upload_speed":0,' +
        '"quota":{"quota_bytes":0,"used_bytes":0,"reserved_bytes":0,"available_bytes":0,"limited":false},' +
        '"disk":{"total_bytes":1000,"used_bytes":1e999,"reserved_bytes":0,"available_bytes":0,"limited":false}}',
    ) as TaskStatsResponse;
    mockFetch.mockResolvedValue(json(200, stats));
    const { container } = render(<StatsWidget />);
    await screen.findByRole("img", { name: /1 次真实采样/ });
    expect((container.querySelector(".progress-bar") as HTMLElement).style.width).toBe("0%");
  });

  it("未冻结时不显示冻结行", async () => {
    mockFetch.mockResolvedValue(json(200, buildStats({})));
    render(<StatsWidget />);
    await screen.findByRole("img", { name: /1 次真实采样/ });
    expect(screen.queryByText(/已冻结/)).not.toBeInTheDocument();
  });

  it("首次加载失败时图表区只留骨架与空磁盘状态", async () => {
    mockFetch.mockRejectedValue(new TypeError("fetch failed"));
    const { container } = render(<StatsWidget />);
    await act(async () => {
      await Promise.resolve();
    });
    // 图表区不再有任何采样提示文字，只剩轴标签骨架。
    expect(screen.queryByText(/等待真实速度样本/)).not.toBeInTheDocument();
    expect(screen.queryByText(/等待下一次连线/)).not.toBeInTheDocument();
    expect(container.querySelector(".chart-axis")).not.toBeNull();
    expect(screen.getByText("等待真实磁盘数据")).toBeInTheDocument();
    expect(container.querySelector(".speed-line")).toBeNull();
    expect(container.querySelector(".speed-point")).toBeNull();
  });

  it("轮询失败保留上次读数", async () => {
    jest.useFakeTimers();
    try {
      mockFetch.mockResolvedValue(json(200, buildStats({})));
      render(<StatsWidget />);
      await act(async () => {
        await Promise.resolve();
      });
      expect(screen.getByText("存储空间")).toBeInTheDocument();

      mockFetch.mockRejectedValue(new TypeError("fetch failed"));
      await act(async () => {
        jest.advanceTimersByTime(5000);
        await Promise.resolve();
      });
      expect(screen.getByText("存储空间")).toBeInTheDocument();
    } finally {
      jest.useRealTimers();
    }
  });

  it("页面隐藏时轮询不请求", async () => {
    jest.useFakeTimers();
    try {
      mockFetch.mockResolvedValue(json(200, buildStats({})));
      const before = mockFetch.mock.calls.filter(([url]) => url === "/api/v2/tasks/stats").length;
      render(<StatsWidget />);
      await act(async () => {
        await Promise.resolve();
      });
      const statsCalls = () => mockFetch.mock.calls.filter(([url]) => url === "/api/v2/tasks/stats").length;
      expect(statsCalls() - before).toBe(1);

      const hiddenSpy = jest.spyOn(document, "hidden", "get").mockReturnValue(true);
      try {
        await act(async () => {
          jest.advanceTimersByTime(5000);
          await Promise.resolve();
        });
        expect(statsCalls() - before).toBe(1);
      } finally {
        hiddenSpy.mockRestore();
      }
    } finally {
      jest.useRealTimers();
    }
  });

  it("可见时约定时间后重新拉取读数", async () => {
    jest.useFakeTimers();
    try {
      mockFetch.mockResolvedValueOnce(json(200, buildStats({ counts: activeCount(1) })));
      render(<StatsWidget />);
      await act(async () => {
        await Promise.resolve();
      });
      expect(screen.getByText("活动任务").parentElement?.textContent).toContain("1");

      mockFetch.mockResolvedValueOnce(json(200, buildStats({ counts: activeCount(3) })));
      await act(async () => {
        jest.advanceTimersByTime(5000);
        await Promise.resolve();
      });
      expect(screen.getByText("活动任务").parentElement?.textContent).toContain("3");
    } finally {
      jest.useRealTimers();
    }
  });

  it("只用真实样本：首样本画点，第二次轮询后才连线", async () => {
    jest.useFakeTimers();
    try {
      mockFetch.mockResolvedValueOnce(json(200, {
        ...buildStats({}),
        download_speed: 100,
        upload_speed: 20,
      }));
      const { container } = render(<StatsWidget />);
      expect(await screen.findByRole("img", { name: /1 次真实采样/ })).toBeInTheDocument();
      expect(screen.queryByText(/等待下一次连线/)).not.toBeInTheDocument();
      expect(container.querySelector(".chart-axis")).not.toBeNull();
      expect(container.querySelector(".speed-point")).not.toBeNull();
      expect(container.querySelector(".upload-point")).not.toBeNull();
      expect(container.querySelector(".speed-line")).toBeNull();
      expect(container.querySelector(".upload-line")).toBeNull();

      mockFetch.mockResolvedValueOnce(json(200, {
        ...buildStats({}),
        download_speed: 1000,
        upload_speed: 100,
      }));
      await act(async () => {
        jest.advanceTimersByTime(5000);
        await Promise.resolve();
      });
      expect(await screen.findByRole("img", { name: /2 次真实采样/ })).toBeInTheDocument();
      expect(container.querySelector(".speed-line")?.getAttribute("d")).toContain("L630.0 42.4");
      expect(container.querySelector(".upload-line")?.getAttribute("d")).toContain("L630.0 135.6");
      expect(screen.getByText("60 秒前")).toBeInTheDocument();
    } finally {
      jest.useRealTimers();
    }
  });

  it("上一次轮询仍在途时跳过新请求", async () => {
    jest.useFakeTimers();
    try {
      mockFetch.mockResolvedValueOnce(json(200, buildStats({})));
      render(<StatsWidget />);
      await act(async () => {
        await Promise.resolve();
      });
      const statsCalls = () => mockFetch.mock.calls.filter(([url]) => url === "/api/v2/tasks/stats").length;
      expect(statsCalls()).toBe(1);

      let release: (value: unknown) => void = () => undefined;
      mockFetch.mockImplementationOnce(() => new Promise((resolve) => {
        release = resolve;
      }));
      await act(async () => {
        jest.advanceTimersByTime(5000);
        await Promise.resolve();
      });
      expect(statsCalls()).toBe(2);
      await act(async () => {
        jest.advanceTimersByTime(5000);
        await Promise.resolve();
      });
      expect(statsCalls()).toBe(2);
      release(json(200, buildStats({})));
      await act(async () => {
        await Promise.resolve();
      });
    } finally {
      jest.useRealTimers();
    }
  });

  it("卸载后迟到的响应不再写入", async () => {
    const holder: { resolve?: (value: unknown) => void } = {};
    mockFetch.mockImplementation(() => new Promise((resolve) => {
      holder.resolve = resolve;
    }));
    const { unmount } = render(<StatsWidget />);
    unmount();
    holder.resolve?.(json(200, buildStats({})));
    await act(async () => {
      await Promise.resolve();
    });
    expect(screen.queryByText("存储空间")).not.toBeInTheDocument();
  });
});

describe("BatchAddTasksDialog", () => {
  let originalShowModal: HTMLDialogElement["showModal"] | undefined;
  beforeEach(() => {
    originalShowModal = HTMLDialogElement.prototype.showModal;
    Object.defineProperty(HTMLDialogElement.prototype, "showModal", {
      configurable: true,
      value: jest.fn(function showModal(this: HTMLDialogElement) {
        this.setAttribute("open", "");
      }),
    });
  });
  afterEach(() => {
    document.querySelectorAll("dialog").forEach((d) => d.removeAttribute("open"));
    if (originalShowModal) {
      Object.defineProperty(HTMLDialogElement.prototype, "showModal", { configurable: true, value: originalShowModal });
    } else {
      Reflect.deleteProperty(HTMLDialogElement.prototype, "showModal");
    }
  });

  it("错误态显示 role=alert", () => {
    render(
      <BatchAddTasksDialog
        batchUris=""
        isBatchAdding={false}
        feedback={{ kind: "error", message: "提交结果暂无法确认" }}
        onBatchUrisChange={jest.fn()}
        onSubmit={jest.fn()}
        onCancel={jest.fn()}
        onRetryFailed={jest.fn()}
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("提交结果暂无法确认");
  });

  it("部分失败列表支持回填，完成按钮只通知一次关闭", () => {
    const onRetryFailed = jest.fn();
    const onCancel = jest.fn();
    render(
      <BatchAddTasksDialog
        batchUris=""
        isBatchAdding={false}
        feedback={{ kind: "result", acceptedCount: 1, failures: [{ uri: "https://a/2", reason: "链接无效" }] }}
        onBatchUrisChange={jest.fn()}
        onSubmit={jest.fn()}
        onCancel={onCancel}
        onRetryFailed={onRetryFailed}
      />,
    );
    expect(screen.getByText("已提交 1 个，1 个失败")).toBeInTheDocument();
    expect(screen.getByText("链接无效")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重试失败项" }));
    expect(onRetryFailed).toHaveBeenCalledTimes(1);
    expect(onCancel).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "完成" }));
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("提交按钮把自身交给父级用于成功后的统一退出", () => {
    const onSubmit = jest.fn();
    render(
      <BatchAddTasksDialog
        batchUris="https://a/1"
        isBatchAdding={false}
        feedback={null}
        onBatchUrisChange={jest.fn()}
        onSubmit={onSubmit}
        onCancel={jest.fn()}
        onRetryFailed={jest.fn()}
      />,
    );
    const submit = screen.getByRole("button", { name: "添加任务" });
    fireEvent.click(submit);
    expect(onSubmit).toHaveBeenCalledWith(submit);
  });
});

describe("buildTorrentTree", () => {
  it("按插入顺序建树并回填目录大小", () => {
    const tree = fillDirectorySizes(buildTorrentTree([
      { index: 1, path: "根/子/a.bin", size: 10 },
      { index: 2, path: "b.bin", size: 5 },
      { index: 3, path: "根/c.bin", size: 7 },
    ]));
    expect(tree.map((n) => n.name)).toEqual(["根", "b.bin"]);
    const root = tree[0];
    expect(root.type).toBe("directory");
    expect(root.size).toBe(17);
    expect(root.children.map((n) => n.name)).toEqual(["子", "c.bin"]);
  });
});

describe("api client boundary", () => {
  it("StatsWidget 经统一 client 请求", async () => {
    mockFetch.mockResolvedValue(json(200, {
      counts: { queued: 0, active: 0, waiting: 0, paused: 0, completed: 0, failed: 0, cancelled: 0, expired: 0 },
      total: 0, download_speed: 0, upload_speed: 0,
      quota: { quota_bytes: 0, used_bytes: 0, reserved_bytes: 0, available_bytes: 0, limited: false },
      disk: { total_bytes: 1, used_bytes: 0, reserved_bytes: 0, available_bytes: 1, limited: false },
    }));
    render(<StatsWidget />);
    await screen.findByRole("img", { name: /1 次真实采样/ });
    expect(mockFetch.mock.calls.some(([url]) => url === "/api/v2/tasks/stats")).toBe(true);
  });
});

describe("TorrentFileTree 只读与部分选中", () => {
  it("文件名与目录名都带完整名称的悬停提示", () => {
    const nodes = fillDirectorySizes(buildTorrentTree([
      { index: 1, path: "很长的目录名称会被截断/很长的文件名会被截断.mkv", size: 10 },
    ]));
    const { container } = render(
      <TorrentFileTree nodes={nodes} selectedIndexes={new Set([1])} searchQuery="" />,
    );
    const titles = Array.from(container.querySelectorAll(".torrent-tree-name"))
      .map((element) => element.getAttribute("title"));
    expect(titles).toEqual(["很长的目录名称会被截断", "很长的文件名会被截断.mkv"]);
  });

  it("只读模式下只渲染有选中叶子的节点", () => {
    const nodes = fillDirectorySizes(buildTorrentTree([
      { index: 1, path: "A/x.bin", size: 10 },
      { index: 2, path: "B/y.bin", size: 20 },
    ]));
    const { container } = render(
      <TorrentFileTree nodes={nodes} selectedIndexes={new Set([1])} searchQuery="" readonly />,
    );
    const text = container.textContent ?? "";
    expect(text).toContain("x.bin");
    expect(text).not.toContain("y.bin");
  });

  it("只读模式下无选中叶子时目录不渲染", () => {
    const nodes = fillDirectorySizes(buildTorrentTree([
      { index: 1, path: "A/x.bin", size: 10 },
    ]));
    const onToggleFile = jest.fn();
    const { container } = render(
      <TorrentFileTree
        nodes={nodes}
        selectedIndexes={new Set()}
        searchQuery=""
        readonly
        onToggleFile={onToggleFile}
        onToggleDirectory={jest.fn()}
      />,
    );
    // 只读且无匹配叶子：树体为空，只留下表头与「没有匹配文件」提示。
    expect(container.textContent).toContain("没有匹配文件");
    expect(container.textContent).not.toContain("x.bin");
  });

  it("部分选中时目录复选框为 mixed 并显示半选标记", () => {
    const nodes = fillDirectorySizes(buildTorrentTree([
      { index: 1, path: "A/x.bin", size: 10 },
      { index: 2, path: "A/y.bin", size: 20 },
    ]));
    const onToggleDirectory = jest.fn();
    render(
      <TorrentFileTree
        nodes={nodes}
        selectedIndexes={new Set([1])}
        searchQuery=""
        onToggleFile={jest.fn()}
        onToggleDirectory={onToggleDirectory}
      />,
    );
    const dirCheckbox = screen.getByLabelText("A");
    expect(dirCheckbox).toHaveAttribute("aria-checked", "mixed");
    expect(dirCheckbox.textContent).toBe("-");
    fireEvent.click(dirCheckbox);
    expect(onToggleDirectory).toHaveBeenCalledWith([1, 2]);
  });

  it("搜索无命中时仍渲染树但过滤掉不匹配叶子", () => {
    const nodes = fillDirectorySizes(buildTorrentTree([
      { index: 1, path: "A/x.bin", size: 10 },
      { index: 2, path: "B/y.txt", size: 20 },
    ]));
    const { container } = render(
      <TorrentFileTree
        nodes={nodes}
        selectedIndexes={new Set([1, 2])}
        searchQuery="y.txt"
        onToggleFile={jest.fn()}
        onToggleDirectory={jest.fn()}
      />,
    );
    expect(container.textContent).toContain("y.txt");
    expect(container.textContent).not.toContain("x.bin");
  });
});

describe("TorrentFileTree 未选中/空目录/文件节点", () => {
  it("全部未选中时所有目录复选框为未选状态", () => {
    const nodes = fillDirectorySizes(buildTorrentTree([
      { index: 1, path: "A/x.bin", size: 10 },
    ]));
    render(
      <TorrentFileTree
        nodes={nodes}
        selectedIndexes={new Set()}
        searchQuery=""
        onToggleFile={jest.fn()}
        onToggleDirectory={jest.fn()}
      />,
    );
    const checkbox = screen.getByLabelText("A");
    expect(checkbox).toHaveAttribute("aria-checked", "false");
    expect(checkbox.textContent).toBe("");
  });

  it("文件节点点击触发 onToggleFile", () => {
    const nodes = fillDirectorySizes(buildTorrentTree([
      { index: 7, path: "x.bin", size: 10 },
    ]));
    const onToggleFile = jest.fn();
    render(
      <TorrentFileTree
        nodes={nodes}
        selectedIndexes={new Set([7])}
        searchQuery=""
        onToggleFile={onToggleFile}
        onToggleDirectory={jest.fn()}
      />,
    );
    const fileCheckbox = screen.getByLabelText("x.bin");
    expect(fileCheckbox).toHaveAttribute("aria-checked", "true");
    fireEvent.click(fileCheckbox);
    expect(onToggleFile).toHaveBeenCalledWith(7);
  });

  it("只读模式渲染已选文件的勾选标记（非交互）", () => {
    const nodes = fillDirectorySizes(buildTorrentTree([
      { index: 7, path: "x.bin", size: 10 },
    ]));
    const { container } = render(
      <TorrentFileTree nodes={nodes} selectedIndexes={new Set([7])} searchQuery="" readonly />,
    );
    expect(container.querySelector(".torrent-check-checked")?.textContent).toBe("✓");
    expect(screen.queryByRole("checkbox")).not.toBeInTheDocument();
  });
});

describe("TaskCard 无 uri 分支", () => {
  it("uri 为空时不显示复制按钮，点击复制回调也不触发", () => {
    const onCopyUri = jest.fn();
    render(
      <TaskCard
        task={task({ status: "active", uri: null })}
        isSelected={false}
        isOperating={false}
        onToggleSelection={() => undefined}
        onCancel={() => undefined}
        onRetry={() => undefined}
        onCopyUri={onCopyUri}
      />,
    );
    expect(screen.queryByRole("button", { name: "复制" })).not.toBeInTheDocument();
    expect(onCopyUri).not.toHaveBeenCalled();
  });
});
