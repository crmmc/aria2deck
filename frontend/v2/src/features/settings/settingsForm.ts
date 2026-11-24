import type { PackFormat, SettingsResponse, SettingsUpdateRequest } from "@/shared/api/types";

/** 设置表单：数值字段以字符串编辑，保存时统一解析并校验（错误给中文内联）。
 *  只包含 V2 契约真实支持的字段；归档参考 独有控件（隐藏扩展名、旧连接数、WS 百分比抖动、
 *  清空 aria2 Secret）按兼容清单 C02-C06 整块屏蔽，不在表单里留空字段。
 */

export type RateLimitKey =
  | "account_security_rate_limit"
  | "authenticated_api_rate_limit"
  | "public_api_rate_limit"
  | "share_access_rate_limit"
  | "create_task_rate_limit"
  | "create_torrent_rate_limit"
  | "create_pack_rate_limit"
  | "aria2_test_rate_limit"
  | "rpc_rate_limit"
  | "file_search_rate_limit"
  | "create_share_rate_limit";

/** 限流区间跟随 V2 契约（1-1000000），归档参考实现的 0=不限制 与更窄上限按 C32 收紧。 */
export const RATE_LIMIT_MIN = 1;
export const RATE_LIMIT_MAX = 1_000_000;

export interface RateLimitField {
  id: string;
  key: RateLimitKey;
  label: string;
  description: string;
}

/** 顺序、标签与说明沿用 归档参考实现的接口频率限制区块。 */
export const RATE_LIMIT_FIELDS: RateLimitField[] = [
  {
    id: "settings-rate-account-security",
    key: "account_security_rate_limit",
    label: "账户安全限流",
    description: "每 5 分钟最大尝试次数（登录、首个用户创建、修改密码）",
  },
  {
    id: "settings-rate-auth-api",
    key: "authenticated_api_rate_limit",
    label: "普通已登录 API 限流",
    description: "每分钟最大查询请求次数",
  },
  {
    id: "settings-rate-public-api",
    key: "public_api_rate_limit",
    label: "普通匿名公开 API 限流",
    description: "每分钟最大公开查询次数",
  },
  {
    id: "settings-rate-share-access",
    key: "share_access_rate_limit",
    label: "分享密码验证限流",
    description: "每分钟最大密码验证次数",
  },
  {
    id: "settings-rate-create-task",
    key: "create_task_rate_limit",
    label: "创建任务限流",
    description: "每分钟最大创建任务次数",
  },
  {
    id: "settings-rate-create-torrent",
    key: "create_torrent_rate_limit",
    label: "创建种子限流",
    description: "每分钟最大上传种子次数",
  },
  {
    id: "settings-rate-create-pack",
    key: "create_pack_rate_limit",
    label: "创建打包限流",
    description: "每分钟最大创建打包次数",
  },
  {
    id: "settings-rate-create-share",
    key: "create_share_rate_limit",
    label: "创建分享限流",
    description: "每分钟最大创建分享次数",
  },
  {
    id: "settings-rate-aria2-test",
    key: "aria2_test_rate_limit",
    label: "aria2 测试限流",
    description: "每分钟最大连接测试次数",
  },
  {
    id: "settings-rate-rpc",
    key: "rpc_rate_limit",
    label: "JSON-RPC 限流",
    description: "每分钟最大 RPC 请求次数",
  },
  {
    id: "settings-rate-file-search",
    key: "file_search_rate_limit",
    label: "文件搜索限流",
    description: "仅限文件搜索，每分钟最大搜索次数",
  },
];

export type ConnectionLimitKey =
  | "total_connections"
  | "authenticated_per_user_connections"
  | "authenticated_per_file_connections"
  | "anonymous_total_connections"
  | "anonymous_per_ip_connections"
  | "anonymous_per_file_connections";

/** 六个后端配置键统一 0-1000（V2 domain 校验口径）。 */
export const DOWNLOAD_CONNECTION_LIMIT_MIN = 0;
export const DOWNLOAD_CONNECTION_LIMIT_MAX = 1000;

export interface ConnectionLimitField {
  id: string;
  key: ConnectionLimitKey;
  label: string;
  description: string;
}

