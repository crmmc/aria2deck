import type { SettingsForm, SettingsFormSetter } from "../settingsForm";
import { NumberSettingField } from "./NumberSettingField";

type WebSocketSettingsSectionProps = {
  form: Pick<SettingsForm, "wsReconnectMaxDelaySeconds">;
  onFieldChange: SettingsFormSetter;
};

export function WebSocketSettingsSection({ form, onFieldChange }: WebSocketSettingsSectionProps) {
  return (
    <section id="settings-realtime" tabIndex={-1} className="admin-settings-section" aria-label="实时连接设置">
      <div className="settings-section-head">
        <h2>实时连接</h2>
        <p>配置断线后的重连等待策略。</p>
      </div>
      <NumberSettingField
        id="settings-ws-max-delay"
        label="最大重连延迟"
        description="指数退避的最大等待时间，范围 1 至 300 秒。"
        value={form.wsReconnectMaxDelaySeconds}
        min={1}
        max={300}
        rangeMax={300}
        unit="秒"
        onChange={(value) => onFieldChange("wsReconnectMaxDelaySeconds", value)}
      />
    </section>
  );
}
