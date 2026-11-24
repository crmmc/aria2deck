import { fireEvent, render, screen } from "@testing-library/react";

import { AdvancedSettingsSection } from "@/features/settings/components/AdvancedSettingsSection";
import { Aria2SettingsSection } from "@/features/settings/components/Aria2SettingsSection";
import { BasicSettingsSection } from "@/features/settings/components/BasicSettingsSection";
import { CredentialSecuritySection } from "@/features/settings/components/CredentialSecuritySection";
import { DownloadConnectionSettingsSection } from "@/features/settings/components/DownloadConnectionSettingsSection";
import { MachineDiskCard } from "@/features/settings/components/MachineDiskCard";
import { NumberSettingField } from "@/features/settings/components/NumberSettingField";
import { PackSettingsSection } from "@/features/settings/components/PackSettingsSection";
import {
  RateLimitFields,
  RateLimitSettingsSection,
} from "@/features/settings/components/RateLimitSettingsSection";
import { TrackerSettingsSection } from "@/features/settings/components/TrackerSettingsSection";
import { WebSocketSettingsSection } from "@/features/settings/components/WebSocketSettingsSection";
import {
  initialSettingsFormState,
  RATE_LIMIT_FIELDS,
} from "@/features/settings/settingsForm";
import type { MachineStatsResponse, TrackerStatusResponse } from "@/shared/api/types";
import { formatAdminDateTime } from "@/shared/lib/format";

const machineStats: MachineStatsResponse = {
  total_bytes: 1000,
  used_bytes: 500,
  free_bytes: 500,
  download_used_bytes: 200,
  system_used_bytes: 300,
};

describe("MachineDiskCard", () => {
  it("puts system, download and total usage first, second and third in both meter and legend", () => {
    render(<MachineDiskCard stats={machineStats} />);

    const meter = screen.getByRole("meter", { name: "机器磁盘总占用 50.0%" });
    expect(meter.children[0]).toHaveClass("machine-system");
    expect(meter.children[1]).toHaveClass("machine-download");
    const legend = document.querySelector(".machine-legend");
    expect(Array.from(legend?.children ?? []).map((item) => item.textContent)).toEqual([
      "系统占用 300 B · 30.0%",
      "下载占用 200 B · 20.0%",
      "总占用 500 B · 50.0%",
    ]);
    expect(screen.getByText("共 1000 B")).toBeInTheDocument();
    expect(document.querySelector(".machine-heading strong")).toHaveTextContent("500 B 可用");
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
    expect(screen.getByRole("meter")).toHaveAttribute("aria-valuenow", "0");
  });
});

describe("NumberSettingField", () => {
  const renderField = (onChange: (value: string) => void) =>
    render(
      <NumberSettingField
        id="field-a"
        label="测试数值"
        description="说明"
        value="10"
        min={5}
        max={20}
        unit="GB"
        onChange={onChange}
      />,
    );

  it.each(["30", "1", "", "12"])("preserves the raw input %j", (typed) => {
    const onChange = jest.fn();
    renderField(onChange);
    fireEvent.change(screen.getByLabelText("测试数值"), { target: { value: typed } });
    expect(onChange).toHaveBeenCalledWith(typed);
  });

  it("keeps a readonly unit and a fixed error row", () => {
    const view = render(
      <NumberSettingField
        id="field-a"
        label="测试数值"
        description="说明"
        value="10"
        onChange={jest.fn()}
      />,
    );
    expect(document.querySelector(".field-error")).toHaveTextContent("");
    expect(screen.queryByLabelText("测试数值单位")).not.toBeInTheDocument();

    view.rerender(
      <NumberSettingField
        id="field-a"
        label="测试数值"
        description="说明"
        value="10"
        unit="GB"
        error="数值无效"
        onChange={jest.fn()}
      />,
    );
    expect(screen.getByLabelText("测试数值单位")).toHaveValue("GB");
    expect(screen.getByLabelText("测试数值单位")).toHaveAttribute("readonly");
    expect(screen.getByRole("alert")).toHaveTextContent("数值无效");
    expect(screen.getByLabelText("测试数值")).toHaveAttribute("aria-invalid", "true");
  });

  it("uses the minimum for a blank range value without writing it back", () => {
    const onChange = jest.fn();
    render(
      <NumberSettingField
        id="field-range"
        label="范围数值"
        description="说明"
        value=""
        min={0}
        max={22}
        rangeMax={0}
        error="范围无效"
        onChange={onChange}
      />,
    );
    expect(screen.getByLabelText("范围数值")).toHaveValue("0");
    expect(screen.getByLabelText("范围数值数值")).toHaveValue(null);
    expect(screen.getByLabelText("范围数值数值")).toHaveAttribute("aria-invalid", "true");
    expect(onChange).not.toHaveBeenCalled();
  });
});

