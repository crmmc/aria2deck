import { useCallback, useEffect, useState, type ReactNode } from "react";
import { Navigate } from "react-router-dom";

import { useAuth, useToast } from "@/app/hooks";
import { errorMessage } from "@/shared/api/client";
import { ErrorState, LoadingState, Modal } from "@/shared/components/ui";
import type {
  MachineStatsResponse,
  SettingsResponse,
  TrackerStatusResponse,
} from "@/shared/api/types";
import { formatBytes, formatDateTime } from "@/shared/lib/format";
import {
  CREDENTIAL_INVALIDATION_CONFIRMATION,
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
  RATE_LIMIT_FIELDS,
  buildUpdatePayload,
  formFromSettings,
  type SettingsForm,
} from "./settingsForm";

const REFRESH_STATUS_LABEL: Record<string, string> = {
  success: "刷新成功",
  partial: "部分源失败",
  failed: "刷新失败",
};

function Section({ title, hint, children }: { title: string; hint?: string; children: ReactNode }) {
  return (
    <section className="section-card">
      <h2 className="section-title">{title}</h2>
      {hint !== undefined && <p className="hint-text">{hint}</p>}
      {children}
    </section>
  );
}

function toPercent(value: number, total: number): number {
  if (!Number.isFinite(total) || total <= 0) return 0;
  return Math.min(100, (value / total) * 100);
}

/** E1 机器磁盘卡：下载占用/系统占用分段 + 总量（对齐旧版展示）。 */
function MachineDiskCard({ stats }: { stats: MachineStatsResponse }) {
  const downloadPercent = toPercent(stats.download_used_bytes, stats.total_bytes);
  const systemPercent = toPercent(stats.system_used_bytes, stats.total_bytes);
  return (
    <Section title="机器磁盘空间">
      <p className="space-card-values">
        {formatBytes(stats.free_bytes)}{" "}
        <span className="hint-text">/ {formatBytes(stats.total_bytes)} 可用</span>
      </p>
      <div
        className="progress-track machine-disk"
        role="progressbar"
        aria-label="机器磁盘占用"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={Math.round(downloadPercent + systemPercent)}
      >
        <div className="progress-fill progress-download" style={{ width: `${downloadPercent}%` }} />
        <div className="progress-fill progress-system" style={{ width: `${systemPercent}%` }} />
      </div>
      <div className="machine-disk-legend">
        <span>
          <i className="dot dot-download" aria-hidden="true" />
          下载占用：{formatBytes(stats.download_used_bytes)}（{downloadPercent.toFixed(1)}%）
        </span>
        <span>
          <i className="dot dot-system" aria-hidden="true" />
          系统占用：{formatBytes(stats.system_used_bytes)}（{systemPercent.toFixed(1)}%）
        </span>
        <span className="hint-text">
          总占用：{formatBytes(stats.used_bytes)}（{toPercent(stats.used_bytes, stats.total_bytes).toFixed(1)}%）
        </span>
      </div>
    </Section>
  );
}

