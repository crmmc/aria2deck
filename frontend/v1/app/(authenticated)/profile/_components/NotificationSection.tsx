import type { NotificationSettings } from "@/lib/notification";

type NotificationSectionProps = {
  supported: boolean;
  settings: NotificationSettings;
  onEnabledChange: (enabled: boolean) => void;
  onOptionChange: (key: "onComplete" | "onError", value: boolean) => void;
};

export function NotificationSection({
  supported,
  settings,
  onEnabledChange,
  onOptionChange,
}: NotificationSectionProps) {
  return (
    <div className="card mb-6">
      <h2 className="section-title">浏览器通知</h2>

      {!supported ? (
        <div className="alert alert-warning">
          <p>您的浏览器不支持通知功能</p>
        </div>
      ) : (
        <div className="max-w-600">
          <div className="mb-6">
            <div className="flex-between mb-2">
              <label className="font-semibold" htmlFor="profile-notify-toggle">启用通知</label>
              <button
                id="profile-notify-toggle"
                type="button"
                onClick={() => onEnabledChange(!settings.enabled)}
                className={`toggle-switch ${settings.enabled ? "toggle-switch-on" : "toggle-switch-off"}`}
                aria-label="启用通知"
              >
                <div
                  className="toggle-knob"
                  style={{ left: settings.enabled ? 24 : 2 }}
                />
              </button>
            </div>
            <p className="muted text-sm">当下载任务状态变化时，发送浏览器桌面通知</p>
          </div>

          {settings.enabled && (
            <div className="bg-black-02 rounded-lg p-4">
              <p className="muted text-sm mb-4">选择何时发送通知：</p>

              <div className="mb-4">
                <div className="flex-between">
                  <label className="text-base" htmlFor="profile-notify-complete">下载完成时</label>
                  <button
                    id="profile-notify-complete"
                    type="button"
                    onClick={() => onOptionChange("onComplete", !settings.onComplete)}
                    className={`toggle-switch toggle-switch-sm ${settings.onComplete ? "toggle-switch-on" : "toggle-switch-off"}`}
                    aria-label="下载完成时通知"
                  >
                    <div
                      className="toggle-knob toggle-knob-sm"
                      style={{ left: settings.onComplete ? 22 : 2 }}
                    />
                  </button>
                </div>
              </div>

              <div>
                <div className="flex-between">
                  <label className="text-base" htmlFor="profile-notify-error">下载失败时</label>
                  <button
                    id="profile-notify-error"
                    type="button"
                    onClick={() => onOptionChange("onError", !settings.onError)}
                    className={`toggle-switch toggle-switch-sm ${settings.onError ? "toggle-switch-on" : "toggle-switch-off"}`}
                    aria-label="下载失败时通知"
                  >
                    <div
                      className="toggle-knob toggle-knob-sm"
                      style={{ left: settings.onError ? 22 : 2 }}
                    />
                  </button>
                </div>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
