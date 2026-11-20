import { fireEvent, render, screen } from "@testing-library/react";

import { Aria2SettingsSection } from "@/features/settings/components/Aria2SettingsSection";
import { BasicSettingsSection } from "@/features/settings/components/BasicSettingsSection";
import { CredentialSecuritySection } from "@/features/settings/components/CredentialSecuritySection";
import { DownloadConnectionSettingsSection } from "@/features/settings/components/DownloadConnectionSettingsSection";
import { MachineDiskCard } from "@/features/settings/components/MachineDiskCard";
import { NumberSettingField } from "@/features/settings/components/NumberSettingField";
import { PackSettingsSection } from "@/features/settings/components/PackSettingsSection";
import { RateLimitSettingsSection } from "@/features/settings/components/RateLimitSettingsSection";
import { TrackerSettingsSection } from "@/features/settings/components/TrackerSettingsSection";
import { WebSocketSettingsSection } from "@/features/settings/components/WebSocketSettingsSection";
import { initialSettingsFormState } from "@/features/settings/settingsForm";
import type { TrackerStatusResponse } from "@/shared/api/types";

describe("MachineDiskCard", () => {
  it("renders every legend row with V1 percentages", () => {
    render(
      <MachineDiskCard
        stats={{
          total_bytes: 1000,
          used_bytes: 500,
          free_bytes: 500,
          download_used_bytes: 200,
          system_used_bytes: 300,
        }}
      />,
    );
    expect(screen.getByText(/下载占用：200\.0 B（20\.0%）/)).toBeInTheDocument();
    expect(screen.getByText(/系统占用：300\.0 B（30\.0%）/)).toBeInTheDocument();
    expect(screen.getByText(/总占用：500\.0 B（50\.0%）/)).toBeInTheDocument();
  });

  it("falls back to zero percent when the disk size is unknown", () => {
    render(
      <MachineDiskCard
        stats={{
          total_bytes: 0,
          used_bytes: 0,
          free_bytes: 0,
          download_used_bytes: 0,
          system_used_bytes: 0,
        }}
      />,
    );
    expect(screen.getAllByText(/0\.0%/)).toHaveLength(3);
  });
});

describe("NumberSettingField", () => {
  const renderField = (onChange: (value: number) => void) =>
    render(
      <NumberSettingField
        id="field-a"
        label="测试数值"
        description="说明"
        value={10}
        min={5}
        max={20}
        onChange={onChange}
      />,
    );

  it.each([
    { typed: "30", expected: 20 },
    { typed: "1", expected: 5 },
    { typed: "", expected: 5 },
    { typed: "12", expected: 12 },
  ])("clamps $typed into range", ({ typed, expected }) => {
    const onChange = jest.fn();
    renderField(onChange);
    fireEvent.change(screen.getByLabelText("测试数值"), { target: { value: typed } });
    expect(onChange).toHaveBeenCalledWith(expected);
  });
});

describe("DownloadConnectionSettingsSection", () => {
  const renderSection = (values: Record<string, string>, onChange = jest.fn()) =>
    render(<DownloadConnectionSettingsSection values={values} onChange={onChange} />);

  it("renders the six V2 keys and reports edits by config key", () => {
    const onChange = jest.fn();
    renderSection({}, onChange);
    expect(screen.getAllByRole("spinbutton")).toHaveLength(6);
    fireEvent.change(screen.getByLabelText("匿名总连接数"), { target: { value: "40" } });
    expect(onChange).toHaveBeenCalledWith("anonymous_total_connections", "40");
  });

  it.each([
    { typed: "2000", expected: "1000" },
    { typed: "abc", expected: "" },
    { typed: "", expected: "" },
    { typed: "37", expected: "37" },
  ])("clamps $typed without turning a blank into zero", ({ typed, expected }) => {
    const onChange = jest.fn();
    renderSection({ total_connections: "5" }, onChange);
    fireEvent.change(screen.getByLabelText("系统总下载连接上限"), { target: { value: typed } });
    expect(onChange).toHaveBeenCalledWith("total_connections", expected);
  });
});

