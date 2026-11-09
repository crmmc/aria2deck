import type { PackFormat, SettingsResponse, SettingsUpdateRequest } from "@/shared/api/types";

/** 设置表单：数值字段以字符串编辑，保存时统一解析并校验（错误给中文内联）。 */

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

export interface RateLimitField {
  key: RateLimitKey;
  label: string;
}

export const RATE_LIMIT_FIELDS: RateLimitField[] = [
  { key: "account_security_rate_limit", label: "账户安全" },
  { key: "authenticated_api_rate_limit", label: "已认证接口" },
  { key: "public_api_rate_limit", label: "公开接口" },
  { key: "share_access_rate_limit", label: "分享访问" },
  { key: "create_task_rate_limit", label: "创建任务" },
  { key: "create_torrent_rate_limit", label: "种子解析" },
  { key: "create_pack_rate_limit", label: "创建打包" },
  { key: "aria2_test_rate_limit", label: "连接测试" },
  { key: "rpc_rate_limit", label: "JSON-RPC" },
  { key: "file_search_rate_limit", label: "文件搜索" },
  { key: "create_share_rate_limit", label: "创建分享" },
];

const GIB = 1024 ** 3;
const DOWNLOAD_CONNECTION_LIMIT_KEYS = new Set([
  "total_connections",
  "authenticated_per_user_connections",
  "authenticated_per_file_connections",
  "anonymous_total_connections",
  "anonymous_per_ip_connections",
  "anonymous_per_file_connections",
]);

export interface SettingsForm {
  siteTitle: string;
  maxTaskSizeGib: string;
  minFreeDiskGib: string;
  aria2RpcUrl: string;
  aria2RpcSecret: string; // 留空 = 保持不变（不随保存提交）。
  aria2BtStopTimeoutSeconds: string;
  packFormat: PackFormat;
  packCompressionLevel: string;
  wsReconnectMaxDelaySeconds: string;
  wsReconnectJitterSeconds: string;
  wsReconnectFactor: string;
  historyRetentionDays: string;
  trackerFixedListText: string;
  trackerRemoteUrlsText: string;
  trackerRefreshIntervalMinutes: string;
  downloadConnectionLimitsText: string;
  rateLimits: Record<string, string>;
}

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

function parseConnectionLimits(raw: string): {
  value: Record<string, number>;
  error: string | null;
} {
  const result: Record<string, number> = {};
  for (const line of parseLines(raw)) {
    const separator = line.indexOf("=");
    if (separator <= 0) {
      return { value: {}, error: `下载连接数格式无效：${line}（应为配置键=连接数）` };
    }
    const key = line.slice(0, separator);
    const rawValue = line.slice(separator + 1);
    const value = Number(rawValue);
    if (
      rawValue.trim() === "" ||
      !DOWNLOAD_CONNECTION_LIMIT_KEYS.has(key) ||
      !Number.isInteger(value) ||
      value < 0 ||
      value > 1000
    ) {
      return { value: {}, error: `下载连接数配置无效：${line}（仅支持六个配置键，连接数 0-1000）` };
    }
    result[key] = value;
  }
  return { value: result, error: null };
}

export function formFromSettings(settings: SettingsResponse): SettingsForm {
  return {
    siteTitle: settings.site_title,
    maxTaskSizeGib: String(Math.round((settings.max_task_size / GIB) * 100) / 100),
    minFreeDiskGib: String(Math.round((settings.min_free_disk / GIB) * 100) / 100),
    aria2RpcUrl: settings.aria2_rpc_url,
    aria2RpcSecret: "",
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
    downloadConnectionLimitsText: Object.entries(settings.download_connection_limits)
      .map(([key, value]) => `${key}=${value}`)
      .join("\n"),
    rateLimits: Object.fromEntries(
      RATE_LIMIT_FIELDS.map((field) => [
        field.key,
        String(settings[field.key as keyof SettingsResponse]),
      ]),
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
  // 掩码/留空都不提交，避免覆盖真实 Secret。
  const secret = form.aria2RpcSecret.trim();
  if (secret !== "") payload.aria2_rpc_secret = secret;

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
  payload.tracker_refresh_interval_minutes = trackerInterval.value;

  const limits = parseConnectionLimits(form.downloadConnectionLimitsText);
  if (limits.error !== null) return { ok: false, error: limits.error };
  payload.download_connection_limits = limits.value;

  for (const field of RATE_LIMIT_FIELDS) {
    const parsed = parseIntInRange(form.rateLimits[field.key] ?? "", `${field.label}限流`, 1, 1_000_000);
    if (parsed.error !== null) return { ok: false, error: parsed.error };
    payload[field.key] = parsed.value;
  }

  return { ok: true, payload };
}