describe("DownloadConnectionSettingsSection", () => {
  it("renders all six V2 keys with units and reports edits by config key", () => {
    const onChange = jest.fn();
    render(<DownloadConnectionSettingsSection values={{}} onChange={onChange} />);
    expect(screen.getAllByRole("spinbutton")).toHaveLength(6);
    expect(screen.getAllByLabelText(/单位$/)).toHaveLength(6);
    fireEvent.change(screen.getByLabelText("匿名总连接数"), { target: { value: "40" } });
    expect(onChange).toHaveBeenCalledWith("anonymous_total_connections", "40");
  });

  it.each([
    { typed: "2000", expected: "2000" },
    { typed: "abc", expected: "" },
    { typed: "", expected: "" },
    { typed: "37", expected: "37" },
  ])("preserves $typed without clamping or turning a blank into zero", ({ typed, expected }) => {
    const onChange = jest.fn();
    render(<DownloadConnectionSettingsSection values={{ total_connections: "5" }} onChange={onChange} />);
    fireEvent.change(screen.getByLabelText("系统总下载连接上限"), { target: { value: typed } });
    expect(onChange).toHaveBeenCalledWith("total_connections", expected);
  });
});

describe("RateLimitSettingsSection", () => {
  it("keeps the two Demo fields separate from the nine advanced fields", () => {
    const onChange = jest.fn();
    const view = render(
      <RateLimitFields
        fields={RATE_LIMIT_FIELDS.slice(0, 2)}
        rateLimits={{}}
        onChange={onChange}
      />,
    );
    expect(screen.getAllByRole("spinbutton")).toHaveLength(2);
    expect(screen.getByLabelText("账户安全限流")).toHaveValue(null);
    expect(screen.getByLabelText("普通已登录 API 限流")).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("账户安全限流"), { target: { value: "7" } });
    expect(onChange).toHaveBeenCalledWith("account_security_rate_limit", "7");

    view.rerender(
      <RateLimitFields
        fields={RATE_LIMIT_FIELDS.slice(2)}
        rateLimits={{}}
        onChange={onChange}
      />,
    );
    expect(screen.getAllByRole("spinbutton")).toHaveLength(9);
    expect(screen.queryByLabelText("账户安全限流")).not.toBeInTheDocument();
    expect(screen.getByLabelText("文件搜索限流")).toBeInTheDocument();
  });

  it("still provides the complete eleven-field advanced section", () => {
    render(<RateLimitSettingsSection rateLimits={{}} onChange={jest.fn()} />);
    expect(screen.getAllByRole("spinbutton")).toHaveLength(11);
  });
});

