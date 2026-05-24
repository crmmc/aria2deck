import { bytesToGB, gbToBytes } from "@/lib/utils";

export type SettingsFormState = {
  maxTaskSize: string;
  minFreeDisk: string;
  aria2RpcUrl: string;
  aria2RpcSecret: string;
  hiddenExtensions: string[];
  extensionInput: string;
  packFormat: "zip" | "tar.zst";
  packCompressionLevel: number;
  wsReconnectMaxDelay: number;
  wsReconnectJitter: number;
  wsReconnectFactor: number;
  siteTitle: string;
  rateLimitAccountSecurity: number;
  rateLimitAuthenticatedApi: number;
  rateLimitPublicApi: number;
  rateLimitShareAccess: number;
  rateLimitAuthenticatedDownload: number;
  rateLimitAnonymousDownload: number;
  rateLimitCreateTask: number;
  rateLimitCreateTorrent: number;
  rateLimitCreatePack: number;
  rateLimitAria2Test: number;
  rateLimitRpc: number;
  downloadTotalConnections: number;
  downloadAuthenticatedReservedConnections: number;
  downloadAuthenticatedPerUserConnections: number;
  downloadAuthenticatedPerFileConnections: number;
  downloadAnonymousBaseConnections: number;
  downloadAnonymousBorrowConnections: number;
  downloadAnonymousPerIpConnections: number;
  downloadAnonymousPerFileConnections: number;
};

export type SettingsFormAction =
  | { type: "replace"; state: SettingsFormState }
  | { type: "field"; field: keyof SettingsFormState; value: SettingsFormState[keyof SettingsFormState] }
  | { type: "add_hidden_extension"; extension: string }
  | { type: "remove_hidden_extension"; extension: string }
  | { type: "set_extension_input"; value: string };

export const initialSettingsFormState: SettingsFormState = {
  maxTaskSize: "",
  minFreeDisk: "",
  aria2RpcUrl: "",
  aria2RpcSecret: "",
  hiddenExtensions: [],
  extensionInput: "",
  packFormat: "zip",
  packCompressionLevel: 5,
  wsReconnectMaxDelay: 60,
  wsReconnectJitter: 0.2,
  wsReconnectFactor: 2,
  siteTitle: "",
  rateLimitAccountSecurity: 5,
  rateLimitAuthenticatedApi: 60,
  rateLimitPublicApi: 60,
  rateLimitShareAccess: 5,
  rateLimitAuthenticatedDownload: 300,
  rateLimitAnonymousDownload: 60,
  rateLimitCreateTask: 30,
  rateLimitCreateTorrent: 20,
  rateLimitCreatePack: 5,
  rateLimitAria2Test: 10,
  rateLimitRpc: 300,
  downloadTotalConnections: 100,
  downloadAuthenticatedReservedConnections: 60,
  downloadAuthenticatedPerUserConnections: 16,
  downloadAuthenticatedPerFileConnections: 8,
  downloadAnonymousBaseConnections: 20,
  downloadAnonymousBorrowConnections: 20,
  downloadAnonymousPerIpConnections: 4,
  downloadAnonymousPerFileConnections: 2,
};

export function settingsFormReducer(state: SettingsFormState, action: SettingsFormAction): SettingsFormState {
  switch (action.type) {
    case "replace":
      return action.state;
    case "field":
      return { ...state, [action.field]: action.value };
    case "add_hidden_extension": {
      const ext = action.extension.trim().toLowerCase();
      if (!ext) return state;
      const normalized = ext.startsWith(".") ? ext : "." + ext;
      if (state.hiddenExtensions.includes(normalized)) {
        return { ...state, extensionInput: "" };
      }
      return {
        ...state,
        hiddenExtensions: [...state.hiddenExtensions, normalized],
        extensionInput: "",
      };
    }
    case "remove_hidden_extension":
      return {
        ...state,
        hiddenExtensions: state.hiddenExtensions.filter((e) => e !== action.extension),
      };
    case "set_extension_input":
      return { ...state, extensionInput: action.value };
  }
}

