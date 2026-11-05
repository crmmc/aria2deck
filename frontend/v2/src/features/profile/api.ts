import { api } from "@/shared/api/client";
import { passwordClientHash } from "@/shared/lib/crypto";
import type { OkResponse, RpcAccessResponse } from "@/shared/api/types";

/** 客户端先派生再提交（ACC-12）：PBKDF2 + 用户名盐的 64 位十六进制，旧/新密码按同一当前用户名派生。 */
export async function changePassword(
  username: string,
  oldPassword: string | null,
  newPassword: string,
): Promise<OkResponse> {
  return api.post<OkResponse>("/api/v2/auth/password", {
    old_password: oldPassword === null ? null : await passwordClientHash(username, oldPassword),
    new_password: await passwordClientHash(username, newPassword),
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