describe("PackSettingsSection", () => {
  it("uses count-free radio segments and natural format labels", () => {
    const onFieldChange = jest.fn();
    render(<PackSettingsSection form={initialSettingsFormState} onFieldChange={onFieldChange} />);
    expect(screen.getByRole("radiogroup", { name: "打包格式" })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "ZIP · Deflate64" })).toBeChecked();
    fireEvent.click(screen.getByRole("radio", { name: "TAR · Zstandard" }));
    expect(onFieldChange).toHaveBeenCalledWith("packFormat", "tar.zst");
    expect(document.querySelector(".pack-format-options .segment span")).toBeNull();
  });

  it("keeps stored values 10-22 in state and the number input while the slider stops at nine", () => {
    const onFieldChange = jest.fn();
    render(
      <PackSettingsSection
        form={{ ...initialSettingsFormState, packCompressionLevel: "22" }}
        onFieldChange={onFieldChange}
      />,
    );
    const range = screen.getByLabelText("压缩等级");
    const number = screen.getByLabelText("压缩等级数值");
    expect(range).toHaveValue("9");
    expect(range).toHaveAttribute("max", "9");
    expect(range).toHaveStyle({ "--range": "100%" });
    expect(number).toHaveValue(22);
    expect(number).toHaveAttribute("max", "22");
    fireEvent.change(number, { target: { value: "21" } });
    expect(onFieldChange).toHaveBeenCalledWith("packCompressionLevel", "21");
    expect(onFieldChange).not.toHaveBeenCalledWith("packCompressionLevel", "9");
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

  function renderStatus(trackerStatus: TrackerStatusResponse | null, refreshing = false) {
    return render(
      <TrackerSettingsSection
        form={initialSettingsFormState}
        trackerStatus={trackerStatus}
        refreshing={refreshing}
        onFieldChange={jest.fn()}
        onRefresh={jest.fn()}
      />,
    );
  }

  it("does not invent a healthy or empty status when the request failed", () => {
    renderStatus(null);
    expect(screen.getByText("Tracker 状态不可用")).toBeInTheDocument();
    expect(screen.queryByText("当前共 0 条 Tracker")).not.toBeInTheDocument();
  });

  it("shows partial failure, its real source and refresh time without calling it total failure", () => {
    const timestamp = Date.UTC(2026, 10, 23, 8, 30);
    renderStatus(status({
      entry_count: 42,
      refresh_status: "partial",
      failed_urls: ["https://bad.example/list"],
      last_refresh_at_ms: timestamp,
    }));
    expect(screen.getByText("当前共 42 条 Tracker")).toBeInTheDocument();
    expect(screen.getByText(new RegExp(`上次刷新部分失败（失败源 1 个）.*${formatAdminDateTime(timestamp)}`))).toBeInTheDocument();
    expect(screen.getByText("失败源：https://bad.example/list")).toBeInTheDocument();
    expect(screen.queryByText(/全部失败/)).not.toBeInTheDocument();
  });

  it.each([
    { refreshStatus: "success" as const, text: "上次刷新成功" },
    { refreshStatus: "failed" as const, text: "上次刷新全部失败" },
    { refreshStatus: "refreshing" as const, text: "上次刷新刷新中" },
    { refreshStatus: "never" as const, text: "上次刷新从未刷新" },
  ])("renders the real $refreshStatus refresh result", ({ refreshStatus, text }) => {
    renderStatus(status({ refresh_status: refreshStatus }));
    expect(screen.getByText(new RegExp(text))).toBeInTheDocument();
  });

  it("edits all three fields and disables refresh while busy", () => {
    const onFieldChange = jest.fn();
    const onRefresh = jest.fn();
    render(
      <TrackerSettingsSection
        form={initialSettingsFormState}
        trackerStatus={status()}
        refreshing
        onFieldChange={onFieldChange}
        onRefresh={onRefresh}
      />,
    );
    fireEvent.change(screen.getByLabelText("固定 tracker 列表"), {
      target: { value: "udp://t.example:6969" },
    });
    fireEvent.change(screen.getByLabelText("远程 tracker 列表 URL"), {
      target: { value: "https://t.example/list.txt" },
    });
    fireEvent.change(screen.getByLabelText("自动刷新间隔"), { target: { value: "15" } });
    expect(onFieldChange.mock.calls).toEqual([
      ["trackerFixedListText", "udp://t.example:6969"],
      ["trackerRemoteUrlsText", "https://t.example/list.txt"],
      ["trackerRefreshIntervalMinutes", "15"],
    ]);
    expect(screen.getByRole("button", { name: "正在刷新" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "正在刷新" }));
    expect(onRefresh).not.toHaveBeenCalled();
  });
});

