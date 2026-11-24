import {
  useCallback,
  useEffect,
  useReducer,
  useRef,
  useState,
  type FormEvent,
} from "react";
import { Navigate, useNavigate, useParams } from "react-router-dom";

import { useAuth } from "@/shared/auth/hooks";
import { useToast } from "@/shared/components/toast-context";
import { Icon, type IconName } from "@/shared/components/Icon";
import { PageHeader } from "@/shared/components/PageHeader";
import { settingsCommands } from "@/shared/lib/commands";
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
  buildChangedUpdatePayload,
  formFromSettings,
  initialSettingsFormState,
  isSettingsFormDirty,
  RATE_LIMIT_FIELDS,
  settingsFormReducer,
  type SettingsForm,
} from "./settingsForm";
import { AdvancedSettingsSection } from "./components/AdvancedSettingsSection";
import { Aria2SettingsSection } from "./components/Aria2SettingsSection";
import { BasicSettingsSection } from "./components/BasicSettingsSection";
import { CredentialSecuritySection } from "./components/CredentialSecuritySection";
import { DownloadConnectionSettingsSection } from "./components/DownloadConnectionSettingsSection";
import { PackSettingsSection } from "./components/PackSettingsSection";
import { RateLimitFields } from "./components/RateLimitSettingsSection";
import { TrackerSettingsSection } from "./components/TrackerSettingsSection";
import { WebSocketSettingsSection } from "./components/WebSocketSettingsSection";

const SETTINGS_SECTIONS = [
  { id: "basic", label: "基础设置", icon: "settings" },
  { id: "aria2", label: "aria2 后端", icon: "server" },
  { id: "pack", label: "打包设置", icon: "archive" },
  { id: "realtime", label: "实时连接", icon: "refresh" },
  { id: "tracker", label: "Tracker 列表", icon: "link" },
  { id: "security", label: "安全与限流", icon: "shield" },
] as const satisfies readonly { id: string; label: string; icon: IconName }[];

type SettingsSection = (typeof SETTINGS_SECTIONS)[number]["id"];

const PRIMARY_RATE_LIMIT_FIELDS = RATE_LIMIT_FIELDS.slice(0, 2);
const ADVANCED_RATE_LIMIT_FIELDS = RATE_LIMIT_FIELDS.slice(2);

function isSettingsSection(value: string | undefined): value is SettingsSection {
  return SETTINGS_SECTIONS.some((section) => section.id === value);
}

