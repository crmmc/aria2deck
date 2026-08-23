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
    error_message: null,
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

  it("renders dropdown in a portal attached to document.body on click", async () => {
    mockApi.listPackTasks.mockResolvedValue([makeTask({ status: "pending", id: 1 })]);
    const { container } = render(<PackTaskCard />);

    const trigger = await screen.findByRole("button", { name: /打包任务/ });
    expect(trigger).toHaveAttribute("aria-expanded", "false");

    fireEvent.click(trigger);

    const dropdown = screen.getByText(/排队中/).closest(".pack-dropdown");
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
    expect(screen.getByText(/排队中/)).toBeInTheDocument();

    fireEvent.click(trigger);
    await waitFor(() => {
      expect(screen.queryByText(/排队中/)).not.toBeInTheDocument();
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
    expect(screen.getByText(/排队中/)).toBeInTheDocument();

    fireEvent.mouseLeave(trigger);
    // 200ms 延迟内不应立刻收起（留出移入面板 8px 间隙的时间）
    act(() => {
      jest.advanceTimersByTime(150);
    });
    expect(screen.getByText(/排队中/)).toBeInTheDocument();

    act(() => {
      jest.advanceTimersByTime(500);
    });
    expect(screen.queryByText(/排队中/)).not.toBeInTheDocument();
  });

  it("cancels pending collapse when hovering the panel", async () => {
    mockApi.listPackTasks.mockResolvedValue([makeTask({ status: "pending", id: 1 })]);

    render(<PackTaskCard />);

    const trigger = await screen.findByRole("button", { name: /打包任务/ });
    fireEvent.mouseEnter(trigger);
    act(() => {
      jest.advanceTimersByTime(100);
    });
    const panel = screen.getByText(/排队中/).closest(".pack-dropdown");
    expect(panel).not.toBeNull();

    fireEvent.mouseLeave(trigger);
    act(() => {
      jest.advanceTimersByTime(100);
    });
    fireEvent.mouseEnter(panel!);
    act(() => {
      jest.advanceTimersByTime(500);
    });
    expect(screen.getByText(/排队中/)).toBeInTheDocument();
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
    expect(screen.getByText(/排队中/)).toBeInTheDocument();

    fireEvent.mouseDown(screen.getByTestId("outside"));

    await waitFor(() => {
      expect(screen.queryByText(/排队中/)).not.toBeInTheDocument();
    });
  });

  it("keeps dropdown open when scrolling inside the panel", async () => {
    mockApi.listPackTasks.mockResolvedValue([makeTask({ status: "pending", id: 1 })]);

    render(<PackTaskCard />);

    await openDropdown();
    expect(screen.getByText(/排队中/)).toBeInTheDocument();

    const panel = screen.getByText(/排队中/).closest(".pack-dropdown") as Node;
    fireEvent.scroll(panel, { bubbles: true });

    // 面板内滚动不应触发关闭（动画等待 400ms 后仍应存在）
    act(() => {
      jest.advanceTimersByTime(500);
    });
    expect(screen.getByText(/排队中/)).toBeInTheDocument();
  });

  it("closes dropdown on Escape", async () => {
    mockApi.listPackTasks.mockResolvedValue([makeTask({ status: "pending", id: 1 })]);

    render(<PackTaskCard />);

    await openDropdown();
    expect(screen.getByText(/排队中/)).toBeInTheDocument();

    fireEvent.keyDown(document, { key: "Escape" });

    await waitFor(() => {
      expect(screen.queryByText(/排队中/)).not.toBeInTheDocument();
    });
  });

  it("expands dropdown and can cancel active task", async () => {
    mockApi.listPackTasks.mockResolvedValue([makeTask({ status: "pending", id: 1 })]);
    mockApi.cancelPackTask.mockResolvedValue({ ok: true, message: "ok" });

    render(<PackTaskCard />);

    await openDropdown();

    expect(screen.getByText(/排队中/)).toBeInTheDocument();
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
