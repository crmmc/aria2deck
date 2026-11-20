import type { SettingsForm, SettingsFormSetter } from "../settingsForm";

type WebSocketSettingsSectionProps = {
  form: Pick<SettingsForm, "wsReconnectMaxDelaySeconds">;
  onFieldChange: SettingsFormSetter;
};

/**
 * 归档参考 区块保留了「最大重连延迟」这一项：V2 语义等价（秒整数），单位与范围一致。
 * 抖动（归档参考 是 0-100% 比例，V2 是 0-30 秒）与指数因子（归档参考 允许 1.1-10 小数，V2 是 1-10 整数）
 * 无法无损往返，按兼容清单 C05 不做旧控件；两者原值随保存原样提交，不改写用户配置。
 */
export function WebSocketSettingsSection({ form, onFieldChange }: WebSocketSettingsSectionProps) {
  return (
    <>
      <h2 className="section-title mt-7">WebSocket 重连设置</h2>
      <p className="muted text-sm mb-4">配置后端与 aria2 WebSocket 连接断开后的重连策略。</p>

      <div className="mb-7">
        <label className="form-label-lg" htmlFor="settings-ws-max-delay">
          最大重连延迟: {form.wsReconnectMaxDelaySeconds} 秒
        </label>
        <p className="muted text-sm mb-3">指数退避的最大等待时间（1-300 秒）</p>
        <input
          id="settings-ws-max-delay"
          type="range"
          min="1"
          max="300"
          value={form.wsReconnectMaxDelaySeconds}
          onChange={(e) => onFieldChange("wsReconnectMaxDelaySeconds", e.target.value)}
          className="w-full"
          style={{ maxWidth: 300 }}
          aria-label="最大重连延迟"
        />
      </div>
    </>
  );
}
