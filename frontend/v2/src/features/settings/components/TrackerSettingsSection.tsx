import type { TrackerStatusResponse } from "@/shared/api/types";

import type { SettingsForm, SettingsFormSetter } from "../settingsForm";

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

/** 归档参考实现原文：毫秒时间戳转本地字符串，缺省显示「从未刷新」。 */
function formatRefreshTime(ms: number | null | undefined): string {
  if (!ms) return "从未刷新";
  return new Date(ms).toLocaleString();
}

/** V2 的 refresh_status 取值为 never/success/partial/failed/refreshing（归档参考 为 never/ok/partial/failed）。 */
function statusTextOf(status: TrackerStatusResponse | null, failedCount: number): string {
  switch (status?.refresh_status ?? "never") {
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

export function TrackerSettingsSection({
  form,
  trackerStatus,
  refreshing,
  onFieldChange,
  onRefresh,
}: TrackerSettingsSectionProps) {
  const failedList = trackerStatus?.failed_urls ?? [];
  const failedCount = failedList.length;

  return (
    <>
      <h2 className="section-title">Tracker 列表</h2>

      <div className="mb-6">
        <label className="form-label-lg" htmlFor="settings-tracker-fixed-list">固定 tracker 列表</label>
        <p className="muted text-sm mb-2">
          每行一条，支持 http/https/udp，单条最长 2048；非法条目保存时会被后端拒绝。
        </p>
        <textarea
          id="settings-tracker-fixed-list"
          className="input"
          rows={4}
          value={form.trackerFixedListText}
          onChange={(e) => onFieldChange("trackerFixedListText", e.target.value)}
          placeholder={"http://tracker.example.com/announce\nudp://tracker.example.com:6969"}
          aria-label="固定 tracker 列表"
        />
      </div>

      <div className="mb-6">
        <label className="form-label-lg" htmlFor="settings-tracker-remote-urls">远程 tracker 列表 URL</label>
        <p className="muted text-sm mb-2">每行一个远程列表地址，定期或手动拉取后与固定列表合并去重。</p>
        <textarea
          id="settings-tracker-remote-urls"
          className="input"
          rows={3}
          value={form.trackerRemoteUrlsText}
          onChange={(e) => onFieldChange("trackerRemoteUrlsText", e.target.value)}
          placeholder={"https://example.com/trackers.txt"}
          aria-label="远程 tracker 列表 URL"
        />
      </div>

      <div className="mb-6">
        <label className="form-label-lg" htmlFor="settings-tracker-refresh-interval">tracker 刷新间隔（分钟）</label>
        <p className="muted text-sm mb-2">0 表示仅手动刷新；配置自动刷新时不得少于 5 分钟。</p>
        <input
          id="settings-tracker-refresh-interval"
          className="input"
          type="number"
          step="1"
          min="0"
          max="10080"
          value={form.trackerRefreshIntervalMinutes}
          onChange={(e) => onFieldChange("trackerRefreshIntervalMinutes", e.target.value)}
          aria-label="tracker 刷新间隔（分钟）"
        />
      </div>

      <div className="mb-7 p-4 rounded-lg alert-info">
        <div className="flex-between">
          <div>
            <div>当前条目数：{trackerStatus?.entry_count ?? 0}</div>
            <div className="muted text-sm">
              上次刷新结果：{statusTextOf(trackerStatus, failedCount)}，刷新时间：
              {formatRefreshTime(trackerStatus?.last_refresh_at_ms)}
            </div>
            {failedList.length > 0 && (
              <div className="muted text-sm" style={{ wordBreak: "break-all" }}>
                失败源：{failedList.join("、")}
              </div>
            )}
          </div>
          <button
            type="button"
            className="button"
            disabled={refreshing}
            onClick={onRefresh}
          >
            {refreshing ? "刷新中..." : "立即刷新"}
          </button>
        </div>
      </div>
    </>
  );
}
