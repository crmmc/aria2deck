import type { SettingsResponse } from "@/shared/api/types";

import {
  RATE_LIMIT_FIELDS,
  buildUpdatePayload,
  formFromSettings,
  type SettingsForm,
} from "@/features/settings/settingsForm";

const GIB = 1024 ** 3;

function settings(overrides: Partial<SettingsResponse> = {}): SettingsResponse {
  return {
    site_title: "Aria2Deck",
    max_task_size: 10 * GIB,
    min_free_disk: 5 * GIB,
    aria2_rpc_url: "http://127.0.0.1:6800/rpc",
    aria2_rpc_secret: "********",
    aria2_bt_stop_timeout_seconds: 60,
    hidden_file_extensions: [".ini", ".tmp"],
    pack_format: "zip",
    pack_compression_level: 5,
    ws_reconnect_max_delay_seconds: 30,
    ws_reconnect_jitter_seconds: 3,
    ws_reconnect_factor: 2,
    history_retention_days: 90,
    tracker_fixed_list: ["udp://a/announce"],
    tracker_remote_urls: ["https://t.example/list.txt"],
    tracker_refresh_interval_minutes: 60,
    download_connection_limits: { per_host: 8 },
    account_security_rate_limit: 10,
    authenticated_api_rate_limit: 600,
    public_api_rate_limit: 300,
    share_access_rate_limit: 30,
    create_task_rate_limit: 60,
    create_torrent_rate_limit: 30,
    create_pack_rate_limit: 30,
    aria2_test_rate_limit: 10,
    rpc_rate_limit: 600,
    file_search_rate_limit: 120,
    create_share_rate_limit: 60,
    updated_at_ms: 1700000000000,
    ...overrides,
  };
}

function form(overrides: Partial<SettingsForm> = {}): SettingsForm {
  return { ...formFromSettings(settings()), aria2RpcSecret: "", ...overrides };
}

describe("formFromSettings", () => {
  it("converts byte fields to GiB text and lists to multiline text", () => {
    const result = formFromSettings(settings());
    expect(result.maxTaskSizeGib).toBe("10");
    expect(result.minFreeDiskGib).toBe("5");
    expect(result.hiddenExtensionsText).toBe(".ini\n.tmp");
    expect(result.trackerRemoteUrlsText).toBe("https://t.example/list.txt");
    expect(result.downloadConnectionLimitsText).toBe("per_host=8");
    expect(result.rateLimits.rpc_rate_limit).toBe("600");
    expect(result.aria2RpcSecret).toBe(""); // 掩码不进入可提交字段。
  });

  it("rounds fractional GiB to two decimals", () => {
    const result = formFromSettings(settings({ max_task_size: 1.5 * GIB + 100 }));
    expect(result.maxTaskSizeGib).toBe("1.5");
  });

  it("renders empty connection limits as empty text", () => {
    const result = formFromSettings(settings({ download_connection_limits: {} }));
    expect(result.downloadConnectionLimitsText).toBe("");
  });
});

describe("buildUpdatePayload", () => {
  it("builds a full payload and omits untouched secret", () => {
    const result = buildUpdatePayload(form());
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.payload.site_title).toBe("Aria2Deck");
    expect(result.payload.max_task_size).toBe(10 * GIB);
    expect(result.payload.min_free_disk).toBe(5 * GIB);
    expect(result.payload.hidden_file_extensions).toEqual([".ini", ".tmp"]);
    expect(result.payload.tracker_fixed_list).toEqual(["udp://a/announce"]);
    expect(result.payload.download_connection_limits).toEqual({ per_host: 8 });
    expect(result.payload.account_security_rate_limit).toBe(10);
    expect(result.payload.aria2_rpc_secret).toBeUndefined();
    expect(Object.keys(result.payload)).not.toContain("updated_at_ms");
  });

  it("includes typed secret only", () => {
    const result = buildUpdatePayload(form({ aria2RpcSecret: "  new-secret  " }));
    expect(result.ok).toBe(true);
    if (result.ok) expect(result.payload.aria2_rpc_secret).toBe("new-secret");
  });

  const invalidCases: { name: string; patch: Partial<SettingsForm>; message: string }[] = [
    { name: "site title too long", patch: { siteTitle: "x".repeat(101) }, message: "网站标题" },
    { name: "bad max task size", patch: { maxTaskSizeGib: "abc" }, message: "最大任务大小" },
    { name: "negative min disk", patch: { minFreeDiskGib: "-1" }, message: "最小剩余磁盘空间" },
    { name: "bt timeout out of range", patch: { aria2BtStopTimeoutSeconds: "0" }, message: "BT 停止超时" },
    { name: "pack level out of range", patch: { packCompressionLevel: "23" }, message: "压缩级别" },
    { name: "ws delay out of range", patch: { wsReconnectMaxDelaySeconds: "301" }, message: "重连最大延迟" },
    { name: "ws jitter out of range", patch: { wsReconnectJitterSeconds: "-1" }, message: "重连抖动" },
    { name: "ws factor out of range", patch: { wsReconnectFactor: "0" }, message: "重连退避倍数" },
    { name: "retention days out of range", patch: { historyRetentionDays: "0" }, message: "历史保留天数" },
    { name: "tracker interval out of range", patch: { trackerRefreshIntervalMinutes: "10081" }, message: "Tracker 刷新间隔" },
    { name: "rate limit non-integer", patch: { rateLimits: { account_security_rate_limit: "1.5" } }, message: "账户安全限流" },
    { name: "rate limit out of range", patch: { rateLimits: { rpc_rate_limit: "0" } }, message: "JSON-RPC限流" },
  ];

  it.each(invalidCases)("rejects $name", ({ patch, message }) => {
    const base = form();
    const merged: Partial<SettingsForm> = {
      ...patch,
      rateLimits: { ...base.rateLimits, ...(patch.rateLimits ?? {}) },
    };
    const result = buildUpdatePayload(form(merged));
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error).toContain(message);
  });

  it("rejects connection limit lines without separator", () => {
    const result = buildUpdatePayload(form({ downloadConnectionLimitsText: "perhost" }));
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error).toContain("下载连接数格式无效");
  });

  it("rejects connection limit with invalid key or value", () => {
    const badKey = buildUpdatePayload(form({ downloadConnectionLimitsText: "Bad-Key=8" }));
    expect(badKey.ok).toBe(false);
    const badValue = buildUpdatePayload(form({ downloadConnectionLimitsText: "per_host=99" }));
    expect(badValue.ok).toBe(false);
  });

  it("accepts multiple valid connection limit lines and strips blanks", () => {
    const result = buildUpdatePayload(
      form({ downloadConnectionLimitsText: "per_host=8\n\nglobal=64\n " }),
    );
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.payload.download_connection_limits).toEqual({ per_host: 8, global: 64 });
    }
  });

  it("rejects a missing rate limit key as a non-integer", () => {
    const base = form();
    const rateLimits = { ...base.rateLimits };
    delete rateLimits.rpc_rate_limit;
    const result = buildUpdatePayload(form({ rateLimits }));
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error).toContain("JSON-RPC限流必须是整数");
  });

  it("covers all 11 rate limit fields", () => {
    expect(RATE_LIMIT_FIELDS).toHaveLength(11);
    const result = buildUpdatePayload(form());
    expect(result.ok).toBe(true);
    if (result.ok) {
      for (const field of RATE_LIMIT_FIELDS) {
        expect(Number.isInteger(result.payload[field.key])).toBe(true);
      }
    }
  });
});