/** 顺序与文案取自 归档参考 可 1:1 映射的五项；匿名池在 V2 合并为一个键（C04）。 */
export const DOWNLOAD_CONNECTION_LIMIT_FIELDS: ConnectionLimitField[] = [
  {
    id: "settings-conn-total",
    key: "total_connections",
    label: "系统总下载连接上限",
    description: "所有用户的总并发下载连接数上限（0 = 不限制）",
  },
  {
    id: "settings-conn-user-max",
    key: "authenticated_per_user_connections",
    label: "已登录单用户最大并发",
    description: "单个已登录用户的并发下载连接数上限（0 = 不限制）",
  },
  {
    id: "settings-conn-auth-per-file",
    key: "authenticated_per_file_connections",
    label: "已登录单文件最大并发",
    description: "同一已登录用户对同一文件的并发下载连接数上限（0 = 不限制）",
  },
  {
    id: "settings-conn-anon-total",
    key: "anonymous_total_connections",
    label: "匿名总连接数",
    description: "匿名分享下载可占用的连接数总量，独立于已登录容量（0 = 全面拒绝匿名下载）",
  },
  {
    id: "settings-conn-anon-per-ip",
    key: "anonymous_per_ip_connections",
    label: "匿名单 IP 最大并发",
    description: "单个匿名来源的并发下载连接数上限（0 = 不限制）",
  },
  {
    id: "settings-conn-anon-per-file",
    key: "anonymous_per_file_connections",
    label: "匿名单文件最大并发",
    description: "同一匿名来源对同一文件的并发下载连接数上限（0 = 不限制）",
  },
];

const GIB = 1024 ** 3;

export interface SettingsForm {
  siteTitle: string;
  maxTaskSizeGib: string;
  minFreeDiskGib: string;
  aria2RpcUrl: string;
  /** 掩码或空 = 保存时不提交，后端保留现值（C06：V2 没有清空已配置 Secret 的表示）。 */
  aria2RpcSecret: string;
  aria2BtStopTimeoutSeconds: string;
  packFormat: PackFormat;
  packCompressionLevel: string;
  wsReconnectMaxDelaySeconds: string;
  /** WS 抖动/倍数为 V2 独有语义（秒/整数倍），不进入界面，仅原值往返。 */
  wsReconnectJitterSeconds: string;
  wsReconnectFactor: string;
  historyRetentionDays: string;
  trackerFixedListText: string;
  trackerRemoteUrlsText: string;
  trackerRefreshIntervalMinutes: string;
  /** 留空 = 不覆盖该键，沿用后端默认；空串不得被当成 0 提交。 */
  downloadConnectionLimits: Record<string, string>;
  rateLimits: Record<string, string>;
}

export type SettingsFormAction =
  | { type: "replace"; state: SettingsForm }
  | { type: "field"; field: keyof SettingsForm; value: SettingsForm[keyof SettingsForm] };

/** 页面把 dispatch 包成这个签名传给各 section（沿用 归档参考实现的 setField 写法）。 */
export type SettingsFormSetter = <K extends keyof SettingsForm>(
  field: K,
  value: SettingsForm[K],
) => void;

/** 归档参考表单 reducer 结构保留：筛选掉隐藏扩展名相关 action 后只剩载入替换与单字段更新。 */
export function settingsFormReducer(state: SettingsForm, action: SettingsFormAction): SettingsForm {
  switch (action.type) {
    case "replace":
      return action.state;
    case "field":
      return { ...state, [action.field]: action.value };
  }
}

export const initialSettingsFormState: SettingsForm = {
  siteTitle: "",
  maxTaskSizeGib: "",
  minFreeDiskGib: "",
  aria2RpcUrl: "",
  aria2RpcSecret: "",
  aria2BtStopTimeoutSeconds: "300",
  packFormat: "zip",
  packCompressionLevel: "6",
  wsReconnectMaxDelaySeconds: "30",
  wsReconnectJitterSeconds: "1",
  wsReconnectFactor: "2",
  historyRetentionDays: "30",
  trackerFixedListText: "",
  trackerRemoteUrlsText: "",
  trackerRefreshIntervalMinutes: "0",
  downloadConnectionLimits: {},
  rateLimits: {},
};