describe("RateLimitSettingsSection", () => {
  it("renders all 11 V1 rows and falls back to the minimum for blank values", () => {
    const onChange = jest.fn();
    render(<RateLimitSettingsSection rateLimits={{}} onChange={onChange} />);
    expect(screen.getAllByRole("spinbutton")).toHaveLength(11);
    expect(screen.getByLabelText("账户安全限流")).toHaveValue(1);
    fireEvent.change(screen.getByLabelText("账户安全限流"), { target: { value: "7" } });
    expect(onChange).toHaveBeenCalledWith("account_security_rate_limit", "7");
  });
});

describe("PackSettingsSection", () => {
  it("switches the hint text with the selected format", () => {
    const onFieldChange = jest.fn();
    const tarForm = {
      ...initialSettingsFormState,
      packFormat: "tar.zst" as const,
      packCompressionLevel: "6",
    };
    const { rerender } = render(
      <PackSettingsSection
        form={{ ...tarForm, packFormat: "zip" }}
        onFieldChange={onFieldChange}
      />,
    );
    expect(
      screen.getByText("ZIP: 0 = 仅打包不压缩, 1 = 最快, 9 = 最慢/最小体积"),
    ).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("TAR + Zstandard"));
    expect(onFieldChange).toHaveBeenCalledWith("packFormat", "tar.zst");

    rerender(<PackSettingsSection form={tarForm} onFieldChange={onFieldChange} />);
    expect(
      screen.getByText("TAR+Zstandard: 0-9 会映射到 zstd 速度/压缩率档位"),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("TAR + Zstandard")).toBeChecked();
    fireEvent.click(screen.getByLabelText("ZIP（Deflate64）"));
    expect(onFieldChange).toHaveBeenCalledWith("packFormat", "zip");
    fireEvent.change(screen.getByLabelText("压缩等级"), { target: { value: "8" } });
    expect(onFieldChange).toHaveBeenCalledWith("packCompressionLevel", "8");
  });
});

