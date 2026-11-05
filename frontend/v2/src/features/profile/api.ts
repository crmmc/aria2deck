import { api } from "@/shared/api/client";
import { sha256Hex } from "@/shared/lib/crypto";
import type { OkResponse, RpcAccessResponse } from "@/shared/api/types";

/** 客户端先哈希再提交（ACC-12）：后端契约只接受 64 位十六进制摘要。 */
export async function changePassword(
  oldPassword: string | null,
  newPassword: string,
): Promise<OkResponse> {
  return api.post<OkResponse>("/api/v2/auth/password", {
    old_password: oldPassword === null ? null : await sha256Hex(oldPassword),
    new_password: await sha256Hex(newPassword),
  });
}

export function fetchRpcAccess(): Promise<RpcAccessResponse> {
  return api.get<RpcAccessResponse>("/api/v2/credentials/rpc");
}

export function setRpcAccess(enabled: boolean): Promise<RpcAccessResponse> {
  return api.put<RpcAccessResponse>("/api/v2/credentials/rpc", { enabled });
}

export function refreshRpcSecret(): Promise<RpcAccessResponse> {
  return api.post<RpcAccessResponse>("/api/v2/credentials/rpc/refresh");
}

/** 外部 RPC 端点地址（/aria2/jsonrpc，与浏览器同源）。 */
export function rpcEndpointUrl(): string {
  return `${window.location.origin}/aria2/jsonrpc`;
}