describe("CredentialSecuritySection", () => {
  it("keeps the real danger action and switches to the busy label", () => {
    const onInvalidate = jest.fn();
    const view = render(<CredentialSecuritySection invalidating={false} onInvalidate={onInvalidate} />);
    fireEvent.click(screen.getByRole("button", { name: "作废全部 API Token 与 RPC Secret" }));
    expect(onInvalidate).toHaveBeenCalledTimes(1);

    view.rerender(<CredentialSecuritySection invalidating onInvalidate={onInvalidate} />);
    expect(screen.getByRole("button", { name: "作废全部 API Token 与 RPC Secret" })).toBeDisabled();
    expect(screen.getByText("作废中...")).toBeInTheDocument();
  });
});

describe("AdvancedSettingsSection", () => {
  it("reveals additional real settings only after expanding", () => {
    render(
      <AdvancedSettingsSection>
        <div>额外真实字段</div>
      </AdvancedSettingsSection>,
    );
    const toggle = screen.getByRole("button", { name: /系统高级设置/ });
    expect(screen.queryByText("额外真实字段")).not.toBeInTheDocument();
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("额外真实字段")).toBeInTheDocument();
    fireEvent.click(toggle);
    expect(screen.queryByText("额外真实字段")).not.toBeInTheDocument();
  });
});

describe("BasicSettingsSection", () => {
  it("reports all four real fields and shows units and error placeholders", () => {
    const onFieldChange = jest.fn();
    render(<BasicSettingsSection form={initialSettingsFormState} onFieldChange={onFieldChange} />);
    expect(screen.queryByLabelText("机器磁盘空间")).not.toBeInTheDocument();
    expect(document.querySelectorAll(".field-error")).toHaveLength(4);
    expect(screen.getByLabelText("最大任务大小单位")).toHaveValue("GB");
    expect(screen.getByLabelText("历史保留天数单位")).toHaveValue("天");
    fireEvent.change(screen.getByLabelText("网站标题"), { target: { value: "标题" } });
    fireEvent.change(screen.getByLabelText("最大任务大小"), { target: { value: "7" } });
    fireEvent.change(screen.getByLabelText("最小剩余磁盘空间"), { target: { value: "2" } });
    fireEvent.change(screen.getByLabelText("历史保留天数"), { target: { value: "45" } });
    expect(onFieldChange.mock.calls).toEqual([
      ["siteTitle", "标题"],
      ["maxTaskSizeGib", "7"],
      ["minFreeDiskGib", "2"],
      ["historyRetentionDays", "45"],
    ]);
  });

  it("renders the real machine card only when statistics are available", () => {
    render(
      <BasicSettingsSection
        form={initialSettingsFormState}
        machineStats={machineStats}
        onFieldChange={jest.fn()}
      />,
    );
    expect(screen.getByLabelText("机器磁盘空间")).toBeInTheDocument();
  });
});

describe("WebSocketSettingsSection", () => {
  it("edits range and number independently without fake health or hidden jitter controls", () => {
    const onFieldChange = jest.fn();
    render(
      <WebSocketSettingsSection form={initialSettingsFormState} onFieldChange={onFieldChange} />,
    );
    expect(screen.getByText("30 秒")).toBeInTheDocument();
    expect(screen.queryByText("实时通道正常")).not.toBeInTheDocument();
    expect(screen.getAllByRole("slider")).toHaveLength(1);
    expect(screen.getAllByRole("spinbutton")).toHaveLength(1);
    fireEvent.change(screen.getByLabelText("最大重连延迟"), { target: { value: "120" } });
    fireEvent.change(screen.getByLabelText("最大重连延迟数值"), { target: { value: "150" } });
    expect(onFieldChange.mock.calls).toEqual([
      ["wsReconnectMaxDelaySeconds", "120"],
      ["wsReconnectMaxDelaySeconds", "150"],
    ]);
  });
});

