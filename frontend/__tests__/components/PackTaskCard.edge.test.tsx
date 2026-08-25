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
    folder_path: "/data/folder",
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
    step: null,
    started_at: null,
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

describe("PackTaskCard edge cases", () => {
  beforeEach(() => {
    jest.useFakeTimers();
    jest.clearAllMocks();
    showConfirm.mockResolvedValue(true);
  });

  afterEach(() => {
    jest.clearAllTimers();
    jest.useRealTimers();
    jest.restoreAllMocks();
  });

  it("renders cancelled tasks with delete action", async () => {
    mockApi.listPackTasks
      .mockResolvedValueOnce([makeTask({ id: 21, status: "cancelled", folder_path: "[not-json" })])
      .mockResolvedValueOnce([]);
    mockApi.deletePackTask.mockResolvedValue({ ok: true, message: "deleted" });

    render(<PackTaskCard />);

    await openDropdown();

    expect(screen.getByText("已取消")).toBeInTheDocument();
    expect(screen.getByText("[not-json")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "删除" }));
    await waitFor(() => {
      expect(mockApi.deletePackTask).toHaveBeenCalledWith(21);
    });
  });

  it("formats hour-scale durations for long running packing tasks", async () => {
    jest.setSystemTime(new Date("2025-01-01T02:01:01.000Z"));
    mockApi.listPackTasks.mockResolvedValue([
      makeTask({
        id: 22,
        status: "packing",
        progress: 50,
        step: "compressing",
        started_at: "2025-01-01T01:00:00.000Z",
      }),
    ]);

    render(<PackTaskCard />);
    await openDropdown();

    expect(
      screen.getByText("压缩 · 已用 1小时01分01秒 / 预计剩余 1小时01分01秒")
    ).toBeInTheDocument();
  });

  it("counts multi-file folder paths in the display name", async () => {
    mockApi.listPackTasks.mockResolvedValue([
      makeTask({ id: 23, status: "packing", folder_path: '["a.txt","b.txt","c.txt"]' }),
    ]);

    render(<PackTaskCard />);
    await openDropdown();

    expect(screen.getByText("3 个文件")).toBeInTheDocument();
  });

  it("logs an error when listing tasks fails", async () => {
    const errorSpy = jest.spyOn(console, "error").mockImplementation(() => {});
    mockApi.listPackTasks.mockRejectedValue(new Error("rpc down"));

    render(<PackTaskCard />);

    await waitFor(() => {
      expect(errorSpy).toHaveBeenCalledWith("Failed to load pack tasks:", expect.any(Error));
    });
    expect(screen.queryByRole("button", { name: /打包任务/ })).not.toBeInTheDocument();
    errorSpy.mockRestore();
  });

  it("keeps the dropdown open on mousedown inside the trigger or panel", async () => {
    mockApi.listPackTasks.mockResolvedValue([makeTask({ status: "pending", id: 1 })]);

    render(<PackTaskCard />);

    const trigger = await openDropdown();
    act(() => {
      fireEvent.mouseDown(trigger);
    });
    const panel = screen.getByText("排队中").closest(".pack-dropdown")!;
    act(() => {
      fireEvent.mouseDown(panel);
    });

    act(() => {
      jest.advanceTimersByTime(500);
    });
    expect(screen.getByText("排队中")).toBeInTheDocument();
  });

  it("collapses the dropdown on outside resize or scroll", async () => {
    mockApi.listPackTasks.mockResolvedValue([makeTask({ status: "pending", id: 1 })]);

    render(
      <div>
        <PackTaskCard />
        <div data-testid="outside">outside</div>
      </div>
    );

    await openDropdown();
    expect(screen.getByText("排队中")).toBeInTheDocument();

    const outside = screen.getByTestId("outside");
    act(() => {
      outside.dispatchEvent(new Event("resize", { bubbles: true }));
    });
    act(() => {
      jest.advanceTimersByTime(500);
    });

    await waitFor(() => {
      expect(screen.queryByText("排队中")).not.toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole("button", { name: /打包任务/ }));
    expect(screen.getByText("排队中")).toBeInTheDocument();

    act(() => {
      outside.dispatchEvent(new Event("scroll", { bubbles: true }));
    });
    act(() => {
      jest.advanceTimersByTime(500);
    });

    await waitFor(() => {
      expect(screen.queryByText("排队中")).not.toBeInTheDocument();
    });
  });

  it("ignores hover expand when already expanded", async () => {
    mockApi.listPackTasks.mockResolvedValue([makeTask({ status: "pending", id: 1 })]);

    render(<PackTaskCard />);

    const trigger = await openDropdown();
    fireEvent.mouseEnter(trigger);

    act(() => {
      jest.advanceTimersByTime(500);
    });
    expect(screen.getByText("排队中")).toBeInTheDocument();
  });

  it("falls back to a zero position when the button rect is unavailable", async () => {
    jest
      .spyOn(HTMLElement.prototype, "getBoundingClientRect")
      .mockReturnValue(undefined as never);
    mockApi.listPackTasks.mockResolvedValue([makeTask({ status: "pending", id: 1 })]);

    render(<PackTaskCard />);

    await openDropdown();

    const panel = screen.getByText("排队中").closest(".pack-dropdown") as HTMLElement;
    expect(panel).toHaveStyle({ top: "0px", right: "0px" });
  });

  it("does not cancel the task when confirmation is dismissed", async () => {
    mockApi.listPackTasks.mockResolvedValue([makeTask({ status: "pending", id: 1 })]);
    showConfirm.mockResolvedValue(false);

    render(<PackTaskCard />);

    await openDropdown();
    fireEvent.click(screen.getByRole("button", { name: "取消" }));

    await waitFor(() => {
      expect(showConfirm).toHaveBeenCalled();
    });
    expect(mockApi.cancelPackTask).not.toHaveBeenCalled();
  });

  it("does not delete the task when confirmation is dismissed", async () => {
    mockApi.listPackTasks.mockResolvedValue([makeTask({ status: "done", id: 2 })]);
    showConfirm.mockResolvedValue(false);

    render(<PackTaskCard />);

    await openDropdown();
    fireEvent.click(screen.getByRole("button", { name: "删除" }));

    await waitFor(() => {
      expect(showConfirm).toHaveBeenCalled();
    });
    expect(mockApi.deletePackTask).not.toHaveBeenCalled();
  });

  it("shows error toast when cancelling fails", async () => {
    mockApi.listPackTasks.mockResolvedValue([makeTask({ status: "pending", id: 1 })]);
    mockApi.cancelPackTask.mockRejectedValue(new Error("cancel failed"));

    render(<PackTaskCard />);

    await openDropdown();
    fireEvent.click(screen.getByRole("button", { name: "取消" }));

    await waitFor(() => {
      expect(showToast).toHaveBeenCalledWith("取消失败: cancel failed", "error");
    });
  });

  it("shows deleted-source hint and zero output size for compact done tasks", async () => {
    mockApi.listPackTasks.mockResolvedValue([
      makeTask({
        id: 3,
        status: "done",
        output_name: "compact.zip",
        output_size: null,
        delete_source: true,
      }),
    ]);

    render(<PackTaskCard />);

    await openDropdown();

    expect(screen.getByText("输出: 0 B · 已删除源文件")).toBeInTheDocument();
  });
});
