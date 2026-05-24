import type { SettingsFormState } from "../settingsState";

type RateLimitSettingsSectionProps = {
  form: Pick<SettingsFormState,
    "rateLimitAccountSecurity" | "rateLimitAuthenticatedApi" | "rateLimitPublicApi" |
    "rateLimitShareAccess" | "rateLimitAuthenticatedDownload" | "rateLimitAnonymousDownload" |
    "rateLimitCreateTask" | "rateLimitCreateTorrent" | "rateLimitCreatePack" |
    "rateLimitAria2Test" | "rateLimitRpc"
  >;
  onFieldChange: <K extends keyof SettingsFormState>(field: K, value: SettingsFormState[K]) => void;
};

type RateLimitField = {
  id: string;
  label: string;
  description: string;
  field: keyof SettingsFormState;
  min: number;
  max: number;
};

const rateLimitFields: RateLimitField[] = [
  { id: "settings-rate-account-security", label: "账户安全限流", description: "每 5 分钟最大尝试次数（登录、首个用户创建、修改密码）", field: "rateLimitAccountSecurity", min: 1, max: 100 },
  { id: "settings-rate-auth-api", label: "普通已登录 API 限流", description: "每分钟最大查询请求次数（0 = 不限制）", field: "rateLimitAuthenticatedApi", min: 0, max: 10000 },
  { id: "settings-rate-public-api", label: "普通匿名公开 API 限流", description: "每分钟最大公开查询次数（0 = 不限制）", field: "rateLimitPublicApi", min: 0, max: 10000 },
  { id: "settings-rate-share-access", label: "分享密码验证限流", description: "每分钟最大密码验证次数", field: "rateLimitShareAccess", min: 1, max: 10000 },
  { id: "settings-rate-auth-download", label: "已登录下载限流", description: "每分钟最大请求次数（0 = 不限制）", field: "rateLimitAuthenticatedDownload", min: 0, max: 10000 },
  { id: "settings-rate-anon-download", label: "匿名下载限流", description: "每分钟最大匿名下载请求次数（0 = 不限制）", field: "rateLimitAnonymousDownload", min: 0, max: 10000 },
  { id: "settings-rate-create-task", label: "创建任务限流", description: "每分钟最大创建任务次数", field: "rateLimitCreateTask", min: 1, max: 10000 },
  { id: "settings-rate-create-torrent", label: "创建种子限流", description: "每分钟最大上传种子次数", field: "rateLimitCreateTorrent", min: 1, max: 10000 },
  { id: "settings-rate-create-pack", label: "创建打包限流", description: "每分钟最大创建打包次数", field: "rateLimitCreatePack", min: 1, max: 10000 },
  { id: "settings-rate-aria2-test", label: "aria2 测试限流", description: "每分钟最大连接测试次数", field: "rateLimitAria2Test", min: 1, max: 10000 },
  { id: "settings-rate-rpc", label: "JSON-RPC 限流", description: "每分钟最大 RPC 请求次数", field: "rateLimitRpc", min: 1, max: 10000 },
];

export function RateLimitSettingsSection({ form, onFieldChange }: RateLimitSettingsSectionProps) {
  return (
    <>
      <h2 className="section-title">接口频率限制</h2>
      <p className="muted text-sm mb-4">限制用户在单位时间内的请求次数，修改后即时生效。</p>
      {rateLimitFields.map(({ id, label, description, field, min, max }) => (
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
