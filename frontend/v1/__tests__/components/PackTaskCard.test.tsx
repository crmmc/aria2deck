import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import PackTaskCard from "@/components/PackTaskCard";
import { api } from "@/lib/api";
import type { PackTask } from "@/types";

const showToast = jest.fn();
const showConfirm = jest.fn();

jest.mock("@/components/Toast", () => ({
  useToast: () => ({
    showToast,
    showConfirm,
  }),
}));

jest.mock("@/lib/api", () => ({
  api: {
    listPackTasks: jest.fn<Promise<PackTask[]>, []>(),
    clearPackTasks: jest.fn<Promise<{ ok: boolean; count: number }>, []>(),
    cancelPackTask: jest.fn<Promise<{ ok: boolean; message: string }>, [number]>(),
    deletePackTask: jest.fn<Promise<{ ok: boolean; message: string }>, [number]>(),
  },
}));

const mockApi = api as jest.Mocked<typeof api>;

function makeTask(overrides: Partial<PackTask> = {}): PackTask {
  return {
    id: 1,
    owner_id: 1,
    folder_path: "[\"a.txt\",\"b.txt\"]",
    folder_size: 1000,
    reserved_space: 1000,
    output_path: "/tmp/out.zip",
    output_name: null,
    output_size: 512,
    stored_file_id: null,
    delete_source: false,
    status: "pending",
    progress: 10,
    step_progress: 0,
    error_message: null,
    step: null,
    started_at: null,
    step_started_at: null,
    created_at: new Date().toISOString(),
    updated_at: new Date().toISOString(),
    ...overrides,
  };
}

async function openDropdown() {
  const trigger = await screen.findByRole("button", { name: /打包任务/ });
  fireEvent.click(trigger);
  return trigger;
}

