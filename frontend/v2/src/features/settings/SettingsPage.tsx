import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import { Navigate } from "react-router-dom";

import { useAuth } from "@/app/hooks";
import { useToast } from "@/shared/components/toast-context";
import { errorMessage } from "@/shared/api/client";
import type { MachineStatsResponse, TrackerStatusResponse } from "@/shared/api/types";

import {
  CREDENTIAL_INVALIDATION_CONFIRMATION,
  fetchAria2Version,
  fetchMachineStats,
  fetchSettings,
  fetchTrackerStatus,
  invalidateCredentials,
  refreshTrackers,
  testAria2Connection,
  updateSettings,
  type Aria2TestResult,
} from "./api";
import {
  buildUpdatePayload,
  formFromSettings,
  initialSettingsFormState,
  settingsFormReducer,
} from "./settingsForm";
import { AdvancedSettingsSection } from "./components/AdvancedSettingsSection";
import { Aria2SettingsSection } from "./components/Aria2SettingsSection";
import { BasicSettingsSection } from "./components/BasicSettingsSection";
import { CredentialSecuritySection } from "./components/CredentialSecuritySection";
import { DownloadConnectionSettingsSection } from "./components/DownloadConnectionSettingsSection";
import { MachineDiskCard } from "./components/MachineDiskCard";
import { PackSettingsSection } from "./components/PackSettingsSection";
import { RateLimitSettingsSection } from "./components/RateLimitSettingsSection";
import { TrackerSettingsSection } from "./components/TrackerSettingsSection";
import { WebSocketSettingsSection } from "./components/WebSocketSettingsSection";

