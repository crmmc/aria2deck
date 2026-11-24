import type { SettingsResponse } from "@/shared/api/types";

import {
  DOWNLOAD_CONNECTION_LIMIT_FIELDS,
  RATE_LIMIT_FIELDS,
  buildChangedUpdatePayload,
  buildUpdatePayload,
  formFromSettings,
  initialSettingsFormState,
  isSettingsFormDirty,
  settingsFormReducer,
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
    pack_format: "zip",
    pack_compression_level: 5,
    ws_reconnect_max_delay_seconds: 30,
    ws_reconnect_jitter_seconds: 3,
    ws_reconnect_factor: 2,
    history_retention_days: 90,
    tracker_fixed_list: ["udp://a/announce"],
    tracker_remote_urls: ["https://t.example/list.txt"],
    tracker_refresh_interval_minutes: 60,
    download_connection_limits: { total_connections: 100 },
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
  return { ...formFromSettings(settings()), ...overrides };
}

describe("formFromSettings（V1 换算行为保留）", () => {
  it("turns byte fields into GiB text and tracker lists into multiline text", () => {
    const result = formFromSettings(settings());
    expect(result.maxTaskSizeGib).toBe("10");
    expect(result.minFreeDiskGib).toBe("5");
    expect(result.trackerFixedListText).toBe("udp://a/announce");
    expect(result.trackerRemoteUrlsText).toBe("https://t.example/list.txt");
    expect(result.aria2RpcSecret).toBe("********");
  });

  it("rounds fractional GiB to two decimals like V1 bytesToGB", () => {
    const result = formFromSettings(settings({ max_task_size: 1.5 * GIB + 100 }));
    expect(result.maxTaskSizeGib).toBe("1.5");
  });

  it("fills the six real connection keys and leaves unset ones empty", () => {
    const result = formFromSettings(settings());
    expect(result.downloadConnectionLimits.total_connections).toBe("100");
    expect(result.downloadConnectionLimits.anonymous_total_connections).toBe("");
    expect(Object.keys(result.downloadConnectionLimits).sort()).toEqual(
      DOWNLOAD_CONNECTION_LIMIT_FIELDS.map((field) => field.key).sort(),
    );
  });

  it("fills every rate limit field from the wire response", () => {
    const result = formFromSettings(settings());
    expect(RATE_LIMIT_FIELDS).toHaveLength(11);
    for (const field of RATE_LIMIT_FIELDS) {
      expect(result.rateLimits[field.key]).toBe(String(settings()[field.key]));
    }
  });

  it("renders an empty secret when the backend reports none", () => {
    const result = formFromSettings(settings({ aria2_rpc_secret: null }));
    expect(result.aria2RpcSecret).toBe("");
  });

  it("keeps WS jitter/factor values out of the UI but available for round-trip", () => {
    const result = formFromSettings(settings());
    expect(result.wsReconnectJitterSeconds).toBe("3");
    expect(result.wsReconnectFactor).toBe("2");
  });
});

describe("settingsFormReducer（V1 reducer 结构保留）", () => {
  it("replaces the whole state on load", () => {
    const next = { ...initialSettingsFormState, siteTitle: "新标题" };
    expect(settingsFormReducer(initialSettingsFormState, { type: "replace", state: next })).toBe(next);
  });

  it("updates a single field without touching the rest", () => {
    const next = settingsFormReducer(initialSettingsFormState, {
      type: "field",
      field: "siteTitle",
      value: "标题",
    });
    expect(next.siteTitle).toBe("标题");
    expect(next.packFormat).toBe(initialSettingsFormState.packFormat);
    expect(initialSettingsFormState.siteTitle).toBe("");
  });
});

