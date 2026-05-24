import type { SettingsFormState } from "../settingsState";

type WebSocketSettingsSectionProps = {
  form: Pick<SettingsFormState, "wsReconnectMaxDelay" | "wsReconnectJitter" | "wsReconnectFactor">;
  onFieldChange: <K extends keyof SettingsFormState>(field: K, value: SettingsFormState[K]) => void;
};

export function WebSocketSettingsSection({ form, onFieldChange }: WebSocketSettingsSectionProps) {
  return (
    <>
      <h2 className="section-title mt-7">WebSocket 重连设置</h2>
      <p className="muted text-sm mb-4">配置后端与 aria2 WebSocket 连接断开后的重连策略。</p>

      <div className="mb-6">
        <label className="form-label-lg" htmlFor="settings-ws-max-delay">最大重连延迟: {form.wsReconnectMaxDelay} 秒</label>
        <p className="muted text-sm mb-3">指数退避的最大等待时间（1-300 秒）</p>
        <input
          id="settings-ws-max-delay"
          type="range"
          min="1"
          max="300"
          value={form.wsReconnectMaxDelay}
          onChange={(e) => onFieldChange("wsReconnectMaxDelay", parseInt(e.target.value))}
          className="w-full"
          style={{ maxWidth: 300 }}
          aria-label="最大重连延迟"
        />
      </div>

      <div className="mb-6">
        <label className="form-label-lg" htmlFor="settings-ws-jitter">抖动系数: {(form.wsReconnectJitter * 100).toFixed(0)}%</label>
        <p className="muted text-sm mb-3">重连延迟的随机波动范围（0-100%）</p>
        <input
          id="settings-ws-jitter"
          type="range"
          min="0"
          max="100"
          value={form.wsReconnectJitter * 100}
          onChange={(e) => onFieldChange("wsReconnectJitter", parseInt(e.target.value) / 100)}
          className="w-full"
          style={{ maxWidth: 300 }}
          aria-label="抖动系数"
        />
      </div>

      <div className="mb-7">
        <label className="form-label-lg" htmlFor="settings-ws-factor">指数因子: {form.wsReconnectFactor.toFixed(1)}</label>
        <p className="muted text-sm mb-3">每次重连延迟的倍增系数（1.1-10）</p>
        <input
          id="settings-ws-factor"
          type="range"
          min="11"
          max="100"
          value={form.wsReconnectFactor * 10}
          onChange={(e) => onFieldChange("wsReconnectFactor", parseInt(e.target.value) / 10)}
          className="w-full"
          style={{ maxWidth: 300 }}
          aria-label="指数因子"
        />
      </div>
    </>
  );
}
