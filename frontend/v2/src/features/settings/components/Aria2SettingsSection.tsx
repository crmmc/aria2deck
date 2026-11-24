import { useState } from "react";

import { Icon } from "@/shared/components/Icon";

import type { SettingsForm, SettingsFormSetter } from "../settingsForm";
import { NumberSettingField } from "./NumberSettingField";

export type Aria2ConnectionState = {
  connected: boolean;
  version: string | null;
  features: string[];
};

type Aria2SettingsSectionProps = {
  form: Pick<SettingsForm, "aria2RpcUrl" | "aria2RpcSecret" | "aria2BtStopTimeoutSeconds">;
  aria2Status: Aria2ConnectionState | null;
  testResult: Aria2ConnectionState | null;
  testError?: string | null;
  testingConnection: boolean;
  onFieldChange: SettingsFormSetter;
  onTestConnection: () => void;
};

export function Aria2SettingsSection({
  form,
  aria2Status,
  testResult,
  testError = null,
  testingConnection,
  onFieldChange,
  onTestConnection,
}: Aria2SettingsSectionProps) {
  const [secretVisible, setSecretVisible] = useState(false);
  const statusClass = aria2Status === null
    ? "unknown"
    : aria2Status.connected ? "success" : "error";

  return (
    <section id="settings-aria2" tabIndex={-1} className="admin-settings-section" aria-label="aria2 后端">
      <div className="settings-section-head">
        <h2>aria2 后端</h2>
        <p>管理 JSON-RPC 连接和 BitTorrent 停止超时。</p>
      </div>

      <div className={`inline-status ${statusClass}`}>
        <span className={`status-dot ${aria2Status?.connected ? "status-dot-success" : aria2Status === null ? "status-dot-gray" : "status-dot-danger"}`} />
        <div className="inline-status-copy">
          <strong>
            {aria2Status === null ? "aria2 状态未知" : aria2Status.connected ? "aria2 已连接" : "aria2 未连接"}
          </strong>
          <span>
            {aria2Status === null
              ? "未能读取已保存配置的连接状态。"
              : aria2Status.connected
                ? aria2Status.version ? `版本 ${aria2Status.version}` : "连接探测成功，未返回版本。"
                : "无法连接 aria2，请检查 RPC 地址与 Secret。"}
          </span>
        </div>
        <button
          type="button"
          className="button secondary btn-sm"
          onClick={onTestConnection}
          disabled={testingConnection}
        >
          <Icon name="refresh" className="icon-sm" />
          {testingConnection ? "测试中..." : "测试连接"}
        </button>
      </div>

      <div className="form-grid admin-settings-form">
        <div className="field span-2">
          <label className="field-label" htmlFor="settings-rpc-url">aria2 RPC URL</label>
          <p className="field-hint">aria2 JSON-RPC 接口地址。</p>
          <input
            id="settings-rpc-url"
            className="input mono"
            type="url"
            value={form.aria2RpcUrl}
            onChange={(event) => onFieldChange("aria2RpcUrl", event.target.value)}
            placeholder="http://localhost:6800/jsonrpc"
            aria-label="aria2 RPC URL"
          />
          <p className="field-error" data-error-for="aria2RpcUrl" />
        </div>
        <div className="field">
          <label className="field-label" htmlFor="settings-rpc-secret">RPC Secret</label>
          <p className="field-hint">留空表示不修改已保存的密钥。</p>
          <div className="input-wrap">
            <Icon name="key" />
            <input
              id="settings-rpc-secret"
              className="input"
              type={secretVisible ? "text" : "password"}
              value={form.aria2RpcSecret}
              onChange={(event) => onFieldChange("aria2RpcSecret", event.target.value)}
              placeholder="留空表示不修改"
              autoComplete="off"
              aria-label="aria2 RPC Secret"
            />
            <button
              className="icon-btn search-clear"
              type="button"
              aria-label={secretVisible ? "隐藏密钥" : "显示密钥"}
              onClick={() => setSecretVisible((visible) => !visible)}
            >
              <Icon name={secretVisible ? "eye-off" : "eye"} className="icon-sm" />
            </button>
          </div>
          <p className="field-error" data-error-for="aria2RpcSecret" />
        </div>
        <NumberSettingField
          id="settings-bt-stop-timeout"
          label="BT 停止超时"
          description="停种时等待 aria2 完成上报的超时时间。"
          value={form.aria2BtStopTimeoutSeconds}
          min={1}
          max={86400}
          unit="秒"
          onChange={(value) => onFieldChange("aria2BtStopTimeoutSeconds", value)}
        />
      </div>

      {testError !== null ? (
        <div className="inline-status error admin-settings-test-result" role="alert">
          <span className="status-dot status-dot-danger" />
          <div className="inline-status-copy">
            <strong>连接测试请求失败</strong>
            <span>{testError}</span>
          </div>
        </div>
      ) : testResult && (
        <div className={`inline-status ${testResult.connected ? "success" : "error"} admin-settings-test-result`}>
          <span className={`status-dot ${testResult.connected ? "status-dot-success" : "status-dot-danger"}`} />
          <div className="inline-status-copy">
            <strong>测试结果：{testResult.connected ? "连接成功" : "连接失败"}</strong>
            <span>
              {testResult.connected
                ? testResult.version ? `aria2 版本: ${testResult.version}` : "连接成功，未返回版本。"
                : "请检查 RPC 地址与 Secret 是否正确。"}
            </span>
          </div>
        </div>
      )}
    </section>
  );
}