describe("buildUpdatePayload", () => {
  it("builds a full payload and omits the masked secret", () => {
    const result = buildUpdatePayload(form());
    expect(result.ok).toBe(true);
    if (!result.ok) return;
    expect(result.payload.site_title).toBe("Aria2Deck");
    expect(result.payload.max_task_size).toBe(10 * GIB);
    expect(result.payload.min_free_disk).toBe(5 * GIB);
    expect(result.payload.tracker_fixed_list).toEqual(["udp://a/announce"]);
    expect(result.payload.download_connection_limits).toEqual({ total_connections: 100 });
    expect(result.payload.account_security_rate_limit).toBe(10);
    expect(result.payload.aria2_rpc_secret).toBeUndefined();
    expect(result.payload).not.toHaveProperty("hidden_file_extensions");
    expect(Object.keys(result.payload)).not.toContain("updated_at_ms");
  });

  it("trims a typed secret and submits it", () => {
    const result = buildUpdatePayload(form({ aria2RpcSecret: "  new-secret  " }));
    expect(result.ok).toBe(true);
    if (result.ok) expect(result.payload.aria2_rpc_secret).toBe("new-secret");
  });

  it("never submits a secret the user left as the mask", () => {
    const result = buildUpdatePayload(form({ aria2RpcSecret: "***" }));
    expect(result.ok).toBe(true);
    if (result.ok) expect(result.payload.aria2_rpc_secret).toBeUndefined();
  });

  it("skips connection limits left blank instead of coercing them to zero", () => {
    const result = buildUpdatePayload(
      form({
        downloadConnectionLimits: {
          total_connections: "",
          anonymous_total_connections: "40",
        },
      }),
    );
    expect(result.ok).toBe(true);
    if (result.ok) expect(result.payload.download_connection_limits).toEqual({
      anonymous_total_connections: 40,
    });
  });

  it("accepts the 0 and 1000 boundaries for every connection key", () => {
    const zero = Object.fromEntries(
      DOWNLOAD_CONNECTION_LIMIT_FIELDS.map((field) => [field.key, "0"]),
    );
    const max = Object.fromEntries(
      DOWNLOAD_CONNECTION_LIMIT_FIELDS.map((field) => [field.key, "1000"]),
    );
    const lower = buildUpdatePayload(form({ downloadConnectionLimits: zero }));
    const upper = buildUpdatePayload(form({ downloadConnectionLimits: max }));
    expect(lower.ok).toBe(true);
    expect(upper.ok).toBe(true);
    if (upper.ok) {
      expect(Object.values(upper.payload.download_connection_limits ?? {})).toEqual([
        1000, 1000, 1000, 1000, 1000, 1000,
      ]);
    }
  });

  it("allows the tracker interval to stay 0 (manual only)", () => {
    const result = buildUpdatePayload(form({ trackerRefreshIntervalMinutes: "0" }));
    expect(result.ok).toBe(true);
    if (result.ok) expect(result.payload.tracker_refresh_interval_minutes).toBe(0);
  });

  const invalidCases: { name: string; patch: Partial<SettingsForm>; message: string }[] = [
    { name: "site title too long", patch: { siteTitle: "x".repeat(101) }, message: "网站标题" },
    { name: "empty max task size", patch: { maxTaskSizeGib: "" }, message: "最大任务大小" },
    { name: "non numeric max task size", patch: { maxTaskSizeGib: "abc" }, message: "最大任务大小" },
    { name: "negative min disk", patch: { minFreeDiskGib: "-1" }, message: "最小剩余磁盘空间" },
    { name: "bt timeout zero", patch: { aria2BtStopTimeoutSeconds: "0" }, message: "BT 停止超时" },
    { name: "pack level out of range", patch: { packCompressionLevel: "23" }, message: "压缩级别" },
    { name: "ws delay out of range", patch: { wsReconnectMaxDelaySeconds: "301" }, message: "重连最大延迟" },
    { name: "ws jitter out of range", patch: { wsReconnectJitterSeconds: "-1" }, message: "重连抖动" },
    { name: "ws factor out of range", patch: { wsReconnectFactor: "0" }, message: "重连退避倍数" },
    { name: "retention days out of range", patch: { historyRetentionDays: "0" }, message: "历史保留天数" },
    { name: "tracker interval out of range", patch: { trackerRefreshIntervalMinutes: "10081" }, message: "Tracker 刷新间隔" },
    { name: "connection limit out of range", patch: { downloadConnectionLimits: { total_connections: "1001" } }, message: "系统总下载连接上限" },
    { name: "connection limit non integer", patch: { downloadConnectionLimits: { total_connections: "1.5" } }, message: "系统总下载连接上限" },
    { name: "rate limit non-integer", patch: { rateLimits: { account_security_rate_limit: "1.5" } }, message: "账户安全限流" },
    { name: "rate limit zero", patch: { rateLimits: { rpc_rate_limit: "0" } }, message: "JSON-RPC 限流" },
    { name: "rate limit empty", patch: { rateLimits: { rpc_rate_limit: "" } }, message: "JSON-RPC 限流" },
  ];

  it.each(invalidCases)("rejects $name", ({ patch, message }) => {
    const base = form();
    const result = buildUpdatePayload({
      ...base,
      ...patch,
      rateLimits: { ...base.rateLimits, ...(patch.rateLimits ?? {}) },
      downloadConnectionLimits: {
        ...base.downloadConnectionLimits,
        ...(patch.downloadConnectionLimits ?? {}),
      },
    });
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error).toContain(message);
  });

  it.each(["1", "4"])(
    "rejects tracker auto refresh interval %s because the backend only allows 0 or >=5",
    (value) => {
      const result = buildUpdatePayload(form({ trackerRefreshIntervalMinutes: value }));
      expect(result.ok).toBe(false);
      if (!result.ok) expect(result.error).toContain("Tracker 刷新间隔");
    },
  );

  it("rejects a missing rate limit key as a non-integer", () => {
    const base = form();
    const rateLimits = { ...base.rateLimits };
    delete rateLimits.rpc_rate_limit;
    const result = buildUpdatePayload(form({ rateLimits }));
    expect(result.ok).toBe(false);
    if (!result.ok) expect(result.error).toContain("JSON-RPC 限流必须是整数");
  });

  it("keeps the WS jitter/factor round-trip values in the payload", () => {
    const result = buildUpdatePayload(form({ wsReconnectJitterSeconds: "3", wsReconnectFactor: "4" }));
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.payload.ws_reconnect_jitter_seconds).toBe(3);
      expect(result.payload.ws_reconnect_factor).toBe(4);
    }
  });
});

