import type { SettingsForm, SettingsFormSetter } from "../settingsForm";

export type Aria2ConnectionState = {
  connected: boolean;
  version: string | null;
  features: string[];
};

type Aria2SettingsSectionProps = {
  form: Pick<SettingsForm, "aria2RpcUrl" | "aria2RpcSecret" | "aria2BtStopTimeoutSeconds">;
  aria2Status: Aria2ConnectionState | null;
  testResult: Aria2ConnectionState | null;
  testingConnection: boolean;
  onFieldChange: SettingsFormSetter;
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
        {aria2Status !== null && !aria2Status.connected && (
          <p className="muted text-sm text-danger">无法连接 aria2，请检查 RPC 地址与 Secret。</p>
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

      <div className="mb-6">
        <label className="form-label-lg" htmlFor="settings-rpc-secret">aria2 RPC Secret</label>
        <p className="muted text-sm mb-2">aria2 RPC 认证密钥（可选）。留空表示不修改已保存的密钥。</p>
        <input
          id="settings-rpc-secret"
          className="input"
          type="password"
          value={form.aria2RpcSecret}
          onChange={(e) => onFieldChange("aria2RpcSecret", e.target.value)}
          placeholder="留空表示不修改"
          autoComplete="off"
          aria-label="aria2 RPC Secret"
        />
      </div>

      <div className="mb-6">
        <label className="form-label-lg" htmlFor="settings-bt-stop-timeout">BT 停止超时（秒）</label>
        <p className="muted text-sm mb-2">停种时等待 aria2 完成上报的超时时间（1-86400 秒）。</p>
        <input
          id="settings-bt-stop-timeout"
          className="input"
          type="number"
          step="1"
          min="1"
          max="86400"
          value={form.aria2BtStopTimeoutSeconds}
          onChange={(e) => onFieldChange("aria2BtStopTimeoutSeconds", e.target.value)}
          aria-label="BT 停止超时"
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
            {!testResult.connected && (
              <p className="text-sm ml-4 mt-1 text-danger">
                请检查 RPC 地址与 Secret 是否正确。
              </p>
            )}
          </div>
        )}
      </div>
    </>
  );
}