export function configToSettingsFormState(cfg: Record<string, unknown>): SettingsFormState {
  return {
    maxTaskSize: bytesToGB(cfg.max_task_size as number),
    minFreeDisk: bytesToGB(cfg.min_free_disk as number),
    aria2RpcUrl: (cfg.aria2_rpc_url as string) || "",
    aria2RpcSecret: (cfg.aria2_rpc_secret as string) || "",
    hiddenExtensions: (cfg.hidden_file_extensions as string[]) || [],
    extensionInput: "",
    packFormat: (cfg.pack_format as "zip" | "tar.zst") || "zip",
    packCompressionLevel: (cfg.pack_compression_level as number) ?? 5,
    wsReconnectMaxDelay: (cfg.ws_reconnect_max_delay as number) ?? 60,
    wsReconnectJitter: (cfg.ws_reconnect_jitter as number) ?? 0.2,
    wsReconnectFactor: (cfg.ws_reconnect_factor as number) ?? 2,
    siteTitle: (cfg.site_title as string) || "",
    rateLimitAccountSecurity: (cfg.rate_limit_account_security as number) ?? 5,
    rateLimitAuthenticatedApi: (cfg.rate_limit_authenticated_api as number) ?? 60,
    rateLimitPublicApi: (cfg.rate_limit_public_api as number) ?? 60,
    rateLimitShareAccess: (cfg.rate_limit_share_access as number) ?? 5,
    rateLimitAuthenticatedDownload: (cfg.rate_limit_authenticated_download as number) ?? 300,
    rateLimitAnonymousDownload: (cfg.rate_limit_anonymous_download as number) ?? 60,
    rateLimitCreateTask: (cfg.rate_limit_create_task as number) ?? 30,
    rateLimitCreateTorrent: (cfg.rate_limit_create_torrent as number) ?? 20,
    rateLimitCreatePack: (cfg.rate_limit_create_pack as number) ?? 5,
    rateLimitAria2Test: (cfg.rate_limit_aria2_test as number) ?? 10,
    rateLimitRpc: (cfg.rate_limit_rpc as number) ?? 300,
    downloadTotalConnections: (cfg.download_total_connections as number) ?? 100,
    downloadAuthenticatedReservedConnections: (cfg.download_authenticated_reserved_connections as number) ?? 60,
    downloadAuthenticatedPerUserConnections: (cfg.download_authenticated_per_user_connections as number) ?? 16,
    downloadAuthenticatedPerFileConnections: (cfg.download_authenticated_per_file_connections as number) ?? 8,
    downloadAnonymousBaseConnections: (cfg.download_anonymous_base_connections as number) ?? 20,
    downloadAnonymousBorrowConnections: (cfg.download_anonymous_borrow_connections as number) ?? 20,
    downloadAnonymousPerIpConnections: (cfg.download_anonymous_per_ip_connections as number) ?? 4,
    downloadAnonymousPerFileConnections: (cfg.download_anonymous_per_file_connections as number) ?? 2,
  };
}

export type SettingsPayloadValidation =
  | { valid: true; payload: Record<string, unknown> }
  | { valid: false; error: string };

export function settingsFormStateToPayload(state: SettingsFormState): SettingsPayloadValidation {
  const maxTaskSizeGb = parseFloat(state.maxTaskSize);
  const minFreeDiskGb = parseFloat(state.minFreeDisk);

  if (!Number.isFinite(maxTaskSizeGb) || maxTaskSizeGb <= 0) {
    return { valid: false, error: "最大任务大小必须为正数" };
  }
  if (!Number.isFinite(minFreeDiskGb) || minFreeDiskGb <= 0) {
    return { valid: false, error: "最小剩余磁盘空间必须为正数" };
  }

  const newMax = gbToBytes(maxTaskSizeGb);
  const newMin = gbToBytes(minFreeDiskGb);
  if (isNaN(newMax) || isNaN(newMin) || newMax <= 0 || newMin <= 0) {
    return { valid: false, error: "请输入有效的数值" };
  }

  return {
    valid: true,
    payload: {
      max_task_size: newMax,
      min_free_disk: newMin,
      aria2_rpc_url: state.aria2RpcUrl,
      aria2_rpc_secret: state.aria2RpcSecret.startsWith("*")
        ? undefined
        : state.aria2RpcSecret,
      hidden_file_extensions: state.hiddenExtensions,
      pack_format: state.packFormat,
      pack_compression_level: state.packCompressionLevel,
      ws_reconnect_max_delay: state.wsReconnectMaxDelay,
      ws_reconnect_jitter: state.wsReconnectJitter,
      ws_reconnect_factor: state.wsReconnectFactor,
      site_title: state.siteTitle || undefined,
      rate_limit_account_security: state.rateLimitAccountSecurity,
      rate_limit_authenticated_api: state.rateLimitAuthenticatedApi,
      rate_limit_public_api: state.rateLimitPublicApi,
      rate_limit_share_access: state.rateLimitShareAccess,
      rate_limit_authenticated_download: state.rateLimitAuthenticatedDownload,
      rate_limit_anonymous_download: state.rateLimitAnonymousDownload,
      rate_limit_create_task: state.rateLimitCreateTask,
      rate_limit_create_torrent: state.rateLimitCreateTorrent,
      rate_limit_create_pack: state.rateLimitCreatePack,
      rate_limit_aria2_test: state.rateLimitAria2Test,
      rate_limit_rpc: state.rateLimitRpc,
      download_total_connections: state.downloadTotalConnections,
      download_authenticated_reserved_connections: state.downloadAuthenticatedReservedConnections,
      download_authenticated_per_user_connections: state.downloadAuthenticatedPerUserConnections,
      download_authenticated_per_file_connections: state.downloadAuthenticatedPerFileConnections,
      download_anonymous_base_connections: state.downloadAnonymousBaseConnections,
      download_anonymous_borrow_connections: state.downloadAnonymousBorrowConnections,
      download_anonymous_per_ip_connections: state.downloadAnonymousPerIpConnections,
      download_anonymous_per_file_connections: state.downloadAnonymousPerFileConnections,
    },
  };
}