export function SettingsPage() {
  const { user } = useAuth();
  const { showToast, showConfirm } = useToast();
  const navigate = useNavigate();
  const { section: sectionParam } = useParams<{ section?: string }>();
  const activeSection = isSettingsSection(sectionParam) ? sectionParam : "basic";
  const mountedRef = useRef(true);
  const savingRef = useRef(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [machineStats, setMachineStats] = useState<MachineStatsResponse | null>(null);
  const [form, dispatch] = useReducer(settingsFormReducer, initialSettingsFormState);
  const [savedForm, setSavedForm] = useState<SettingsForm>(initialSettingsFormState);
  const [focusSection, setFocusSection] = useState<SettingsSection | null>(null);
  const [aria2Status, setAria2Status] = useState<Aria2TestResult | null>(null);
  const [testResult, setTestResult] = useState<Aria2TestResult | null>(null);
  const [testError, setTestError] = useState<string | null>(null);
  const [testingConnection, setTestingConnection] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [invalidatingCredentials, setInvalidatingCredentials] = useState(false);
  const [trackerStatus, setTrackerStatus] = useState<TrackerStatusResponse | null>(null);
  const [refreshingTrackers, setRefreshingTrackers] = useState(false);

  const isAdmin = user?.is_admin ?? false;
  const dirty = isSettingsFormDirty(form, savedForm);

  useEffect(() => {
    if (!isAdmin || loading || error !== null) return;
    return settingsCommands.subscribe((section) => {
      setFocusSection(section);
      if (section !== activeSection) navigate(`/settings/${section}`);
    });
  }, [activeSection, error, isAdmin, loading, navigate]);

  useEffect(() => {
    if (focusSection === null || focusSection !== activeSection || loading || error !== null) return;
    document.getElementById(`settings-${focusSection}`)?.focus({ preventScroll: true });
    setFocusSection(null);
  }, [activeSection, error, focusSection, loading]);

  /** 配置读取失败进入页面级错误；独立状态失败不阻塞设置编辑。 */
  const loadConfig = useCallback(async (): Promise<boolean> => {
    try {
      const cfg = await fetchSettings();
      if (!mountedRef.current) return false;
      const nextForm = formFromSettings(cfg);
      dispatch({ type: "replace", state: nextForm });
      setSavedForm(nextForm);
      setError(null);
      setTestResult(null);
      setTestError(null);
    } catch {
      if (!mountedRef.current) return false;
      setError("加载配置失败");
      return false;
    } finally {
      if (mountedRef.current) setLoading(false);
    }

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

  async function saveConfig(event: FormEvent) {
    event.preventDefault();
    if (savingRef.current) return;
    savingRef.current = true;
    setSaving(true);
    setSaveError(null);
    try {
      const changes = buildChangedUpdatePayload(form, savedForm);
      if (!changes.ok) {
        setSaveError(changes.error);
        return;
      }
      if (Object.keys(changes.payload).length === 0) return;

      await updateSettings(changes.payload);
      if (!mountedRef.current) return;
      const reloaded = await loadConfig();
      if (reloaded && mountedRef.current) showToast("配置已保存", "success");
    } catch (err) {
      if (!mountedRef.current) return;
      setSaveError(errorMessage(err, "保存配置失败"));
    } finally {
      savingRef.current = false;
      if (mountedRef.current) setSaving(false);
    }
  }

  function discardChanges() {
    dispatch({ type: "replace", state: savedForm });
    setSaveError(null);
  }

  async function testConnection() {
    setTestError(null);
    if (!form.aria2RpcUrl) {
      setTestResult({ connected: false, version: null, features: [] });
      return;
    }

    setTestingConnection(true);
    setTestResult(null);
    try {
      const secret = form.aria2RpcSecret.startsWith("*") ? undefined : form.aria2RpcSecret;
      const result = await testAria2Connection({
        aria2_rpc_url: form.aria2RpcUrl,
        ...(secret === undefined || secret === "" ? {} : { aria2_rpc_secret: secret }),
      });
      if (mountedRef.current) setTestResult(result);
    } catch (err) {
      if (!mountedRef.current) return;
      setTestResult(null);
      setTestError(errorMessage(err, "连接测试失败"));
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

  function renderActiveSection() {
    if (activeSection === "basic") {
      return <BasicSettingsSection form={form} machineStats={machineStats} onFieldChange={setField} />;
    }
    if (activeSection === "aria2") {
      return (
        <Aria2SettingsSection
          form={form}
          aria2Status={aria2Status}
          testResult={testResult}
          testError={testError}
          testingConnection={testingConnection}
          onFieldChange={setField}
          onTestConnection={testConnection}
        />
      );
    }
    if (activeSection === "pack") {
      return <PackSettingsSection form={form} onFieldChange={setField} />;
    }
    if (activeSection === "realtime") {
      return <WebSocketSettingsSection form={form} onFieldChange={setField} />;
    }
    if (activeSection === "tracker") {
      return (
        <TrackerSettingsSection
          form={form}
          trackerStatus={trackerStatus}
          refreshing={refreshingTrackers}
          onFieldChange={setField}
          onRefresh={refreshTrackerList}
        />
      );
    }
    return (
      <section id="settings-security" tabIndex={-1} className="admin-settings-section" aria-label="安全与限流">
        <div className="settings-section-head">
          <h2>安全与限流</h2>
          <p>限制接口请求频率，并管理所有外部访问凭证。</p>
        </div>
        <RateLimitFields
          fields={PRIMARY_RATE_LIMIT_FIELDS}
          rateLimits={form.rateLimits}
          onChange={setRateLimit}
        />
        <AdvancedSettingsSection>
          <RateLimitFields
            fields={ADVANCED_RATE_LIMIT_FIELDS}
            rateLimits={form.rateLimits}
            onChange={setRateLimit}
          />
          <DownloadConnectionSettingsSection
            values={form.downloadConnectionLimits}
            onChange={setConnectionLimit}
          />
        </AdvancedSettingsSection>
        <CredentialSecuritySection
          invalidating={invalidatingCredentials}
          onInvalidate={invalidateCredentialsHandler}
        />
      </section>
    );
  }

  if (user !== null && !user.is_admin) {
    return <Navigate to="/tasks" replace />;
  }
  if (sectionParam !== undefined && !isSettingsSection(sectionParam)) {
    return <Navigate to="/settings/basic" replace />;
  }
  if (loading) return null;
  if (error !== null) {
    return (
      <div className="glass-frame">
        <div className="card text-danger">{error}</div>
      </div>
    );
  }

  return (
    <div className="glass-frame">
      <PageHeader title="系统设置" description="系统配置（仅管理员）" />
      <div className="settings-layout">
        <nav className="panel settings-nav" aria-label="设置分区">
          {SETTINGS_SECTIONS.map((section) => (
            <button
              key={section.id}
              type="button"
              className={activeSection === section.id ? "active" : undefined}
              aria-current={activeSection === section.id ? "page" : "false"}
              onClick={() => navigate(`/settings/${section.id}`)}
            >
              <Icon name={section.icon} className="icon-sm" />
              {section.label}
            </button>
          ))}
        </nav>
        <div className="settings-content">
          <form aria-label="系统设置表单" aria-busy={saving} onSubmit={saveConfig}>
            <section className="panel admin-settings-panel">
              <div className="panel-body" id="settings-panel">
                <fieldset className="settings-form-fields" disabled={saving}>
                  {renderActiveSection()}
                </fieldset>
              </div>
            </section>
            <div className={`save-bar${dirty ? "" : " clean"}`} id="save-bar">
              <div className={`save-status${dirty ? " dirty" : ""}`} id="save-status" aria-live="polite">
                <Icon name={dirty ? "alert" : "check"} className="icon-sm" />
                <span>{dirty ? "有未保存的配置更改" : "所有配置已保存"}</span>
              </div>
              {saveError && <span className="save-error-inline" role="alert">{saveError}</span>}
              <button
                className="button secondary btn-sm"
                type="button"
                disabled={!dirty || saving}
                onClick={discardChanges}
              >
                放弃更改
              </button>
              <button className="button btn-sm" type="submit" disabled={!dirty || saving}>
                <Icon name="check" className="icon-sm" />
                {saving ? "保存中..." : "保存配置"}
              </button>
            </div>
          </form>
        </div>
      </div>
    </div>
  );
}
