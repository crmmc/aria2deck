import {
  configToSettingsFormState,
  settingsFormStateToPayload,
  initialSettingsFormState,
} from "@/app/(authenticated)/settings/settingsState";

describe("settingsState", () => {
  test("configToSettingsFormState maps missing optional values to defaults", () => {
    const state = configToSettingsFormState({
      max_task_size: 10 * 1024 * 1024 * 1024,
      min_free_disk: 5 * 1024 * 1024 * 1024,
    });

    expect(state.maxTaskSize).toBe("10.00");
    expect(state.minFreeDisk).toBe("5.00");
    expect(state.aria2RpcUrl).toBe("");
    expect(state.aria2RpcSecret).toBe("");
    expect(state.hiddenExtensions).toEqual([]);
    expect(state.packFormat).toBe("zip");
    expect(state.packCompressionLevel).toBe(5);
    expect(state.wsReconnectMaxDelay).toBe(60);
    expect(state.wsReconnectJitter).toBe(0.2);
    expect(state.wsReconnectFactor).toBe(2);
    expect(state.rateLimitAccountSecurity).toBe(5);
    expect(state.downloadTotalConnections).toBe(100);
  });

  test("settingsFormStateToPayload converts GB strings to byte fields", () => {
    const state = { ...initialSettingsFormState, maxTaskSize: "2", minFreeDisk: "1" };
    const result = settingsFormStateToPayload(state);

    expect(result.valid).toBe(true);
    if (result.valid) {
      expect(result.payload.max_task_size).toBe(2 * 1024 * 1024 * 1024);
      expect(result.payload.min_free_disk).toBe(1 * 1024 * 1024 * 1024);
    }
  });

  test("masked aria2 secret results in undefined in payload", () => {
    const state = { ...initialSettingsFormState, maxTaskSize: "1", minFreeDisk: "1", aria2RpcSecret: "****masked****" };
    const result = settingsFormStateToPayload(state);

    expect(result.valid).toBe(true);
    if (result.valid) {
      expect(result.payload.aria2_rpc_secret).toBeUndefined();
    }
  });

  test("invalid positive-number fields return validation error", () => {
    const state1 = { ...initialSettingsFormState, maxTaskSize: "0", minFreeDisk: "1" };
    const result1 = settingsFormStateToPayload(state1);
    expect(result1.valid).toBe(false);
    if (!result1.valid) {
      expect(result1.error).toBe("最大任务大小必须为正数");
    }

    const state2 = { ...initialSettingsFormState, maxTaskSize: "1", minFreeDisk: "-5" };
    const result2 = settingsFormStateToPayload(state2);
    expect(result2.valid).toBe(false);
    if (!result2.valid) {
      expect(result2.error).toBe("最小剩余磁盘空间必须为正数");
    }
  });

  test("settings form maps history_retention_days", () => {
    const state = configToSettingsFormState({
      max_task_size: 10 * 1024 * 1024 * 1024,
      min_free_disk: 5 * 1024 * 1024 * 1024,
      history_retention_days: 7,
    });
    expect(state.historyRetentionDays).toBe(7);

    const result = settingsFormStateToPayload({
      ...initialSettingsFormState,
      maxTaskSize: "1",
      minFreeDisk: "1",
      historyRetentionDays: 14,
    });
    expect(result.valid).toBe(true);
    if (result.valid) {
      expect(result.payload.history_retention_days).toBe(14);
    }
  });
});
