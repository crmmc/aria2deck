import { act, render, screen, waitFor } from "@testing-library/react";
import StatsWidget from "@/components/StatsWidget";
import { api } from "@/lib/api";

jest.mock("@/lib/api", () => ({
  __esModule: true,
  api: {
    getStats: jest.fn(),
  },
}));

const mockApi = api as jest.Mocked<typeof api>;
const GB = 1024 * 1024 * 1024;

const baseStats = {
  download_speed: 4 * 1024 * 1024,
  upload_speed: 512 * 1024,
  active_task_count: 1,
  disk_used_space: 80 * GB,
  disk_frozen_space: 5 * GB,
  disk_total_space: 100 * GB,
  disk_space_limited: true,
};

describe("StatsWidget", () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  afterEach(() => {
    jest.restoreAllMocks();
  });

  test("renders fetched stats, limited-space warning, and frozen space details", async () => {
    mockApi.getStats.mockResolvedValue(baseStats as never);

    render(<StatsWidget />);

    expect(await screen.findByText("可用空间")).toBeInTheDocument();
    expect(screen.getByText("15.0 GB")).toBeInTheDocument();
    expect(screen.getByText("/ 100.0 GB")).toBeInTheDocument();
    expect(
      screen.getByTitle("当前机器可分配空间已被其他任务占用，可用空间已按全局预算调整")
    ).toBeInTheDocument();
    expect(screen.getByText("已冻结: 5.0 GB (下载中)")).toBeInTheDocument();
    expect(document.querySelector(".progress-bar")).toHaveStyle({
      width: "80%",
      background: "var(--danger)",
    });
  });

  test("polls for refreshed stats and clamps invalid totals to zero percent", async () => {
    const setIntervalSpy = jest.spyOn(window, "setInterval").mockImplementation((callback) => {
      return 1 as never;
    });
    const clearIntervalSpy = jest.spyOn(window, "clearInterval");
    mockApi.getStats
      .mockResolvedValueOnce({
        ...baseStats,
        disk_used_space: 50,
        disk_total_space: 0,
        disk_frozen_space: 0,
        disk_space_limited: false,
      } as never)
      .mockResolvedValueOnce({
        ...baseStats,
        active_task_count: 2,
        disk_used_space: 10 * GB,
        disk_total_space: 20 * GB,
      } as never);

    const { unmount } = render(<StatsWidget />);

    expect(await screen.findByText("任务速度")).toBeInTheDocument();
    expect(setIntervalSpy).toHaveBeenCalledWith(expect.any(Function), 5000);
    expect(document.querySelector(".progress-bar")).toHaveStyle({ width: "0%" });

    const pollStats = setIntervalSpy.mock.calls[0]?.[0] as () => void;
    await act(async () => {
      pollStats();
      await Promise.resolve();
    });

    await waitFor(() => {
      expect(mockApi.getStats).toHaveBeenCalledTimes(2);
    });
    expect(document.querySelector(".stats-section-half .stats-value")).toHaveTextContent("2");

    unmount();

    expect(clearIntervalSpy).toHaveBeenCalledWith(1);
  });
});
