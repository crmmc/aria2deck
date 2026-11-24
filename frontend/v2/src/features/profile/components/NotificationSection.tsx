export interface NotificationSettings {
  enabled: boolean;
  onComplete: boolean;
  onError: boolean;
}

type NotificationSectionProps = {
  supported: boolean;
  settings: NotificationSettings;
  onEnabledChange: (enabled: boolean) => void;
  onOptionChange: (key: "onComplete" | "onError", value: boolean) => void;
};

type SwitchRowProps = {
  id: string;
  label: string;
  description?: string;
  checked: boolean;
  ariaLabel?: string;
  onChange: () => void;
};

function SwitchRow({ id, label, description, checked, ariaLabel, onChange }: SwitchRowProps) {
  return (
    <div className="switch-row">
      <div className="switch-copy">
        <strong><label htmlFor={id}>{label}</label></strong>
        {description && <p>{description}</p>}
      </div>
      <div className="switch-control">
        <span className="switch-state">{checked ? "已启用" : "已停用"}</span>
        <button
          id={id}
          type="button"
          onClick={onChange}
          className={`toggle-switch ${checked ? "toggle-switch-on" : "toggle-switch-off"}`}
          aria-label={ariaLabel ?? label}
        >
          <div className="toggle-knob" style={{ transform: checked ? "translateX(20px)" : "none" }} />
        </button>
      </div>
    </div>
  );
}

export function NotificationSection({ supported, settings, onEnabledChange, onOptionChange }: NotificationSectionProps) {
  return (
    <section className="panel profile-panel">
      <div className="panel-head"><h2>浏览器通知</h2></div>
      <div className="panel-body">
        {!supported ? (
          <div className="alert alert-warning"><p>您的浏览器不支持通知功能</p></div>
        ) : (
          <>
            <SwitchRow
              id="profile-notify-toggle"
              label="启用通知"
              description="任务状态变化时发送桌面通知。"
              checked={settings.enabled}
              onChange={() => onEnabledChange(!settings.enabled)}
            />
            {settings.enabled && (
              <>
                <SwitchRow id="profile-notify-complete" label="下载完成时" ariaLabel="下载完成时通知" checked={settings.onComplete} onChange={() => onOptionChange("onComplete", !settings.onComplete)} />
                <SwitchRow id="profile-notify-error" label="下载失败时" ariaLabel="下载失败时通知" checked={settings.onError} onChange={() => onOptionChange("onError", !settings.onError)} />
              </>
            )}
          </>
        )}
      </div>
    </section>
  );
}