describe("Aria2SettingsSection", () => {
  const renderSection = (
    overrides: Partial<Parameters<typeof Aria2SettingsSection>[0]> = {},
  ) => {
    const onFieldChange = jest.fn();
    const onTestConnection = jest.fn();
    const view = render(
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
    return { view, onFieldChange, onTestConnection };
  };

  it("reports URL, secret and BT timeout edits and triggers the test", () => {
    const { onFieldChange, onTestConnection } = renderSection();
    expect(screen.getByText("版本 1.37.0")).toBeInTheDocument();
    expect(screen.queryByText(/WebSocket 正常/)).not.toBeInTheDocument();
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

  it("toggles Secret visibility with the Demo eye control", () => {
    renderSection();
    const input = screen.getByLabelText("aria2 RPC Secret");
    expect(input).toHaveAttribute("type", "password");
    fireEvent.click(screen.getByRole("button", { name: "显示密钥" }));
    expect(input).toHaveAttribute("type", "text");
    fireEvent.click(screen.getByRole("button", { name: "隐藏密钥" }));
    expect(input).toHaveAttribute("type", "password");
  });

  it("distinguishes an unavailable saved status from an HTTP 200 disconnected result", () => {
    const { view } = renderSection({ aria2Status: null });
    expect(screen.getByText("aria2 状态未知")).toBeInTheDocument();
    expect(screen.queryByText("aria2 未连接")).not.toBeInTheDocument();
    view.unmount();

    renderSection({ aria2Status: { connected: false, version: null, features: [] } });
    expect(screen.getByText("aria2 未连接")).toBeInTheDocument();
    expect(screen.getByText("无法连接 aria2，请检查 RPC 地址与 Secret。")).toBeInTheDocument();
  });

  it("distinguishes a transport failure from a completed connection test", () => {
    renderSection({ testError: "网络异常，请检查连接后重试" });
    expect(screen.getByRole("alert")).toHaveTextContent("连接测试请求失败");
    expect(screen.getByRole("alert")).toHaveTextContent("网络异常，请检查连接后重试");
    expect(screen.queryByText("测试结果：连接失败")).not.toBeInTheDocument();
  });

  it("renders successful, versionless and disconnected test results", () => {
    const { view } = renderSection({
      testResult: { connected: true, version: "1.38.0", features: [] },
    });
    expect(screen.getByText("测试结果：连接成功")).toBeInTheDocument();
    expect(screen.getByText("aria2 版本: 1.38.0")).toBeInTheDocument();

    view.rerender(
      <Aria2SettingsSection
        form={initialSettingsFormState}
        aria2Status={{ connected: true, version: null, features: [] }}
        testResult={{ connected: true, version: null, features: [] }}
        testingConnection={false}
        onFieldChange={jest.fn()}
        onTestConnection={jest.fn()}
      />,
    );
    expect(screen.getByText("连接探测成功，未返回版本。")).toBeInTheDocument();
    expect(screen.getByText("连接成功，未返回版本。")).toBeInTheDocument();

    view.rerender(
      <Aria2SettingsSection
        form={initialSettingsFormState}
        aria2Status={{ connected: true, version: "1.37.0", features: [] }}
        testResult={{ connected: false, version: null, features: [] }}
        testingConnection={false}
        onFieldChange={jest.fn()}
        onTestConnection={jest.fn()}
      />,
    );
    expect(screen.getByText("测试结果：连接失败")).toBeInTheDocument();
    expect(screen.getByText("请检查 RPC 地址与 Secret 是否正确。")).toBeInTheDocument();
  });

  it("shows the busy label while testing", () => {
    renderSection({ testingConnection: true });
    expect(screen.getByRole("button", { name: "测试中..." })).toBeDisabled();
  });
});
