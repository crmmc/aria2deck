import { Icon } from "@/shared/components/Icon";
import type { TrackerStatusResponse } from "@/shared/api/types";
import { formatAdminDateTime } from "@/shared/lib/format";

import type { SettingsForm, SettingsFormSetter } from "../settingsForm";
import { NumberSettingField } from "./NumberSettingField";

type TrackerSettingsSectionProps = {
  form: Pick<
    SettingsForm,
    "trackerFixedListText" | "trackerRemoteUrlsText" | "trackerRefreshIntervalMinutes"
  >;
  trackerStatus: TrackerStatusResponse | null;
  refreshing: boolean;
  onFieldChange: SettingsFormSetter;
  onRefresh: () => void;
};

function formatRefreshTime(ms: number | null | undefined): string {
  if (!ms) return "从未刷新";
  return formatAdminDateTime(ms);
}

function statusTextOf(status: TrackerStatusResponse, failedCount: number): string {
  switch (status.refresh_status) {
    case "success":
      return "成功";
    case "partial":
      return `部分失败（失败源 ${failedCount} 个）`;
    case "failed":
      return "全部失败";
    case "refreshing":
      return "刷新中";
    default:
      return "从未刷新";
  }
}

function statusClassOf(status: TrackerStatusResponse | null): string {
  switch (status?.refresh_status) {
    case "success":
      return "success";
    case "partial":
      return "warning";
    case "failed":
      return "error";
    case "refreshing":
      return "active";
    default:
      return "unknown";
  }
}

export function TrackerSettingsSection({
  form,
  trackerStatus,
  refreshing,
  onFieldChange,
  onRefresh,
}: TrackerSettingsSectionProps) {
  const failedList = trackerStatus?.failed_urls ?? [];
  const failedCount = failedList.length;
  const statusClass = statusClassOf(trackerStatus);

  return (
    <section id="settings-tracker" tabIndex={-1} className="admin-settings-section" aria-label="Tracker 列表">
      <div className="settings-section-head">
        <h2>Tracker 列表</h2>
        <p>固定列表与远程列表合并去重后交给下载后端。</p>
      </div>
      <div className="form-grid">
        <div className="field span-2">
          <label className="field-label" htmlFor="settings-tracker-fixed-list">固定 Tracker 列表</label>
          <p className="field-hint">每行一条，支持 HTTP、HTTPS 和 UDP，单条最长 2048。</p>
          <textarea
            id="settings-tracker-fixed-list"
            className="textarea mono"
            rows={4}
            value={form.trackerFixedListText}
            onChange={(event) => onFieldChange("trackerFixedListText", event.target.value)}
            placeholder={"http://tracker.example.com/announce\nudp://tracker.example.com:6969"}
            aria-label="固定 tracker 列表"
          />
          <p className="field-error" data-error-for="trackerFixedListText" />
        </div>
        <div className="field span-2">
          <label className="field-label" htmlFor="settings-tracker-remote-urls">远程列表 URL</label>
          <p className="field-hint">每行一个远程列表地址，定期或手动拉取后与固定列表合并去重。</p>
          <textarea
            id="settings-tracker-remote-urls"
            className="textarea mono"
            rows={3}
            value={form.trackerRemoteUrlsText}
            onChange={(event) => onFieldChange("trackerRemoteUrlsText", event.target.value)}
            placeholder="https://example.com/trackers.txt"
            aria-label="远程 tracker 列表 URL"
          />
          <p className="field-error" data-error-for="trackerRemoteUrlsText" />
        </div>
        <NumberSettingField
          id="settings-tracker-refresh-interval"
          label="自动刷新间隔"
          description="0 表示仅手动刷新，自动刷新不得少于 5 分钟。"
          value={form.trackerRefreshIntervalMinutes}
          min={0}
          max={10080}
          unit="分钟"
          onChange={(value) => onFieldChange("trackerRefreshIntervalMinutes", value)}
        />
      </div>
      <div className={`inline-status ${statusClass} admin-settings-tracker-status`}>
        <span className={`status-dot ${statusClass === "success" ? "status-dot-success" : statusClass === "warning" ? "status-dot-warning" : statusClass === "error" ? "status-dot-danger" : statusClass === "active" ? "status-dot-primary" : "status-dot-gray"}`} />
        <div className="inline-status-copy">
          {trackerStatus === null ? (
            <>
              <strong>Tracker 状态不可用</strong>
              <span>未能读取当前条目和刷新结果。</span>
            </>
          ) : (
            <>
              <strong>当前共 {trackerStatus.entry_count} 条 Tracker</strong>
              <span>
                上次刷新{statusTextOf(trackerStatus, failedCount)} · {formatRefreshTime(trackerStatus.last_refresh_at_ms)}
              </span>
              {failedList.length > 0 && <span className="tracker-failed-sources">失败源：{failedList.join("、")}</span>}
            </>
          )}
        </div>
        <button
          type="button"
          className="button secondary btn-sm"
          disabled={refreshing}
          onClick={onRefresh}
        >
          <Icon name="refresh" className="icon-sm" />
          {refreshing ? "正在刷新" : "立即刷新"}
        </button>
      </div>
    </section>
  );
}
