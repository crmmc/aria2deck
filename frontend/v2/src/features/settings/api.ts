import { api } from "@/shared/api/client";
import type {
  CredentialInvalidateResponse,
  MachineStatsResponse,
  SettingsResponse,
  SettingsUpdateRequest,
  TrackerStatusResponse,
} from "@/shared/api/types";

/** 凭证作废固定确认串（ACC-11）：管理员必须原样输入才会执行。 */
export const CREDENTIAL_INVALIDATION_CONFIRMATION = "INVALIDATE_ALL_CREDENTIALS";

export function fetchSettings(): Promise<SettingsResponse> {
  return api.get<SettingsResponse>("/api/v2/settings");
}

/** E1 机器磁盘统计（仅管理员）。 */
export function fetchMachineStats(): Promise<MachineStatsResponse> {
  return api.get<MachineStatsResponse>("/api/v2/stats/machine");
}

export function updateSettings(
  changes: SettingsUpdateRequest,
): Promise<SettingsResponse> {
  return api.patch<SettingsResponse>("/api/v2/settings", changes);
}

export function fetchTrackerStatus(): Promise<TrackerStatusResponse> {
  return api.get<TrackerStatusResponse>("/api/v2/trackers");
}

export function refreshTrackers(): Promise<TrackerStatusResponse> {
  return api.post<TrackerStatusResponse>("/api/v2/trackers/refresh");
}

/** aria2 连接测试（CFG-04）：恒 200；connected=false 时无版本/特性，不泄露 URL 或异常。 */
export interface Aria2TestResult {
  connected: boolean;
  version: string | null;
  features: string[];
}

export function testAria2Connection(): Promise<Aria2TestResult> {
  return api.post<Aria2TestResult>("/api/v2/settings/aria2/test");
}

export function invalidateCredentials(
  confirmation: string,
): Promise<CredentialInvalidateResponse> {
  return api.post<CredentialInvalidateResponse>("/api/v2/credentials/invalidate", {
    confirmation,
  });
}
