"use client";

import { useEffect, useId, useReducer, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { useToast } from "@/components/Toast";
import { api } from "@/lib/api";
import { MachineStats } from "@/types";
import { formatBytes } from "@/lib/utils";
import {
  settingsFormReducer,
  initialSettingsFormState,
  configToSettingsFormState,
  settingsFormStateToPayload,
  type SettingsFormState,
} from "./settingsState";
import { BasicSettingsSection } from "./_components/BasicSettingsSection";
import { Aria2SettingsSection } from "./_components/Aria2SettingsSection";
import { HiddenExtensionsSection } from "./_components/HiddenExtensionsSection";
import { PackSettingsSection } from "./_components/PackSettingsSection";
import { WebSocketSettingsSection } from "./_components/WebSocketSettingsSection";
import { RateLimitSettingsSection } from "./_components/RateLimitSettingsSection";
import { DownloadConnectionSettingsSection } from "./_components/DownloadConnectionSettingsSection";
import { CredentialSecuritySection } from "./_components/CredentialSecuritySection";

function toPercent(value: number, total: number): number {
  if (!total || total <= 0) return 0;
  return (value / total) * 100;
}

function AdvancedSettingsSection({ children }: { children: React.ReactNode }) {
  const [expanded, setExpanded] = useState(false);
  const panelId = useId();

  return (
    <section className={`settings-advanced-section mt-7${expanded ? " settings-advanced-section-open" : ""}`}>
      <button
        type="button"
        className="settings-advanced-toggle"
        aria-expanded={expanded}
        aria-controls={panelId}
        onClick={() => setExpanded((prev) => !prev)}
      >
        <span className="settings-advanced-copy">
          <span className="settings-advanced-title">系统高级设置</span>
          <span className="settings-advanced-description">
            收纳接口频率限制和下载并发限制，按需展开调整。
          </span>
        </span>
        <span className="settings-advanced-meta">
          <span className="settings-advanced-state">{expanded ? "收起" : "展开"}</span>
          <span
            aria-hidden="true"
            className={`settings-advanced-chevron${expanded ? " settings-advanced-chevron-open" : ""}`}
          >
            ⌄
          </span>
        </span>
      </button>

      {expanded && (
        <div id={panelId} className="settings-advanced-panel">
          {children}
        </div>
      )}
    </section>
  );
}

export default function SettingsPage() {
  const { push } = useRouter();
  const { showToast, showConfirm } = useToast();
  const mountedRef = useRef(true);
  const [loading, setLoading] = useState(true);
  const [isAdmin, setIsAdmin] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [machineStats, setMachineStats] = useState<MachineStats | null>(null);

  const [form, dispatch] = useReducer(settingsFormReducer, initialSettingsFormState);

  const [aria2Status, setAria2Status] = useState<{
    connected: boolean;
    version?: string;
    error?: string;
  } | null>(null);
  const [testResult, setTestResult] = useState<{
    connected: boolean;
    version?: string;
    error?: string;
  } | null>(null);
  const [testingConnection, setTestingConnection] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [invalidatingCredentials, setInvalidatingCredentials] = useState(false);

  useEffect(() => {
    mountedRef.current = true;
    (async () => {
      try {
        const user = await api.me();
        if (mountedRef.current) {
          if (!user.is_admin) {
            push("/tasks");
            return;
          }
          setIsAdmin(true);
          await loadConfig();
        }
      } catch {
        if (!mountedRef.current) return;
        setError("加载配置失败");
      } finally {
        if (mountedRef.current) setLoading(false);
      }
    })();
    return () => {
      mountedRef.current = false;
    };
  }, [push]);

  async function loadConfig(throwOnError: boolean = false) {
    try {
      const [cfg, stats, aria2Ver] = await Promise.all([
        api.getConfig(),
        api.getMachineStats(),
        api.getAria2Version(),
      ]);
      if (mountedRef.current) {
        dispatch({ type: "replace", state: configToSettingsFormState(cfg as Record<string, unknown>) });
        setMachineStats(stats);
        setAria2Status(aria2Ver);
        setTestResult(null);
      }
    } catch {
      if (!mountedRef.current) return;
      setError("加载配置失败");
      if (throwOnError) {
        throw new Error("加载配置失败");
      }
    }
  }

  async function saveConfig(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    setSaveError(null);
    try {
      const validation = settingsFormStateToPayload(form);
      if (!validation.valid) {
        setSaveError(validation.error);
        return;
      }
      if (!mountedRef.current) return;

      await api.updateConfig(validation.payload);
      await loadConfig(true);
      if (mountedRef.current) showToast("配置已保存", "success");
    } catch (err) {
      if (!mountedRef.current) return;
      const message = (err as Error).message || "保存配置失败";
      setSaveError(message);
    } finally {
      if (mountedRef.current) setSaving(false);
    }
  }

  async function testConnection() {
    if (!form.aria2RpcUrl) {
      setTestResult({ connected: false, error: "请输入 aria2 RPC URL" });
      return;
    }

    setTestingConnection(true);
    setTestResult(null);
    try {
      const result = await api.testAria2Connection(
        form.aria2RpcUrl,
        form.aria2RpcSecret.startsWith("*") ? undefined : form.aria2RpcSecret,
      );
      if (mountedRef.current) setTestResult(result);
    } catch (err) {
      if (!mountedRef.current) return;
      setTestResult({ connected: false, error: (err as Error).message });
    } finally {
      if (mountedRef.current) setTestingConnection(false);
    }
  }

  async function invalidateCredentials() {
    const confirmed = await showConfirm({
      title: "作废全部凭证",
      message:
        "将删除所有用户的 API Token，并清空全部用户 RPC Secret。登录密码与当前会话不受影响，但自动化客户端需重新签发凭证。此操作不可恢复。",
      confirmText: "确认作废",
      danger: true,
    });
    if (!confirmed || !mountedRef.current) return;

    setInvalidatingCredentials(true);
    try {
      const result = await api.invalidateAllCredentials();
      if (!mountedRef.current) return;
      showToast(
        `已作废 ${result.api_token_count} 个 API Token、${result.rpc_secret_count} 个 RPC Secret`,
        "success",
      );
    } catch (err) {
      if (!mountedRef.current) return;
      showToast("作废失败：" + ((err as Error).message || "未知错误"), "error");
    } finally {
      if (mountedRef.current) setInvalidatingCredentials(false);
    }
  }

  const setField = <K extends keyof SettingsFormState>(field: K, value: SettingsFormState[K]) => {
    dispatch({ type: "field", field, value });
  };

  if (loading) return null;
  if (error) return (
    <div className="glass-frame full-height animate-in">
      <div className="card text-danger">{error}</div>
    </div>
  );
  if (!isAdmin) return null;

  return (
    <div className="glass-frame full-height animate-in">
      <div className="page-header">
        <h1 className="page-title">系统设置</h1>
        <p className="muted">系统配置（仅管理员）</p>
      </div>

      {machineStats && (
        <div className="card mb-6">
          <h2 className="mb-4">机器磁盘空间</h2>
          <div className="flex items-baseline gap-2">
            <span className="stats-value">{formatBytes(machineStats.disk_free)}</span>
            <span className="stats-unit">/ {formatBytes(machineStats.disk_total)}</span>
            <span className="muted">可用</span>
          </div>
          <div className="progress-container mt-2 max-w-600" style={{ overflow: "hidden" }}>
            <div style={{ display: "flex", width: "100%", height: "100%" }}>
              <div
                style={{
                  width: `${toPercent(machineStats.download_used, machineStats.disk_total)}%`,
                  background: "#3b82f6",
                }}
              />
              <div
                style={{
                  width: `${toPercent(machineStats.system_used, machineStats.disk_total)}%`,
                  background: "#f59e0b",
                }}
              />
            </div>
          </div>
          <div className="mt-3 text-sm" style={{ display: "grid", gap: 6 }}>
            <div className="flex items-center gap-2">
              <span style={{ width: 10, height: 10, borderRadius: 9999, background: "#3b82f6" }} />
              <span>下载占用：{formatBytes(machineStats.download_used)}（{toPercent(machineStats.download_used, machineStats.disk_total).toFixed(1)}%）</span>
            </div>
            <div className="flex items-center gap-2">
              <span style={{ width: 10, height: 10, borderRadius: 9999, background: "#f59e0b" }} />
              <span>系统占用：{formatBytes(machineStats.system_used)}（{toPercent(machineStats.system_used, machineStats.disk_total).toFixed(1)}%）</span>
            </div>
            <div className="muted">总占用：{formatBytes(machineStats.disk_used)}（{toPercent(machineStats.disk_used, machineStats.disk_total).toFixed(1)}%）</div>
          </div>
        </div>
      )}

      <div className="card">
        <form onSubmit={saveConfig} className="max-w-600">
          <BasicSettingsSection form={form} onFieldChange={setField} />
          <Aria2SettingsSection
            form={form}
            aria2Status={aria2Status}
            testResult={testResult}
            testingConnection={testingConnection}
            onFieldChange={setField}
            onTestConnection={testConnection}
          />
          <HiddenExtensionsSection form={form} dispatch={dispatch} />
          <PackSettingsSection form={form} onFieldChange={setField} />
          <WebSocketSettingsSection form={form} onFieldChange={setField} />

          <AdvancedSettingsSection>
            <RateLimitSettingsSection form={form} onFieldChange={setField} />
            <DownloadConnectionSettingsSection form={form} onFieldChange={setField} />
          </AdvancedSettingsSection>

          <div className="settings-form-actions flex items-center gap-4">
            <button className="button" type="submit" disabled={saving}>
              {saving ? "保存中..." : "保存配置"}
            </button>
            {saveError && (
              <span className="save-error-inline">{saveError}</span>
            )}
          </div>
        </form>
      </div>

      <div className="card mt-6">
        <CredentialSecuritySection
          invalidating={invalidatingCredentials}
          onInvalidate={invalidateCredentials}
        />
      </div>
    </div>
  );
}