function gibToBytes(raw: string, label: string): { value: number; error: string | null } {
  const gib = Number(raw);
  if (raw.trim() === "" || !Number.isFinite(gib) || gib < 0) {
    return { value: 0, error: `${label}必须是不小于 0 的数字` };
  }
  return { value: Math.round(gib * GIB), error: null };
}

function parseIntInRange(
  raw: string,
  label: string,
  min: number,
  max: number,
): { value: number; error: string | null } {
  const value = Number(raw);
  if (raw.trim() === "" || !Number.isInteger(value)) {
    return { value: 0, error: `${label}必须是整数` };
  }
  if (value < min || value > max) {
    return { value: 0, error: `${label}必须在 ${min} 到 ${max} 之间` };
  }
  return { value, error: null };
}

function parseLines(raw: string): string[] {
  return raw
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line !== "");
}

export function formFromSettings(settings: SettingsResponse): SettingsForm {
  const storedLimits = settings.download_connection_limits;
  return {
    siteTitle: settings.site_title,
    maxTaskSizeGib: String(Math.round((settings.max_task_size / GIB) * 100) / 100),
    minFreeDiskGib: String(Math.round((settings.min_free_disk / GIB) * 100) / 100),
    aria2RpcUrl: settings.aria2_rpc_url,
    aria2RpcSecret: settings.aria2_rpc_secret ?? "",
    aria2BtStopTimeoutSeconds: String(settings.aria2_bt_stop_timeout_seconds),
    packFormat: settings.pack_format,
    packCompressionLevel: String(settings.pack_compression_level),
    wsReconnectMaxDelaySeconds: String(settings.ws_reconnect_max_delay_seconds),
    wsReconnectJitterSeconds: String(settings.ws_reconnect_jitter_seconds),
    wsReconnectFactor: String(settings.ws_reconnect_factor),
    historyRetentionDays: String(settings.history_retention_days),
    trackerFixedListText: settings.tracker_fixed_list.join("\n"),
    trackerRemoteUrlsText: settings.tracker_remote_urls.join("\n"),
    trackerRefreshIntervalMinutes: String(settings.tracker_refresh_interval_minutes),
    downloadConnectionLimits: Object.fromEntries(
      DOWNLOAD_CONNECTION_LIMIT_FIELDS.map(({ key }) => [
        key,
        storedLimits[key] === undefined ? "" : String(storedLimits[key]),
      ]),
    ),
    rateLimits: Object.fromEntries(
      RATE_LIMIT_FIELDS.map(({ key }) => [key, String(settings[key])]),
    ),
  };
}

export type PayloadResult =
  | { ok: true; payload: SettingsUpdateRequest }
  | { ok: false; error: string };

