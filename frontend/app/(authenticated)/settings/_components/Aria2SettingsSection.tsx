import type { SettingsFormState } from "../settingsState";

type Aria2SettingsSectionProps = {
  form: Pick<SettingsFormState, "aria2RpcUrl" | "aria2RpcSecret">;
  aria2Status: { connected: boolean; version?: string; error?: string } | null;
  testResult: { connected: boolean; version?: string; error?: string } | null;
  testingConnection: boolean;
  onFieldChange: <K extends keyof SettingsFormState>(field: K, value: SettingsFormState[K]) => void;
  onTestConnection: () => void;
};

export function Aria2SettingsSection({
  form,
  aria2Status,
  testResult,
  testingConnection,
  onFieldChange,
  onTestConnection,
}: Aria2SettingsSectionProps) {
  return (
    <>
      <h2 className="section-title">aria2 后端配置</h2>

      <div className={`mb-6 p-4 rounded-lg ${aria2Status?.connected ? "alert-success" : "alert-danger"}`}>
        <div className="flex-between mb-2">
          <div className="flex items-center gap-2">
            <div className={`status-dot ${aria2Status?.connected ? "status-dot-success" : "status-dot-danger"}`} />
            <span className="font-semibold">{aria2Status?.connected ? "已连接" : "未连接"}</span>
          </div>
          {aria2Status?.connected && aria2Status.version && (
            <span className="muted text-sm font-mono">aria2 {aria2Status.version}</span>
          )}
        </div>
        {aria2Status?.error && (
          <p className="muted text-sm text-danger">错误：{aria2Status.error}</p>
        )}
      </div>

      <div className="mb-6">
        <label className="form-label-lg" htmlFor="settings-rpc-url">aria2 RPC URL</label>
        <p className="muted text-sm mb-2">aria2 JSON-RPC 接口地址，例如：http://localhost:6800/jsonrpc</p>
        <input
          id="settings-rpc-url"
          className="input"
          type="text"
          value={form.aria2RpcUrl}
          onChange={(e) => onFieldChange("aria2RpcUrl", e.target.value)}
          placeholder="http://localhost:6800/jsonrpc"
          aria-label="aria2 RPC URL"
        />
      </div>

      <div className="mb-4">
        <label className="form-label-lg" htmlFor="settings-rpc-secret">aria2 RPC Secret</label>
        <p className="muted text-sm mb-2">aria2 RPC 认证密钥（可选）。留空表示不使用认证。</p>
        <input
          id="settings-rpc-secret"
          className="input"
          type="password"
          value={form.aria2RpcSecret}
          onChange={(e) => onFieldChange("aria2RpcSecret", e.target.value)}
          placeholder="留空表示无密钥"
          aria-label="aria2 RPC Secret"
        />
      </div>

      <div className="mb-7">
        <button
          type="button"
          className="button secondary mb-4"
          onClick={onTestConnection}
          disabled={testingConnection}
        >
          {testingConnection ? "测试中..." : "测试连接"}
        </button>

        {testResult && (
          <div className={`p-3 rounded-lg ${testResult.connected ? "alert-success" : "alert-danger"}`}>
            <div className="flex items-center gap-2">
              <div className={`status-dot-sm ${testResult.connected ? "status-dot-success" : "status-dot-danger"}`} />
              <span className="text-base font-semibold">
                测试结果：{testResult.connected ? "连接成功" : "连接失败"}
              </span>
            </div>
            {testResult.connected && testResult.version && (
              <p className="muted text-sm ml-4 mt-1">aria2 版本: {testResult.version}</p>
            )}
            {testResult.error && (
              <p className="text-sm ml-4 mt-1 text-danger">{testResult.error}</p>
            )}
          </div>
        )}
      </div>
    </>
  );
}
