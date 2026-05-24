import type { SettingsFormState } from "../settingsState";

type DownloadConnectionSettingsSectionProps = {
  form: Pick<SettingsFormState,
    "downloadTotalConnections" | "downloadAuthenticatedReservedConnections" |
    "downloadAuthenticatedPerUserConnections" | "downloadAuthenticatedPerFileConnections" |
    "downloadAnonymousBaseConnections" | "downloadAnonymousBorrowConnections" |
    "downloadAnonymousPerIpConnections" | "downloadAnonymousPerFileConnections"
  >;
  onFieldChange: <K extends keyof SettingsFormState>(field: K, value: SettingsFormState[K]) => void;
};

type ConnectionField = {
  id: string;
  label: string;
  description: string;
  field: keyof SettingsFormState;
  min: number;
  max: number;
};

const connectionFields: ConnectionField[] = [
  { id: "settings-conn-total", label: "系统总下载连接上限", description: "所有用户的总并发下载连接数上限（0 = 不限制）", field: "downloadTotalConnections", min: 0, max: 10000 },
  { id: "settings-conn-auth-reserved", label: "已登录保底连接数", description: "为已登录下载预留的最小可用连接数", field: "downloadAuthenticatedReservedConnections", min: 0, max: 10000 },
  { id: "settings-conn-user-max", label: "已登录单用户最大并发", description: "单个已登录用户的并发下载连接数上限（0 = 不限制）", field: "downloadAuthenticatedPerUserConnections", min: 0, max: 1000 },
  { id: "settings-conn-auth-per-file", label: "已登录单文件最大并发", description: "同一已登录用户对同一文件的并发下载连接数上限（0 = 不限制）", field: "downloadAuthenticatedPerFileConnections", min: 0, max: 100 },
  { id: "settings-conn-anon-base", label: "匿名基础连接数", description: "匿名分享下载默认可占用的连接数", field: "downloadAnonymousBaseConnections", min: 0, max: 10000 },
  { id: "settings-conn-anon-borrow", label: "匿名可借用连接数", description: "匿名分享在不影响已登录保底的前提下可额外借用的连接数", field: "downloadAnonymousBorrowConnections", min: 0, max: 10000 },
  { id: "settings-conn-anon-per-ip", label: "匿名单 IP 最大并发", description: "单个匿名来源的并发下载连接数上限（0 = 不限制）", field: "downloadAnonymousPerIpConnections", min: 0, max: 1000 },
  { id: "settings-conn-anon-per-file", label: "匿名单文件最大并发", description: "同一匿名来源对同一文件的并发下载连接数上限（0 = 不限制）", field: "downloadAnonymousPerFileConnections", min: 0, max: 100 },
];

export function DownloadConnectionSettingsSection({ form, onFieldChange }: DownloadConnectionSettingsSectionProps) {
  return (
    <>
      <h2 className="section-title mt-7">下载并发限制</h2>
      <p className="muted text-sm mb-4">控制已登录下载保底与匿名分享下载的可借用容量，保证已登录用户体验优先。</p>
      {connectionFields.map(({ id, label, description, field, min, max }) => (
        <div key={id} className="mb-7">
          <label className="form-label-lg" htmlFor={id}>{label}</label>
          <p className="muted text-sm mb-3">{description}</p>
          <input
            id={id}
            type="number"
            min={min}
            max={max}
            value={form[field as keyof typeof form] as number}
            onChange={(e) => onFieldChange(field, Math.max(min, Math.min(max, parseInt(e.target.value) || min)))}
            className="input"
            style={{ maxWidth: 200 }}
            aria-label={label}
          />
        </div>
      ))}
    </>
  );
}