describe("settings form semantic diff", () => {
  it("treats freshly built arrays and maps with equal values as clean", () => {
    const saved = form();
    const current = form();

    expect(isSettingsFormDirty(current, saved)).toBe(false);
    expect(buildChangedUpdatePayload(current, saved)).toEqual({ ok: true, payload: {} });
  });

  it("treats numeric 1 and 1.0 as equivalent", () => {
    const saved = form({ historyRetentionDays: "1" });
    const current = form({ historyRetentionDays: "1.0" });
    expect(isSettingsFormDirty(current, saved)).toBe(false);
    expect(buildChangedUpdatePayload(current, saved)).toEqual({ ok: true, payload: {} });
  });

  const equivalentCases: { name: string; patch: Partial<SettingsForm> }[] = [
    {
      name: "tracker trailing blank lines",
      patch: {
        trackerFixedListText: "udp://a/announce\n\n",
        trackerRemoteUrlsText: "https://t.example/list.txt\n  \n",
      },
    },
    { name: "empty and masked secret", patch: { aria2RpcSecret: "" } },
  ];

  it.each(equivalentCases)("does not mark $name as dirty", ({ patch }) => {
    const current = form(patch);
    expect(isSettingsFormDirty(current, form())).toBe(false);
    expect(buildChangedUpdatePayload(current, form())).toEqual({ ok: true, payload: {} });
  });

  it("returns only the changed scalar and preserves hidden WS values", () => {
    const result = buildChangedUpdatePayload(form({ siteTitle: "新标题" }), form());

    expect(result).toEqual({ ok: true, payload: { site_title: "新标题" } });
    expect(isSettingsFormDirty(form({ siteTitle: "新标题" }), form())).toBe(true);
  });

  it("compares tracker arrays by value and returns a changed array", () => {
    const result = buildChangedUpdatePayload(
      form({ trackerFixedListText: "udp://b/announce\nudp://a/announce" }),
      form(),
    );

    expect(result).toEqual({
      ok: true,
      payload: { tracker_fixed_list: ["udp://b/announce", "udp://a/announce"] },
    });
  });

  it("returns the complete current connection map when any child changes", () => {
    const saved = form({
      downloadConnectionLimits: {
        total_connections: "100",
        anonymous_total_connections: "40",
      },
    });
    const current = form({
      downloadConnectionLimits: {
        total_connections: "101",
        anonymous_total_connections: "40",
      },
    });

    expect(buildChangedUpdatePayload(current, saved)).toEqual({
      ok: true,
      payload: {
        download_connection_limits: {
          total_connections: 101,
          anonymous_total_connections: 40,
        },
      },
    });
  });

  it("submits a hidden WS field only when that field itself changes", () => {
    expect(
      buildChangedUpdatePayload(form({ wsReconnectJitterSeconds: "4" }), form()),
    ).toEqual({ ok: true, payload: { ws_reconnect_jitter_seconds: 4 } });
  });

  it("includes a newly typed secret but not an unchanged mask", () => {
    expect(buildChangedUpdatePayload(form(), form({ aria2RpcSecret: "" }))).toEqual({
      ok: true,
      payload: {},
    });
    expect(
      buildChangedUpdatePayload(form({ aria2RpcSecret: "new-secret" }), form()),
    ).toEqual({ ok: true, payload: { aria2_rpc_secret: "new-secret" } });
  });

  it("always treats an invalid current form as dirty and preserves validation", () => {
    const current = form({ maxTaskSizeGib: "" });
    expect(isSettingsFormDirty(current, form({ maxTaskSizeGib: "" }))).toBe(true);
    expect(buildChangedUpdatePayload(current, form())).toEqual({
      ok: false,
      error: "最大任务大小必须是不小于 0 的数字",
    });
  });

  it("does not produce a diff from an invalid saved snapshot", () => {
    const saved = form({ historyRetentionDays: "0" });
    expect(isSettingsFormDirty(form(), saved)).toBe(true);
    expect(buildChangedUpdatePayload(form(), saved)).toEqual({
      ok: false,
      error: "历史保留天数必须在 1 到 3650 之间",
    });
  });
});