describe("PackTaskCard", () => {
  beforeEach(() => {
    jest.useFakeTimers();
    jest.clearAllMocks();
    showConfirm.mockResolvedValue(true);
  });

  afterEach(() => {
    jest.clearAllTimers();
    jest.useRealTimers();
  });

  it("renders nothing when there are no tasks", async () => {
    mockApi.listPackTasks.mockResolvedValue([]);

    render(<PackTaskCard />);

    await waitFor(() => {
      expect(mockApi.listPackTasks).toHaveBeenCalled();
    });
    expect(screen.queryByRole("button", { name: /打包任务/ })).not.toBeInTheDocument();
  });

  it("shows the approved pending footer", async () => {
    mockApi.listPackTasks.mockResolvedValue([makeTask({
      status: "pending",
      step: null,
      started_at: null,
    })]);

    render(<PackTaskCard />);
    await openDropdown();

    const footerText = screen.getByText("排队中");
    expect(footerText.closest(".flex-between")).toContainElement(
      screen.getByRole("button", { name: "取消" })
    );
    expect(screen.queryByText(/已用|已预留/)).not.toBeInTheDocument();
  });

  it("shows the approved step, elapsed time and local ETA without extra requests", async () => {
    jest.setSystemTime(new Date("2025-01-01T00:00:30.000Z"));
    mockApi.listPackTasks.mockResolvedValue([makeTask({
      status: "packing",
      progress: 52,
      step_progress: 25,
      step: "compressing",
      started_at: "2024-01-01T00:00:00.000Z",
      step_started_at: "2025-01-01T00:00:00.000Z",
    })]);

    render(<PackTaskCard />);
    await openDropdown();

    const footerText = screen.getByText("压缩 · 已用 30秒 / 预计剩余 1分30秒");
    const progressFill = document.querySelector(".pack-progress-fill");
    expect(progressFill).toHaveStyle({ width: "25%" });
    expect(footerText.closest(".flex-between")).toContainElement(
      screen.getByRole("button", { name: "取消" })
    );
    expect(screen.queryByText(/已预留/)).not.toBeInTheDocument();

    act(() => {
      jest.advanceTimersByTime(1000);
    });

    expect(screen.getByText("压缩 · 已用 31秒 / 预计剩余 1分33秒")).toBeInTheDocument();
    expect(mockApi.listPackTasks).toHaveBeenCalledTimes(1);
  });

  it("shows all approved steps and safe placeholders for unknown step or invalid timing", async () => {
    jest.setSystemTime(new Date("2025-01-01T01:00:00.000Z"));
    mockApi.listPackTasks.mockResolvedValue([
      makeTask({ id: 8, status: "packing", progress: 50, step_progress: 50, step: "validating", step_started_at: "invalid" }),
      makeTask({ id: 9, status: "packing", progress: 0, step_progress: 0, step: "compressing", step_started_at: "2025-01-01T00:59:50.000Z" }),
      makeTask({ id: 10, status: "packing", progress: 50, step_progress: 50, step: "verifying", step_started_at: "2025-01-01T00:59:50.000Z" }),
      makeTask({ id: 11, status: "packing", progress: 50, step_progress: 50, step: "unexpected" as PackTask["step"], step_started_at: null }),
    ]);

    render(<PackTaskCard />);
    await openDropdown();

    expect(screen.getByText("校验 · 已用 -- / 预计剩余 --")).toBeInTheDocument();
    expect(screen.getByText("压缩 · 已用 -- / 预计剩余 --")).toBeInTheDocument();
    expect(screen.getByText("验收 · 已用 10秒 / 预计剩余 10秒")).toBeInTheDocument();
    expect(screen.getByText("处理中 · 已用 -- / 预计剩余 --")).toBeInTheDocument();
  });

  it("shows zero ETA at step completion and a placeholder for a future step start", async () => {
    jest.setSystemTime(new Date("2025-01-01T01:00:00.000Z"));
    mockApi.listPackTasks.mockResolvedValue([
      makeTask({
        id: 12,
        status: "packing",
        step_progress: 100,
        step: "compressing",
        step_started_at: "2025-01-01T00:59:50.000Z",
      }),
      makeTask({
        id: 13,
        status: "packing",
        step_progress: 50,
        step: "validating",
        step_started_at: "2025-01-01T01:00:01.000Z",
      }),
    ]);

    render(<PackTaskCard />);
    await openDropdown();

    expect(screen.getByText("压缩 · 已用 10秒 / 预计剩余 0秒")).toBeInTheDocument();
    expect(screen.getByText("校验 · 已用 -- / 预计剩余 --")).toBeInTheDocument();
  });

  it("uses the new step start after polling switches the active phase", async () => {
    jest.setSystemTime(new Date("2025-01-01T00:01:00.000Z"));
    mockApi.listPackTasks
      .mockResolvedValueOnce([makeTask({
        id: 14,
        status: "packing",
        progress: 20,
        step_progress: 50,
        step: "validating",
        step_started_at: "2025-01-01T00:00:00.000Z",
      })])
      .mockResolvedValueOnce([makeTask({
        id: 14,
        status: "packing",
        progress: 50,
        step_progress: 25,
        step: "compressing",
        step_started_at: "2025-01-01T00:00:59.000Z",
      })]);

    render(<PackTaskCard />);
    await openDropdown();
    expect(
      screen.getByText("校验 · 已用 1分00秒 / 预计剩余 1分00秒")
    ).toBeInTheDocument();

    await act(async () => {
      jest.advanceTimersByTime(2000);
    });

    await waitFor(() => {
      expect(
        screen.getByText("压缩 · 已用 3秒 / 预计剩余 9秒")
      ).toBeInTheDocument();
    });
    expect(mockApi.listPackTasks).toHaveBeenCalledTimes(2);
  });

  it("does not show timing for terminal tasks", async () => {
    mockApi.listPackTasks.mockResolvedValue([
      makeTask({ id: 12, status: "done", step: "verifying", started_at: "2025-01-01T00:00:00.000Z" }),
    ]);

    render(<PackTaskCard />);
    await openDropdown();

    expect(screen.getByText("已完成")).toBeInTheDocument();
    expect(screen.queryByText(/已用|预计剩余/)).not.toBeInTheDocument();
  });

  it("renders dropdown in a portal attached to document.body on click", async () => {
    mockApi.listPackTasks.mockResolvedValue([makeTask({ status: "pending", id: 1 })]);
    const { container } = render(<PackTaskCard />);

    const trigger = await screen.findByRole("button", { name: /打包任务/ });
    expect(trigger).toHaveAttribute("aria-expanded", "false");

    fireEvent.click(trigger);

    const dropdown = screen.getByText("排队中").closest(".pack-dropdown");
    expect(dropdown).not.toBeNull();
    expect(dropdown?.parentElement).toBe(document.body);
    expect(dropdown?.className).not.toMatch(/\bcard\b/);
    expect(container.contains(dropdown)).toBe(false);
    expect(trigger).toHaveAttribute("aria-expanded", "true");
  });

  it("toggles dropdown closed when clicking the button again", async () => {
    mockApi.listPackTasks.mockResolvedValue([makeTask({ status: "pending", id: 1 })]);

    render(<PackTaskCard />);

    const trigger = await screen.findByRole("button", { name: /打包任务/ });
    fireEvent.click(trigger);
    expect(screen.getByText("排队中")).toBeInTheDocument();

    fireEvent.click(trigger);
    await waitFor(() => {
      expect(screen.queryByText("排队中")).not.toBeInTheDocument();
    });
  });

  it("expands dropdown on button hover and collapses after 200ms delay on leave", async () => {
    mockApi.listPackTasks.mockResolvedValue([makeTask({ status: "pending", id: 1 })]);

    render(<PackTaskCard />);

    const trigger = await screen.findByRole("button", { name: /打包任务/ });
    fireEvent.mouseEnter(trigger);
    act(() => {
      jest.advanceTimersByTime(100);
    });
    expect(screen.getByText("排队中")).toBeInTheDocument();

    fireEvent.mouseLeave(trigger);
    // 200ms 延迟内不应立刻收起（留出移入面板 8px 间隙的时间）
    act(() => {
      jest.advanceTimersByTime(150);
    });
    expect(screen.getByText("排队中")).toBeInTheDocument();

    act(() => {
      jest.advanceTimersByTime(500);
    });
    expect(screen.queryByText("排队中")).not.toBeInTheDocument();
  });

  it("cancels pending collapse when hovering the panel", async () => {
    mockApi.listPackTasks.mockResolvedValue([makeTask({ status: "pending", id: 1 })]);

    render(<PackTaskCard />);

    const trigger = await screen.findByRole("button", { name: /打包任务/ });
    fireEvent.mouseEnter(trigger);
    act(() => {
      jest.advanceTimersByTime(100);
    });
    const panel = screen.getByText("排队中").closest(".pack-dropdown");
    expect(panel).not.toBeNull();

    fireEvent.mouseLeave(trigger);
    act(() => {
      jest.advanceTimersByTime(100);
    });
    fireEvent.mouseEnter(panel!);
    act(() => {
      jest.advanceTimersByTime(500);
    });
    expect(screen.getByText("排队中")).toBeInTheDocument();
  });

  it("closes dropdown on outside mousedown", async () => {
    mockApi.listPackTasks.mockResolvedValue([makeTask({ status: "pending", id: 1 })]);

    render(
      <div>
        <PackTaskCard />
        <div data-testid="outside">outside</div>
      </div>
    );

    await openDropdown();
    expect(screen.getByText("排队中")).toBeInTheDocument();

    fireEvent.mouseDown(screen.getByTestId("outside"));

    await waitFor(() => {
      expect(screen.queryByText("排队中")).not.toBeInTheDocument();
    });
  });

  it("keeps dropdown open when scrolling inside the panel", async () => {
    mockApi.listPackTasks.mockResolvedValue([makeTask({ status: "pending", id: 1 })]);

    render(<PackTaskCard />);

    await openDropdown();
    expect(screen.getByText("排队中")).toBeInTheDocument();

    const panel = screen.getByText("排队中").closest(".pack-dropdown") as Node;
    fireEvent.scroll(panel, { bubbles: true });

    // 面板内滚动不应触发关闭（动画等待 400ms 后仍应存在）
    act(() => {
      jest.advanceTimersByTime(500);
    });
    expect(screen.getByText("排队中")).toBeInTheDocument();
  });

  it("closes dropdown on Escape", async () => {
    mockApi.listPackTasks.mockResolvedValue([makeTask({ status: "pending", id: 1 })]);

    render(<PackTaskCard />);

    await openDropdown();
    expect(screen.getByText("排队中")).toBeInTheDocument();

    fireEvent.keyDown(document, { key: "Escape" });

    await waitFor(() => {
      expect(screen.queryByText("排队中")).not.toBeInTheDocument();
    });
  });

  it("expands dropdown and can cancel active task", async () => {
    mockApi.listPackTasks.mockResolvedValue([makeTask({ status: "pending", id: 1 })]);
    mockApi.cancelPackTask.mockResolvedValue({ ok: true, message: "ok" });

    render(<PackTaskCard />);

    await openDropdown();

    expect(screen.getByText("排队中")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "取消" }));

    await waitFor(() => {
      expect(showConfirm).toHaveBeenCalled();
      expect(mockApi.cancelPackTask).toHaveBeenCalledWith(1);
    });
  });

  it("clears terminal tasks and shows success toast", async () => {
    mockApi.listPackTasks
      .mockResolvedValueOnce([makeTask({ status: "done", id: 2, output_name: "result.zip" })])
      .mockResolvedValueOnce([]);
    mockApi.clearPackTasks.mockResolvedValue({ ok: true, count: 1 });

    render(<PackTaskCard />);

    await openDropdown();

    fireEvent.click(screen.getByRole("button", { name: "清空已完成" }));

    await waitFor(() => {
      expect(mockApi.clearPackTasks).toHaveBeenCalled();
      expect(showToast).toHaveBeenCalledWith("已清空 1 条记录", "success");
    });
  });

  it("does not call onTaskComplete for tasks that are already done on first load", async () => {
    const onTaskComplete = jest.fn();
    mockApi.listPackTasks.mockResolvedValue([makeTask({ status: "done", id: 3 })]);

    render(<PackTaskCard onTaskComplete={onTaskComplete} />);

    await openDropdown();
    expect(await screen.findByText("已完成")).toBeInTheDocument();
    expect(onTaskComplete).not.toHaveBeenCalled();
  });

  it("calls onTaskComplete when an active task becomes done after polling", async () => {
    const onTaskComplete = jest.fn();
    mockApi.listPackTasks
      .mockResolvedValueOnce([makeTask({ status: "packing", id: 7, progress: 50 })])
      .mockResolvedValueOnce([makeTask({ status: "done", id: 7, progress: 100 })]);

    render(<PackTaskCard onTaskComplete={onTaskComplete} />);

    expect(await screen.findByRole("button", { name: /打包任务/ })).toBeInTheDocument();

    await act(async () => {
      jest.advanceTimersByTime(2000);
    });

    await waitFor(() => {
      expect(onTaskComplete).toHaveBeenCalledTimes(1);
    });
  });

  it("shows error toast when clear tasks fails", async () => {
    mockApi.listPackTasks.mockResolvedValue([makeTask({ status: "done", id: 4 })]);
    mockApi.clearPackTasks.mockRejectedValue(new Error("clear failed"));

    render(<PackTaskCard />);

    await openDropdown();

    fireEvent.click(screen.getByRole("button", { name: "清空已完成" }));

    await waitFor(() => {
      expect(showToast).toHaveBeenCalledWith("清空失败: clear failed", "error");
    });
  });

  it("deletes a done task when delete button clicked", async () => {
    mockApi.listPackTasks
      .mockResolvedValueOnce([makeTask({ status: "done", id: 5, output_name: "done.zip" })])
      .mockResolvedValueOnce([]);
    mockApi.deletePackTask.mockResolvedValue({ ok: true, message: "Deleted" });

    render(<PackTaskCard />);

    await openDropdown();

    fireEvent.click(screen.getByRole("button", { name: "删除" }));

    await waitFor(() => {
      expect(showConfirm).toHaveBeenCalledWith(expect.objectContaining({ title: "删除任务记录" }));
      expect(mockApi.deletePackTask).toHaveBeenCalledWith(5);
    });
  });

  it("shows error toast when delete task fails", async () => {
    mockApi.listPackTasks.mockResolvedValue([makeTask({ status: "failed", id: 6, error_message: "some error" })]);
    mockApi.deletePackTask.mockRejectedValue(new Error("delete failed"));

    render(<PackTaskCard />);

    await openDropdown();

    fireEvent.click(screen.getByRole("button", { name: "删除" }));

    await waitFor(() => {
      expect(showToast).toHaveBeenCalledWith("删除失败: delete failed", "error");
    });
  });
});
