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
  download_speed: 1024,
  upload_speed: 2048,
  active_task_count: 3,
  disk_used_space: 10 * GB,
  disk_frozen_space: 0,
  disk_total_space: 100 * GB,
  disk_space_limited: false,
};

describe("StatsWidget edge cases", () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  afterEach(() => {
    jest.restoreAllMocks();
  });

  it("clamps progress to 0% when computed percent is not finite", async () => {
    mockApi.getStats.mockResolvedValue({
      ...baseStats,
      disk_used_space: Infinity,
    } as never);

    render(<StatsWidget />);

    expect(await screen.findByText("可用空间")).toBeInTheDocument();
    expect(document.querySelector(".progress-bar")).toHaveStyle({ width: "0%" });
  });

  it("skips polling while the document is hidden or a fetch is in flight", async () => {
    const setIntervalSpy = jest.spyOn(window, "setInterval").mockImplementation(
      () => 1 as never
    );
    mockApi.getStats
      .mockResolvedValueOnce(baseStats as never)
      .mockImplementationOnce(() => new Promise(() => {}));

    render(<StatsWidget />);

    expect(await screen.findByText("任务速度")).toBeInTheDocument();
    const poll = setIntervalSpy.mock.calls[0]?.[0] as () => void;

    Object.defineProperty(document, "hidden", { value: true, configurable: true });
    await act(async () => {
      poll();
      await Promise.resolve();
    });
    expect(mockApi.getStats).toHaveBeenCalledTimes(1);

    Object.defineProperty(document, "hidden", { value: false, configurable: true });
    await act(async () => {
      poll();
      await Promise.resolve();
    });
    expect(mockApi.getStats).toHaveBeenCalledTimes(2);

    // 第一次请求尚未完成时不应发起下一次
    await act(async () => {
      poll();
      await Promise.resolve();
    });
    expect(mockApi.getStats).toHaveBeenCalledTimes(2);

    await waitFor(() => {
      expect(screen.getByText("3")).toBeInTheDocument();
    });
  });
});