export function SettingsPage() {
  const { user: me } = useAuth();
  const { push } = useToast();
  const [settings, setSettings] = useState<SettingsResponse | null>(null);
  const [form, setForm] = useState<SettingsForm | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [tracker, setTracker] = useState<TrackerStatusResponse | null>(null);
  const [refreshingTrackers, setRefreshingTrackers] = useState(false);
  const [invalidateOpen, setInvalidateOpen] = useState(false);
  const [invalidateInput, setInvalidateInput] = useState("");
  const [invalidateError, setInvalidateError] = useState<string | null>(null);
  const [invalidating, setInvalidating] = useState(false);
  const [aria2Test, setAria2Test] = useState<Aria2TestResult | null>(null);
  const [aria2Testing, setAria2Testing] = useState(false);
  // E1 机器磁盘：独立加载，失败不阻塞设置编辑。
  const [machine, setMachine] = useState<MachineStatsResponse | null>(null);

  const loadAll = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const config = await fetchSettings();
      setSettings(config);
      setForm(formFromSettings(config));
    } catch (caught) {
      setError(errorMessage(caught, "设置加载失败"));
      return;
    } finally {
      setLoading(false);
    }
    // Tracker 状态独立加载：失败不阻塞设置编辑（分区显示未加载提示）。
    try {
      setTracker(await fetchTrackerStatus());
    } catch {
      setTracker(null);
    }
    try {
      setMachine(await fetchMachineStats());
    } catch {
      setMachine(null);
    }
  }, []);

  useEffect(() => {
    if (me === null || !me.is_admin) return; // 非管理员跳离，不发请求。
    void loadAll();
  }, [loadAll, me]);

  if (me !== null && !me.is_admin) {
    return <Navigate to="/tasks" replace />;
  }
  if (loading) {
    return (
      <section className="page" aria-label="系统设置">
        <header className="page-header">
          <h1>设置</h1>
        </header>
        <LoadingState />
      </section>
    );
  }
  if (error !== null || form === null || settings === null) {
    return (
      <section className="page" aria-label="系统设置">
        <header className="page-header">
          <h1>设置</h1>
        </header>
        <ErrorState message={error ?? "设置加载失败"} onRetry={() => void loadAll()} />
      </section>
    );
  }

  const setField = <K extends keyof SettingsForm>(key: K, value: SettingsForm[K]) => {
    setForm((current) => (current === null ? current : { ...current, [key]: value }));
  };

  const setRateLimit = (key: string, value: string) => {
    setForm((current) =>
      current === null ? current : { ...current, rateLimits: { ...current.rateLimits, [key]: value } },
    );
  };

  const handleSave = async () => {
    if (form === null || saving) return;
    const result = buildUpdatePayload(form);
    if (!result.ok) {
      setFormError(result.error);
      return;
    }
    setFormError(null);
    setSaving(true);
    try {
      await updateSettings(result.payload);
    } catch (caught) {
      setFormError(errorMessage(caught, "保存失败，请检查输入"));
      return;
    } finally {
      setSaving(false);
    }
    // 保存成功后重读，成功提示以重读结果为准（CFG-02）。
    try {
      const config = await fetchSettings();
      setSettings(config);
      setForm(formFromSettings(config));
      push("success", "设置已保存");
    } catch {
      push("error", "已保存，但重新读取失败，请刷新页面查看最新配置");
    }
  };

  const handleAria2Test = async () => {
    if (aria2Testing) return;
    setAria2Testing(true);
    try {
      setAria2Test(await testAria2Connection());
    } catch (caught) {
      // 测试请求本身失败（限流/网络）时给中文提示，不保留旧结果。
      setAria2Test(null);
      push("error", errorMessage(caught, "连接测试失败"));
    } finally {
      setAria2Testing(false);
    }
  };

  const handleRefreshTrackers = async () => {
    if (refreshingTrackers) return;
    setRefreshingTrackers(true);
    try {
      const status = await refreshTrackers();
      setTracker(status);
      if (status.refresh_status === "success") {
        push("success", `Tracker 已刷新，共 ${status.entry_count} 条`);
      } else if (status.refresh_status === "partial") {
        push("error", `Tracker 部分源刷新失败（${status.failed_urls.length} 个）`);
      } else {
        push("error", "Tracker 刷新失败，已保留旧列表");
      }
    } catch (caught) {
      push("error", errorMessage(caught, "Tracker 刷新失败"));
    } finally {
      setRefreshingTrackers(false);
    }
  };

  const handleInvalidate = async () => {
    if (invalidating) return;
    setInvalidateError(null);
    setInvalidating(true);
    try {
      const response = await invalidateCredentials(invalidateInput);
      push(
        "success",
        `已作废 ${response.api_token_count} 个 API Token、${response.rpc_secret_count} 个 RPC Secret`,
      );
      setInvalidateOpen(false);
      setInvalidateInput("");
    } catch (caught) {
      setInvalidateError(errorMessage(caught, "凭证作废失败"));
    } finally {
      setInvalidating(false);
    }
  };

  return (
    <section className="page" aria-label="系统设置">
      <header className="page-header">
        <h1>设置</h1>
        <span className="header-meta">最近更新：{formatDateTime(settings.updated_at_ms)}</span>
        <div className="header-actions">
          <button
            type="button"
            className="primary-button"
            disabled={saving}
            onClick={() => void handleSave()}
          >
            {saving ? "保存中…" : "保存全部"}
          </button>
        </div>
      </header>

      {formError !== null && (
        <div className="form-error" role="alert">
          {formError}
        </div>
      )}

      {machine !== null && <MachineDiskCard stats={machine} />}

      <Section title="基本" hint="站点标题与容量准入。">
        <div className="form-grid">
          <label className="form-field">
            <span>网站标题</span>
            <input
              value={form.siteTitle}
              onChange={(event) => setField("siteTitle", event.target.value)}
              maxLength={100}
            />
          </label>
          <label className="form-field">
            <span>最大任务大小（GiB）</span>
            <input
              value={form.maxTaskSizeGib}
              onChange={(event) => setField("maxTaskSizeGib", event.target.value)}
              inputMode="decimal"
            />
          </label>
          <label className="form-field">
            <span>最小剩余磁盘空间（GiB）</span>
            <input
              value={form.minFreeDiskGib}
              onChange={(event) => setField("minFreeDiskGib", event.target.value)}
              inputMode="decimal"
            />
          </label>
        </div>
      </Section>

      <Section title="下载" hint="隐藏后缀与每主机连接数限制。">
        <div className="form-grid">
          <label className="form-field form-field-wide">
            <span>隐藏文件后缀（每行一个，如 .ini）</span>
            <textarea
              rows={3}
              value={form.hiddenExtensionsText}
              onChange={(event) => setField("hiddenExtensionsText", event.target.value)}
            />
          </label>
          <label className="form-field form-field-wide">
            <span>下载连接数限制（每行一条：主机=连接数，1-64）</span>
            <textarea
              rows={3}
              value={form.downloadConnectionLimitsText}
              onChange={(event) => setField("downloadConnectionLimitsText", event.target.value)}
            />
          </label>
        </div>
      </Section>

      <Section title="aria2" hint="RPC 连接参数；Secret 掩码显示，留空保持不变。">
        <div className="form-grid">
          <label className="form-field">
            <span>RPC 地址</span>
            <input
              value={form.aria2RpcUrl}
              onChange={(event) => setField("aria2RpcUrl", event.target.value)}
              placeholder="http://127.0.0.1:6800/rpc"
            />
          </label>
          <label className="form-field">
            <span>RPC Secret</span>
            <input
              type="password"
              value={form.aria2RpcSecret}
              onChange={(event) => setField("aria2RpcSecret", event.target.value)}
              placeholder={
                settings.aria2_rpc_secret !== null ? "已配置（掩码显示，留空保持不变）" : "未配置"
              }
              autoComplete="off"
            />
          </label>
          <label className="form-field">
            <span>BT 停止超时（秒，1-86400）</span>
            <input
              value={form.aria2BtStopTimeoutSeconds}
              onChange={(event) => setField("aria2BtStopTimeoutSeconds", event.target.value)}
              inputMode="numeric"
            />
          </label>
        </div>
        <div className="aria2-test" aria-label="aria2 连接测试">
          <button
            type="button"
            className="secondary-button"
            disabled={aria2Testing}
            onClick={() => void handleAria2Test()}
          >
            {aria2Testing ? "测试中…" : "测试连接"}
          </button>
          {aria2Test !== null &&
            (aria2Test.connected ? (
              <p className="aria2-test-result">
                <span className="badge tone-ok">连接成功</span>
                版本 {aria2Test.version ?? "未知"}
                {aria2Test.features.length > 0 && (
                  <span className="feature-tags">
                    {aria2Test.features.map((feature) => (
                      <span key={feature} className="badge tone-info">
                        {feature}
                      </span>
                    ))}
                  </span>
                )}
              </p>
            ) : (
              <p className="aria2-test-result" role="alert">
                <span className="badge tone-error">连接失败</span>
                请检查 RPC 地址与 Secret 是否正确。
              </p>
            ))}
        </div>
      </Section>

      <Section title="Tracker" hint="fixed 在前、remote 按源顺序合并去重。">
        <div className="form-grid">
          <label className="form-field form-field-wide">
            <span>固定 Tracker 列表（每行一个）</span>
            <textarea
              rows={3}
              value={form.trackerFixedListText}
              onChange={(event) => setField("trackerFixedListText", event.target.value)}
            />
          </label>
          <label className="form-field form-field-wide">
            <span>远程 Tracker 源（每行一个 http(s) 地址）</span>
            <textarea
              rows={3}
              value={form.trackerRemoteUrlsText}
              onChange={(event) => setField("trackerRemoteUrlsText", event.target.value)}
            />
          </label>
          <label className="form-field">
            <span>刷新间隔（分钟，0 = 手动）</span>
            <input
              value={form.trackerRefreshIntervalMinutes}
              onChange={(event) => setField("trackerRefreshIntervalMinutes", event.target.value)}
              inputMode="numeric"
            />
          </label>
        </div>
        <div className="tracker-status" aria-label="Tracker 状态">
          {tracker === null ? (
            <p className="hint-text">Tracker 状态未加载。</p>
          ) : (
            <>
              <p>
                <span
                  className={`badge tone-${
                    tracker.refresh_status === "success"
                      ? "ok"
                      : tracker.refresh_status === "partial"
                        ? "warn"
                        : "error"
                  }`}
                >
                  {REFRESH_STATUS_LABEL[tracker.refresh_status] ?? tracker.refresh_status}
                </span>
                共 {tracker.entry_count} 条 · 上次刷新：
                {formatDateTime(tracker.last_refresh_at_ms)}
              </p>
              {tracker.failed_urls.length > 0 && (
                <div className="failed-urls" role="alert">
                  <p>以下远程源刷新失败：</p>
                  <ul>
                    {tracker.failed_urls.map((url) => (
                      <li key={url}>{url}</li>
                    ))}
                  </ul>
                </div>
              )}
            </>
          )}
          <button
            type="button"
            className="secondary-button"
            disabled={refreshingTrackers}
            onClick={() => void handleRefreshTrackers()}
          >
            {refreshingTrackers ? "刷新中…" : "刷新 Tracker"}
          </button>
        </div>
      </Section>

      <Section title="WebSocket" hint="断线重连参数（前端实时通道）。">
        <div className="form-grid">
          <label className="form-field">
            <span>最大重连延迟（秒，1-300）</span>
            <input
              value={form.wsReconnectMaxDelaySeconds}
              onChange={(event) => setField("wsReconnectMaxDelaySeconds", event.target.value)}
              inputMode="numeric"
            />
          </label>
          <label className="form-field">
            <span>重连抖动（秒，0-30）</span>
            <input
              value={form.wsReconnectJitterSeconds}
              onChange={(event) => setField("wsReconnectJitterSeconds", event.target.value)}
              inputMode="numeric"
            />
          </label>
          <label className="form-field">
            <span>退避倍数（1-10）</span>
            <input
              value={form.wsReconnectFactor}
              onChange={(event) => setField("wsReconnectFactor", event.target.value)}
              inputMode="numeric"
            />
          </label>
        </div>
      </Section>

      <Section title="限流" hint="各通道每分钟请求上限（1-1000000）。">
        <div className="form-grid">
          {RATE_LIMIT_FIELDS.map((field) => (
            <label className="form-field" key={field.key}>
              <span>{field.label}</span>
              <input
                value={form.rateLimits[field.key] ?? ""}
                onChange={(event) => setRateLimit(field.key, event.target.value)}
                inputMode="numeric"
                aria-label={`${field.label}限流`}
              />
            </label>
          ))}
        </div>
      </Section>

      <details className="section-card advanced-section">
        <summary>高级（打包 / 历史保留 / 凭证作废）</summary>
        <div className="advanced-body">
          <div className="form-grid">
            <label className="form-field">
              <span>打包格式</span>
              <select
                value={form.packFormat}
                onChange={(event) =>
                  setField("packFormat", event.target.value as SettingsForm["packFormat"])
                }
                aria-label="打包格式"
              >
                <option value="zip">zip</option>
                <option value="tar.zst">tar.zst</option>
              </select>
            </label>
            <label className="form-field">
              <span>压缩级别（0-22）</span>
              <input
                value={form.packCompressionLevel}
                onChange={(event) => setField("packCompressionLevel", event.target.value)}
                inputMode="numeric"
              />
            </label>
            <label className="form-field">
              <span>历史保留天数（1-3650）</span>
              <input
                value={form.historyRetentionDays}
                onChange={(event) => setField("historyRetentionDays", event.target.value)}
                inputMode="numeric"
              />
            </label>
          </div>
          <div className="danger-zone">
            <h3 className="section-title">凭证作废</h3>
            <p className="hint-text">
              作废全部用户的 API Token 与 RPC Secret（不影响密码和当前登录会话），需要输入确认串。
            </p>
            <button
              type="button"
              className="danger-button"
              onClick={() => {
                setInvalidateInput("");
                setInvalidateError(null);
                setInvalidateOpen(true);
              }}
            >
              作废全部凭证…
            </button>
          </div>
        </div>
      </details>

      {invalidateOpen && (
        <Modal
          title="作废全部凭证"
          onClose={() => setInvalidateOpen(false)}
          footer={
            <>
              <button
                type="button"
                className="secondary-button"
                onClick={() => setInvalidateOpen(false)}
              >
                取消
              </button>
              <button
                type="button"
                className="danger-button"
                disabled={
                  invalidating || invalidateInput !== CREDENTIAL_INVALIDATION_CONFIRMATION
                }
                onClick={() => void handleInvalidate()}
              >
                {invalidating ? "执行中…" : "确认作废"}
              </button>
            </>
          }
        >
          {invalidateError !== null && (
            <div className="form-error" role="alert">
              {invalidateError}
            </div>
          )}
          <p>
            该操作将作废全部用户的 API Token 与 RPC Secret，使用旧凭证的外部工具会立即失效。
          </p>
          <label className="form-field">
            <span>输入确认串 {CREDENTIAL_INVALIDATION_CONFIRMATION}</span>
            <input
              value={invalidateInput}
              onChange={(event) => setInvalidateInput(event.target.value)}
              autoComplete="off"
              aria-label="凭证作废确认串"
            />
          </label>
        </Modal>
      )}
    </section>
  );
}
