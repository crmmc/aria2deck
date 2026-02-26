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
    listPackTasks: jest.fn(),
    clearPackTasks: jest.fn(),
    cancelPackTask: jest.fn(),
    deletePackTask: jest.fn(),
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
    mockApi.listPackTasks.mockResolvedValue([] as never);

    render(<PackTaskCard />);

    await waitFor(() => {
      expect(mockApi.listPackTasks).toHaveBeenCalled();
    });
    expect(screen.queryByRole("button", { name: /打包任务/ })).not.toBeInTheDocument();
  });

  it("expands dropdown and can cancel active task", async () => {
    mockApi.listPackTasks.mockResolvedValue([makeTask({ status: "pending", id: 1 })] as never);
    mockApi.cancelPackTask.mockResolvedValue({ ok: true, message: "ok" } as never);

    render(<PackTaskCard />);

    const trigger = await screen.findByRole("button", { name: /打包任务/ });
    const wrapper = trigger.closest(".relative") as HTMLElement;
    fireEvent.mouseEnter(wrapper);

    act(() => {
      jest.advanceTimersByTime(20);
    });

    expect(screen.getByText(/排队中/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "取消" }));

    await waitFor(() => {
      expect(showConfirm).toHaveBeenCalled();
      expect(mockApi.cancelPackTask).toHaveBeenCalledWith(1);
    });
  });

  it("clears terminal tasks and shows success toast", async () => {
    mockApi.listPackTasks
      .mockResolvedValueOnce([makeTask({ status: "done", id: 2, output_name: "result.zip" })] as never)
      .mockResolvedValueOnce([] as never);
    mockApi.clearPackTasks.mockResolvedValue({ ok: true, count: 1 } as never);

    render(<PackTaskCard />);

    const trigger = await screen.findByRole("button", { name: /打包任务/ });
    const wrapper = trigger.closest(".relative") as HTMLElement;
    fireEvent.mouseEnter(wrapper);

    act(() => {
      jest.advanceTimersByTime(20);
    });

    fireEvent.click(screen.getByRole("button", { name: "清空已完成" }));

    await waitFor(() => {
      expect(mockApi.clearPackTasks).toHaveBeenCalled();
      expect(showToast).toHaveBeenCalledWith("已清空 1 条记录", "success");
    });
  });

  it("calls onTaskComplete when done task appears", async () => {
    const onTaskComplete = jest.fn();
    mockApi.listPackTasks.mockResolvedValue([makeTask({ status: "done", id: 3 })] as never);

    render(<PackTaskCard onTaskComplete={onTaskComplete} />);

    await waitFor(() => {
      expect(onTaskComplete).toHaveBeenCalledTimes(1);
    });
  });

  it("shows error toast when clear tasks fails", async () => {
    mockApi.listPackTasks.mockResolvedValue([makeTask({ status: "done", id: 4 })] as never);
    mockApi.clearPackTasks.mockRejectedValue(new Error("clear failed") as never);

    render(<PackTaskCard />);

    const trigger = await screen.findByRole("button", { name: /打包任务/ });
    const wrapper = trigger.closest(".relative") as HTMLElement;
    fireEvent.mouseEnter(wrapper);
    act(() => {
      jest.advanceTimersByTime(20);
    });

    fireEvent.click(screen.getByRole("button", { name: "清空已完成" }));

    await waitFor(() => {
      expect(showToast).toHaveBeenCalledWith("清空失败: clear failed", "error");
    });
  });

  it("deletes a done task when delete button clicked", async () => {
    mockApi.listPackTasks
      .mockResolvedValueOnce([makeTask({ status: "done", id: 5, output_name: "done.zip" })] as never)
      .mockResolvedValueOnce([] as never);
    mockApi.deletePackTask.mockResolvedValue({ ok: true, message: "Deleted" } as never);

    render(<PackTaskCard />);

    const trigger = await screen.findByRole("button", { name: /打包任务/ });
    const wrapper = trigger.closest(".relative") as HTMLElement;
    fireEvent.mouseEnter(wrapper);
    act(() => {
      jest.advanceTimersByTime(20);
    });

    fireEvent.click(screen.getByRole("button", { name: "删除" }));

    await waitFor(() => {
      expect(showConfirm).toHaveBeenCalledWith(expect.objectContaining({ title: "删除任务记录" }));
      expect(mockApi.deletePackTask).toHaveBeenCalledWith(5);
    });
  });

  it("shows error toast when delete task fails", async () => {
    mockApi.listPackTasks.mockResolvedValue([makeTask({ status: "failed", id: 6, error_message: "some error" })] as never);
    mockApi.deletePackTask.mockRejectedValue(new Error("delete failed") as never);

    render(<PackTaskCard />);

    const trigger = await screen.findByRole("button", { name: /打包任务/ });
    const wrapper = trigger.closest(".relative") as HTMLElement;
    fireEvent.mouseEnter(wrapper);
    act(() => {
      jest.advanceTimersByTime(20);
    });

    fireEvent.click(screen.getByRole("button", { name: "删除" }));

    await waitFor(() => {
      expect(showToast).toHaveBeenCalledWith("删除失败: delete failed", "error");
    });
  });
});