describe("TrackerSettingsSection", () => {
  function status(overrides: Partial<TrackerStatusResponse> = {}): TrackerStatusResponse {
    return {
      items: [],
      entry_count: 0,
      remote_items: [],
      refresh_status: "never",
      failed_urls: [],
      updated_at_ms: null,
      last_refresh_at_ms: null,
      ...overrides,
    };
  }

  it("shows 从未刷新 for a never refreshed status and for a missing status", () => {
    const { unmount } = render(
      <TrackerSettingsSection
        form={initialSettingsFormState}
        trackerStatus={status()}
        refreshing={false}
        onFieldChange={jest.fn()}
        onRefresh={jest.fn()}
      />,
    );
    expect(screen.getByText(/上次刷新结果：从未刷新/)).toBeInTheDocument();
    expect(screen.getByText(/刷新时间：\s*从未刷新/)).toBeInTheDocument();
    unmount();

    render(
      <TrackerSettingsSection
        form={initialSettingsFormState}
        trackerStatus={null}
        refreshing={false}
        onFieldChange={jest.fn()}
        onRefresh={jest.fn()}
      />,
    );
    expect(screen.getByText(/上次刷新结果：从未刷新/)).toBeInTheDocument();
    expect(screen.getByText("当前条目数：0")).toBeInTheDocument();
  });

  it("edits the three tracker fields and disables the refresh button while busy", () => {
    const onFieldChange = jest.fn();
    const onRefresh = jest.fn();
    render(
      <TrackerSettingsSection
        form={initialSettingsFormState}
        trackerStatus={status({ entry_count: 3, refresh_status: "refreshing" })}
        refreshing
        onFieldChange={onFieldChange}
        onRefresh={onRefresh}
      />,
    );
    expect(screen.getByText(/上次刷新结果：刷新中/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("固定 tracker 列表"), {
      target: { value: "udp://t.example:6969" },
    });
    fireEvent.change(screen.getByLabelText("远程 tracker 列表 URL"), {
      target: { value: "https://t.example/list.txt" },
    });
    fireEvent.change(screen.getByLabelText("tracker 刷新间隔（分钟）"), {
      target: { value: "15" },
    });
    expect(onFieldChange).toHaveBeenCalledWith("trackerFixedListText", "udp://t.example:6969");
    expect(onFieldChange).toHaveBeenCalledWith(
      "trackerRemoteUrlsText",
      "https://t.example/list.txt",
    );
    expect(onFieldChange).toHaveBeenCalledWith("trackerRefreshIntervalMinutes", "15");
    expect(screen.getByRole("button", { name: "刷新中..." })).toBeDisabled();
  });
});

describe("CredentialSecuritySection", () => {
  it("switches to the busy label while invalidating", () => {
    const onInvalidate = jest.fn();
    render(<CredentialSecuritySection invalidating onInvalidate={onInvalidate} />);
    const button = screen.getByRole("button", { name: "作废全部 API Token 与 RPC Secret" });
    expect(button).toHaveTextContent("作废中...");
    expect(button).toBeDisabled();
    fireEvent.click(button);
    expect(onInvalidate).not.toHaveBeenCalled();
  });
});

describe("BasicSettingsSection", () => {
  it("reports every text field change", () => {
    const onFieldChange = jest.fn();
    render(<BasicSettingsSection form={initialSettingsFormState} onFieldChange={onFieldChange} />);
    fireEvent.change(screen.getByLabelText("网站标题"), { target: { value: "标题" } });
    fireEvent.change(screen.getByLabelText("最大任务大小 (GB)"), { target: { value: "7" } });
    fireEvent.change(screen.getByLabelText("最小剩余磁盘空间 (GB)"), { target: { value: "2" } });
    fireEvent.change(screen.getByLabelText("历史保留天数"), { target: { value: "45" } });
    expect(onFieldChange.mock.calls).toEqual([
      ["siteTitle", "标题"],
      ["maxTaskSizeGib", "7"],
      ["minFreeDiskGib", "2"],
      ["historyRetentionDays", "45"],
    ]);
  });
});

describe("WebSocketSettingsSection", () => {
  it("edits the single V2-compatible control", () => {
    const onFieldChange = jest.fn();
    render(
      <WebSocketSettingsSection form={initialSettingsFormState} onFieldChange={onFieldChange} />,
    );
    expect(screen.getByText(/最大重连延迟: 30 秒/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("最大重连延迟"), { target: { value: "120" } });
    expect(onFieldChange).toHaveBeenCalledWith("wsReconnectMaxDelaySeconds", "120");
  });
});

describe("Aria2SettingsSection", () => {
  const renderSection = (
    overrides: Partial<Parameters<typeof Aria2SettingsSection>[0]> = {},
  ) => {
    const onFieldChange = jest.fn();
    const onTestConnection = jest.fn();
    render(
      <Aria2SettingsSection
        form={initialSettingsFormState}
        aria2Status={{ connected: true, version: "1.37.0", features: [] }}
        testResult={null}
        testingConnection={false}
        onFieldChange={onFieldChange}
        onTestConnection={onTestConnection}
        {...overrides}
      />,
    );
    return { onFieldChange, onTestConnection };
  };

  it("reports URL, secret and BT timeout edits and triggers the test", () => {
    const { onFieldChange, onTestConnection } = renderSection();
    fireEvent.change(screen.getByLabelText("aria2 RPC URL"), { target: { value: "http://x/rpc" } });
    fireEvent.change(screen.getByLabelText("aria2 RPC Secret"), { target: { value: "s" } });
    fireEvent.change(screen.getByLabelText("BT 停止超时"), { target: { value: "120" } });
    fireEvent.click(screen.getByRole("button", { name: "测试连接" }));
    expect(onFieldChange.mock.calls).toEqual([
      ["aria2RpcUrl", "http://x/rpc"],
      ["aria2RpcSecret", "s"],
      ["aria2BtStopTimeoutSeconds", "120"],
    ]);
    expect(onTestConnection).toHaveBeenCalledTimes(1);
  });

  it("shows the busy label while testing", () => {
    renderSection({ testingConnection: true, testResult: null });
    expect(screen.getByRole("button", { name: "测试中..." })).toBeDisabled();
  });

  it("renders a successful test result with the version", () => {
    renderSection({ testResult: { connected: true, version: "1.38.0", features: [] } });
    expect(screen.getByText("测试结果：连接成功")).toBeInTheDocument();
    expect(screen.getByText(/aria2 版本: 1\.38\.0/)).toBeInTheDocument();
  });

  it("renders a disconnected banner without a version line when the probe has no version", () => {
    renderSection({ aria2Status: { connected: false, version: null, features: [] } });
    expect(screen.getByText("未连接")).toBeInTheDocument();
    expect(screen.getByText("无法连接 aria2，请检查 RPC 地址与 Secret。")).toBeInTheDocument();
    expect(screen.queryByText(/^aria2 1\./)).not.toBeInTheDocument();
  });
});