/** 归档参考 系统设置页原文迁移（react-router 版）；屏蔽区块见兼容清单 C02-C06。 */
export function SettingsPage() {
  const { user } = useAuth();
  const { showToast, showConfirm } = useToast();
  const mountedRef = useRef(true);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [machineStats, setMachineStats] = useState<MachineStatsResponse | null>(null);
  const [form, dispatch] = useReducer(settingsFormReducer, initialSettingsFormState);
  const [aria2Status, setAria2Status] = useState<Aria2TestResult | null>(null);
  const [testResult, setTestResult] = useState<Aria2TestResult | null>(null);
  const [testingConnection, setTestingConnection] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [invalidatingCredentials, setInvalidatingCredentials] = useState(false);
  const [trackerStatus, setTrackerStatus] = useState<TrackerStatusResponse | null>(null);
  const [refreshingTrackers, setRefreshingTrackers] = useState(false);

  const isAdmin = user?.is_admin ?? false;

  /** 归档参考语义：读取失败进页面级错误；返回是否成功，供保存后决定是否提示成功。 */
  const loadConfig = useCallback(async (): Promise<boolean> => {
    try {
      const cfg = await fetchSettings();
      if (!mountedRef.current) return false;
      dispatch({ type: "replace", state: formFromSettings(cfg) });
      setTestResult(null);
    } catch {
      if (!mountedRef.current) return false;
      setError("加载配置失败");
      return false;
    } finally {
      if (mountedRef.current) setLoading(false);
    }
    // Tracker 状态、机器统计与 aria2 版本独立加载：任一失败不阻塞设置编辑
    // （归档参考 只把配置读取失败当作页面错误）。
    try {
      const status = await fetchTrackerStatus();
      if (mountedRef.current) setTrackerStatus(status);
    } catch {
      if (mountedRef.current) setTrackerStatus(null);
    }
    try {
      const stats = await fetchMachineStats();
      if (mountedRef.current) setMachineStats(stats);
    } catch {
      if (mountedRef.current) setMachineStats(null);
    }
    try {
      const version = await fetchAria2Version();
      if (mountedRef.current) setAria2Status(version);
    } catch {
      if (mountedRef.current) setAria2Status(null);
    }
    return true;
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  useEffect(() => {
    if (!isAdmin) return;
    void loadConfig();
  }, [isAdmin, loadConfig]);

  async function saveConfig(e: React.FormEvent) {
    e.preventDefault();
    setSaving(true);
    setSaveError(null);
    try {
      const validation = buildUpdatePayload(form);
      if (!validation.ok) {
        setSaveError(validation.error);
        return;
      }

      await updateSettings(validation.payload);
      if (!mountedRef.current) return;
      // 成功提示以重读结果为准（归档参考）：重读失败走页面级错误，不宣称已保存。
      const reloaded = await loadConfig();
      if (reloaded && mountedRef.current) showToast("配置已保存", "success");
    } catch (err) {
      if (!mountedRef.current) return;
      setSaveError(errorMessage(err, "保存配置失败"));
    } finally {
      if (mountedRef.current) setSaving(false);
    }
  }

  async function testConnection() {
    if (!form.aria2RpcUrl) {
      setTestResult({ connected: false, version: null, features: [] });
      return;
    }

    setTestingConnection(true);
    setTestResult(null);
    try {
      // 掩码/空 Secret 不提交，后端复用已存值（C06：V2 无清空语义）。
      const secret = form.aria2RpcSecret.startsWith("*") ? undefined : form.aria2RpcSecret;
      const result = await testAria2Connection({
        aria2_rpc_url: form.aria2RpcUrl,
        ...(secret === undefined || secret === "" ? {} : { aria2_rpc_secret: secret }),
      });
      if (mountedRef.current) setTestResult(result);
    } catch (err) {
      if (!mountedRef.current) return;
      // 请求本身失败（限流/网络）：保留 归档参考实现的「连接失败」卡片并把原因放在卡片内。
      setTestResult({ connected: false, version: null, features: [] });
      showToast(errorMessage(err, "连接测试失败"), "error");
    } finally {
      if (mountedRef.current) setTestingConnection(false);
    }
  }

  async function invalidateCredentialsHandler() {
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
      const result = await invalidateCredentials(CREDENTIAL_INVALIDATION_CONFIRMATION);
      if (!mountedRef.current) return;
      showToast(
        `已作废 ${result.api_token_count} 个 API Token、${result.rpc_secret_count} 个 RPC Secret`,
        "success",
      );
    } catch (err) {
      if (!mountedRef.current) return;
      showToast("作废失败：" + errorMessage(err, "未知错误"), "error");
    } finally {
      if (mountedRef.current) setInvalidatingCredentials(false);
    }
  }

  async function refreshTrackerList() {
    setRefreshingTrackers(true);
    try {
      const status = await refreshTrackers();
      if (!mountedRef.current) return;
      setTrackerStatus(status);
      if (status.refresh_status === "failed") {
        showToast("刷新 tracker 列表失败：全部远程源不可用", "error");
        return;
      }
      showToast("tracker 列表已刷新", "success");
    } catch (err) {
      if (!mountedRef.current) return;
      showToast("刷新 tracker 列表失败：" + errorMessage(err, "未知错误"), "error");
    } finally {
      if (mountedRef.current) setRefreshingTrackers(false);
    }
  }

  const setField = <K extends keyof typeof form>(field: K, value: (typeof form)[K]) => {
    dispatch({ type: "field", field, value });
  };

  const setRateLimit = (key: string, value: string) => {
    dispatch({ type: "field", field: "rateLimits", value: { ...form.rateLimits, [key]: value } });
  };

  const setConnectionLimit = (key: string, value: string) => {
    dispatch({
      type: "field",
      field: "downloadConnectionLimits",
      value: { ...form.downloadConnectionLimits, [key]: value },
    });
  };

  if (user !== null && !user.is_admin) {
    return <Navigate to="/tasks" replace />;
  }
  if (loading) return null;
  if (error !== null) {
    return (
      <div className="glass-frame full-height animate-in">
        <div className="card text-danger">{error}</div>
      </div>
    );
  }
  return (
    <div className="glass-frame full-height animate-in">
      <div className="page-header">
        <h1 className="page-title">系统设置</h1>
        <p className="muted">系统配置（仅管理员）</p>
      </div>

      {machineStats && <MachineDiskCard stats={machineStats} />}

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
          <PackSettingsSection form={form} onFieldChange={setField} />
          <WebSocketSettingsSection form={form} onFieldChange={setField} />
          <TrackerSettingsSection
            form={form}
            trackerStatus={trackerStatus}
            refreshing={refreshingTrackers}
            onFieldChange={setField}
            onRefresh={refreshTrackerList}
          />

          <AdvancedSettingsSection>
            <RateLimitSettingsSection rateLimits={form.rateLimits} onChange={setRateLimit} />
            <DownloadConnectionSettingsSection
              values={form.downloadConnectionLimits}
              onChange={setConnectionLimit}
            />
          </AdvancedSettingsSection>

          <div className="settings-form-actions flex items-center gap-4">
            <button className="button" type="submit" disabled={saving}>
              {saving ? "保存中..." : "保存配置"}
            </button>
            {saveError && <span className="save-error-inline">{saveError}</span>}
          </div>
        </form>
      </div>

      <div className="card mt-6">
        <CredentialSecuritySection
          invalidating={invalidatingCredentials}
          onInvalidate={invalidateCredentialsHandler}
        />
      </div>
    </div>
  );
}