export function buildUpdatePayload(form: SettingsForm): PayloadResult {
  const payload: SettingsUpdateRequest = {};

  if (form.siteTitle.length > 100) {
    return { ok: false, error: "网站标题最多 100 个字符" };
  }
  payload.site_title = form.siteTitle;

  const maxSize = gibToBytes(form.maxTaskSizeGib, "最大任务大小");
  if (maxSize.error !== null) return { ok: false, error: maxSize.error };
  payload.max_task_size = maxSize.value;

  const minDisk = gibToBytes(form.minFreeDiskGib, "最小剩余磁盘空间");
  if (minDisk.error !== null) return { ok: false, error: minDisk.error };
  payload.min_free_disk = minDisk.value;

  payload.aria2_rpc_url = form.aria2RpcUrl.trim();
  // 掩码/留空都不提交，避免覆盖真实 Secret；V2 无清空语义（C06）。
  const secret = form.aria2RpcSecret.trim();
  if (secret !== "" && !secret.startsWith("*")) payload.aria2_rpc_secret = secret;

  const btStop = parseIntInRange(form.aria2BtStopTimeoutSeconds, "BT 停止超时", 1, 86400);
  if (btStop.error !== null) return { ok: false, error: btStop.error };
  payload.aria2_bt_stop_timeout_seconds = btStop.value;

  payload.pack_format = form.packFormat;

  const packLevel = parseIntInRange(form.packCompressionLevel, "压缩级别", 0, 22);
  if (packLevel.error !== null) return { ok: false, error: packLevel.error };
  payload.pack_compression_level = packLevel.value;

  const wsMaxDelay = parseIntInRange(form.wsReconnectMaxDelaySeconds, "重连最大延迟", 1, 300);
  if (wsMaxDelay.error !== null) return { ok: false, error: wsMaxDelay.error };
  payload.ws_reconnect_max_delay_seconds = wsMaxDelay.value;

  const wsJitter = parseIntInRange(form.wsReconnectJitterSeconds, "重连抖动", 0, 30);
  if (wsJitter.error !== null) return { ok: false, error: wsJitter.error };
  payload.ws_reconnect_jitter_seconds = wsJitter.value;

  const wsFactor = parseIntInRange(form.wsReconnectFactor, "重连退避倍数", 1, 10);
  if (wsFactor.error !== null) return { ok: false, error: wsFactor.error };
  payload.ws_reconnect_factor = wsFactor.value;

  const retention = parseIntInRange(form.historyRetentionDays, "历史保留天数", 1, 3650);
  if (retention.error !== null) return { ok: false, error: retention.error };
  payload.history_retention_days = retention.value;

  payload.tracker_fixed_list = parseLines(form.trackerFixedListText);
  payload.tracker_remote_urls = parseLines(form.trackerRemoteUrlsText);

  const trackerInterval = parseIntInRange(
    form.trackerRefreshIntervalMinutes,
    "Tracker 刷新间隔",
    0,
    10080,
  );
  if (trackerInterval.error !== null) return { ok: false, error: trackerInterval.error };
  // 后端只接受 0（仅手动）或 5-10080，提前拦住 1-4 避免整笔保存被 422 拒绝。
  if (trackerInterval.value !== 0 && trackerInterval.value < 5) {
    return { ok: false, error: "Tracker 刷新间隔需为 0（仅手动）或不小于 5 分钟" };
  }
  payload.tracker_refresh_interval_minutes = trackerInterval.value;

  const limits: Record<string, number> = {};
  for (const { key, label } of DOWNLOAD_CONNECTION_LIMIT_FIELDS) {
    const raw = (form.downloadConnectionLimits[key] ?? "").trim();
    if (raw === "") continue;
    const parsed = parseIntInRange(
      raw,
      label,
      DOWNLOAD_CONNECTION_LIMIT_MIN,
      DOWNLOAD_CONNECTION_LIMIT_MAX,
    );
    if (parsed.error !== null) return { ok: false, error: parsed.error };
    limits[key] = parsed.value;
  }
  payload.download_connection_limits = limits;

  for (const field of RATE_LIMIT_FIELDS) {
    const parsed = parseIntInRange(
      form.rateLimits[field.key] ?? "",
      `${field.label}`,
      RATE_LIMIT_MIN,
      RATE_LIMIT_MAX,
    );
    if (parsed.error !== null) return { ok: false, error: parsed.error };
    payload[field.key] = parsed.value;
  }

  return { ok: true, payload };
}

type SettingsPayloadKey = keyof SettingsUpdateRequest;

function payloadValuesEqual(left: unknown, right: unknown): boolean {
  return JSON.stringify(left) === JSON.stringify(right);
}

function changedPayloadKeys(
  current: SettingsUpdateRequest,
  saved: SettingsUpdateRequest,
): SettingsPayloadKey[] {
  const keys = new Set<SettingsPayloadKey>([
    ...(Object.keys(current) as SettingsPayloadKey[]),
    ...(Object.keys(saved) as SettingsPayloadKey[]),
  ]);
  return [...keys].filter((key) => !payloadValuesEqual(current[key], saved[key]));
}

export function isSettingsFormDirty(form: SettingsForm, savedForm: SettingsForm): boolean {
  const current = buildUpdatePayload(form);
  if (!current.ok) return true;
  const saved = buildUpdatePayload(savedForm);
  if (!saved.ok) return true;
  return changedPayloadKeys(current.payload, saved.payload).length > 0;
}

export function buildChangedUpdatePayload(
  form: SettingsForm,
  savedForm: SettingsForm,
): PayloadResult {
  const current = buildUpdatePayload(form);
  if (!current.ok) return current;
  const saved = buildUpdatePayload(savedForm);
  if (!saved.ok) return saved;
  const payload = Object.fromEntries(
    changedPayloadKeys(current.payload, saved.payload).map((key) => [key, current.payload[key]]),
  ) as SettingsUpdateRequest;
  return { ok: true, payload };
}
