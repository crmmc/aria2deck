import type { MachineStatsResponse } from "@/shared/api/types";

import type { SettingsForm, SettingsFormSetter } from "../settingsForm";
import { MachineDiskCard } from "./MachineDiskCard";
import { NumberSettingField } from "./NumberSettingField";

type BasicSettingsSectionProps = {
  form: Pick<
    SettingsForm,
    "siteTitle" | "maxTaskSizeGib" | "minFreeDiskGib" | "historyRetentionDays"
  >;
  machineStats?: MachineStatsResponse | null;
  onFieldChange: SettingsFormSetter;
};

export function BasicSettingsSection({
  form,
  machineStats,
  onFieldChange,
}: BasicSettingsSectionProps) {
  return (
    <section id="settings-basic" tabIndex={-1} className="admin-settings-section" aria-label="基础设置">
      <div className="settings-section-head">
        <h2>基础设置</h2>
        <p>站点名称、任务限制和历史记录保留策略。</p>
      </div>

      {machineStats && <MachineDiskCard stats={machineStats} />}

      <div className="form-grid">
        <div className="field span-2">
          <label className="field-label" htmlFor="settings-site-title">网站标题</label>
          <p className="field-hint">显示在导航和浏览器标题中。留空使用默认值。</p>
          <input
            id="settings-site-title"
            className="input"
            type="text"
            value={form.siteTitle}
            onChange={(event) => onFieldChange("siteTitle", event.target.value)}
            placeholder="Aria2 控制器"
            maxLength={100}
            aria-label="网站标题"
          />
          <p className="field-error" data-error-for="siteTitle" />
        </div>
        <NumberSettingField
          id="settings-max-task-size"
          label="最大任务大小"
          description="超过此大小的任务将被拒绝。"
          value={form.maxTaskSizeGib}
          min={0}
          step="any"
          unit="GB"
          onChange={(value) => onFieldChange("maxTaskSizeGib", value)}
        />
        <NumberSettingField
          id="settings-min-disk-space"
          label="最小剩余磁盘空间"
          description="剩余空间低于此值时停止接受新任务。"
          value={form.minFreeDiskGib}
          min={0}
          step="any"
          unit="GB"
          onChange={(value) => onFieldChange("minFreeDiskGib", value)}
        />
        <NumberSettingField
          id="settings-history-retention-days"
          label="历史保留天数"
          description="超过保留期的失败/取消历史将软过期，不可再重试。"
          value={form.historyRetentionDays}
          min={1}
          max={3650}
          unit="天"
          onChange={(value) => onFieldChange("historyRetentionDays", value)}
        />
      </div>
    </section>
  );
}
