import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { TrackerSettingsSection } from "@/app/(authenticated)/settings/_components/TrackerSettingsSection";
import { api } from "@/lib/api";
import type { TrackerStatus } from "@/types";

jest.mock("@/lib/api", () => ({
  __esModule: true,
  api: {
    refreshTrackers: jest.fn(),
  },
}));

const mockApi = api as jest.Mocked<typeof api>;

const baseForm = {
  trackerFixedList: "http://tracker.example.com/announce",
  trackerRemoteUrls: "https://example.com/list.txt",
  trackerRefreshIntervalMinutes: 30,
};

function makeStatus(overrides: Partial<TrackerStatus> = {}): TrackerStatus {
  return {
    entry_count: 120,
    updated_at_ms: 1700000000000,
    last_refresh_at_ms: 1700000000000,
    last_refresh_status: "ok",
    last_refresh_failed_urls: [],
    ...overrides,
  };
}

function setup(status: TrackerStatus | null = makeStatus()) {
  const onFieldChange = jest.fn();
  const onRefresh = jest.fn();
  render(
    <TrackerSettingsSection
      form={baseForm}
      trackerStatus={status}
      refreshing={false}
      onFieldChange={onFieldChange}
      onRefresh={onRefresh}
    />,
  );
  return { onFieldChange, onRefresh };
}

describe("TrackerSettingsSection", () => {
  test("渲染固定列表、远程 URL、刷新间隔三个控件并绑定表单值", () => {
    const { onFieldChange } = setup();

    const fixed = screen.getByLabelText("固定 tracker 列表");
    expect(fixed).toHaveValue(baseForm.trackerFixedList);
    const remote = screen.getByLabelText("远程 tracker 列表 URL");
    expect(remote).toHaveValue(baseForm.trackerRemoteUrls);
    const interval = screen.getByLabelText("tracker 刷新间隔（分钟）");
    expect(interval).toHaveValue(30);

    fireEvent.change(fixed, { target: { value: "udp://t.example.com:6969" } });
    expect(onFieldChange).toHaveBeenCalledWith("trackerFixedList", "udp://t.example.com:6969");
    fireEvent.change(remote, { target: { value: "https://a.com/b.txt" } });
    expect(onFieldChange).toHaveBeenCalledWith("trackerRemoteUrls", "https://a.com/b.txt");
    fireEvent.change(interval, { target: { value: "60" } });
    expect(onFieldChange).toHaveBeenCalledWith("trackerRefreshIntervalMinutes", 60);
  });

  test("状态区显示条目数与成功刷新结果", () => {
    setup(makeStatus());
    expect(screen.getByText(/当前条目数：120/)).toBeInTheDocument();
    expect(screen.getByText(/上次刷新结果：成功/)).toBeInTheDocument();
    expect(screen.queryByText(/失败源/)).not.toBeInTheDocument();
  });

  test("状态区支持部分失败、全部失败、从未刷新三种展示", () => {
    const { unmount } = render(
      <TrackerSettingsSection
        form={baseForm}
        trackerStatus={makeStatus({
          last_refresh_status: "partial",
          last_refresh_failed_urls: ["https://bad.example.com/l"],
        })}
        refreshing={false}
        onFieldChange={jest.fn()}
        onRefresh={jest.fn()}
      />,
    );
    expect(screen.getByText(/部分失败/)).toBeInTheDocument();
    expect(screen.getByText(/失败源 1 个/)).toBeInTheDocument();
    unmount();

    render(
      <TrackerSettingsSection
        form={baseForm}
        trackerStatus={makeStatus({ last_refresh_status: "failed" })}
        refreshing={false}
        onFieldChange={jest.fn()}
        onRefresh={jest.fn()}
      />,
    );
    expect(screen.getByText(/全部失败/)).toBeInTheDocument();
  });

  test("从未刷新状态（trackerStatus 为 null 或 never）显示从未刷新", () => {
    const { unmount } = render(
      <TrackerSettingsSection
        form={baseForm}
        trackerStatus={makeStatus({
          entry_count: 0,
          last_refresh_at_ms: null,
          last_refresh_status: "never",
        })}
        refreshing={false}
        onFieldChange={jest.fn()}
        onRefresh={jest.fn()}
      />,
    );
    expect(screen.getByText(/从未刷新/)).toBeInTheDocument();
    unmount();

    render(
      <TrackerSettingsSection
        form={baseForm}
        trackerStatus={null}
        refreshing={false}
        onFieldChange={jest.fn()}
        onRefresh={jest.fn()}
      />,
    );
    expect(screen.getByText(/从未刷新/)).toBeInTheDocument();
  });

  test("立即刷新按钮调用刷新回调并展示 loading 文案", async () => {
    const status = makeStatus();
    let resolveRefresh: (v: TrackerStatus) => void = () => {};
    mockApi.refreshTrackers.mockImplementation(
      () =>
        new Promise<TrackerStatus>((resolve) => {
          resolveRefresh = resolve;
        }),
    );
    const onRefresh = jest.fn(() => new Promise<void>(() => {}));

    const { rerender } = render(
      <TrackerSettingsSection
        form={baseForm}
        trackerStatus={status}
        refreshing={false}
        onFieldChange={jest.fn()}
        onRefresh={onRefresh}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "立即刷新" }));
    expect(onRefresh).toHaveBeenCalledTimes(1);

    rerender(
      <TrackerSettingsSection
        form={baseForm}
        trackerStatus={status}
        refreshing={true}
        onFieldChange={jest.fn()}
        onRefresh={onRefresh}
      />,
    );
    const button = screen.getByRole("button", { name: "刷新中..." });
    expect(button).toBeDisabled();
    resolveRefresh(status);
    await waitFor(() => expect(button).toBeInTheDocument());
  });
});
