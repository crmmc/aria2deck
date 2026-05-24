import type { SettingsFormState } from "../settingsState";

type BasicSettingsSectionProps = {
  form: Pick<SettingsFormState, "siteTitle" | "maxTaskSize" | "minFreeDisk">;
  onFieldChange: <K extends keyof SettingsFormState>(field: K, value: SettingsFormState[K]) => void;
};

export function BasicSettingsSection({ form, onFieldChange }: BasicSettingsSectionProps) {
  return (
    <>
      <h2 className="section-title">系统配置</h2>

      <div className="mb-6">
        <label className="form-label-lg" htmlFor="settings-site-title">网站标题</label>
        <p className="muted text-sm mb-2">自定义网站标题，显示在侧边栏和页面标题中。留空使用默认值。</p>
        <input
          id="settings-site-title"
          className="input"
          type="text"
          value={form.siteTitle}
          onChange={(e) => onFieldChange("siteTitle", e.target.value)}
          placeholder="Aria2 控制器"
          maxLength={50}
          aria-label="网站标题"
        />
      </div>

      <div className="mb-6">
        <label className="form-label-lg" htmlFor="settings-max-task-size">最大任务大小 (GB)</label>
        <p className="muted text-sm mb-2">超过此大小的任务将被拒绝。</p>
        <input
          id="settings-max-task-size"
          className="input"
          type="number"
          step="any"
          min="0.1"
          value={form.maxTaskSize}
          onChange={(e) => onFieldChange("maxTaskSize", e.target.value)}
          aria-label="最大任务大小 (GB)"
        />
      </div>

      <div className="mb-7">
        <label className="form-label-lg" htmlFor="settings-min-disk-space">最小剩余磁盘空间 (GB)</label>
        <p className="muted text-sm mb-2">如果剩余空间低于此值，将停止接受新任务。</p>
        <input
          id="settings-min-disk-space"
          className="input"
          type="number"
          step="any"
          min="0.1"
          value={form.minFreeDisk}
          onChange={(e) => onFieldChange("minFreeDisk", e.target.value)}
          aria-label="最小剩余磁盘空间 (GB)"
        />
      </div>
    </>
  );
}
