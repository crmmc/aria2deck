import { api } from "@/shared/api/client";
import { passwordClientHash } from "@/shared/lib/crypto";
import type {
  DeleteUserResponse,
  UserListResponse,
  UserResponse,
} from "@/shared/api/types";

export const GIB = 1024 ** 3;

export function fetchUsers(page: number, pageSize: number): Promise<UserListResponse> {
  const params = new URLSearchParams({
    page: String(page),
    page_size: String(pageSize),
  });
  return api.get<UserListResponse>(`/api/v2/users?${params.toString()}`);
}

export async function createUser(input: {
  username: string;
  password: string;
  isAdmin: boolean;
  isInitialPassword: boolean;
  quotaBytes: number | null;
}): Promise<UserResponse> {
  return api.post<UserResponse>("/api/v2/users", {
    username: input.username,
    // wire 用目标用户名派生的客户端哈希（与登录侧同形态），不传明文。
    password: await passwordClientHash(input.username, input.password),
    is_admin: input.isAdmin,
    is_initial_password: input.isInitialPassword,
    quota_bytes: input.quotaBytes,
  });
}

export async function updateUser(
  userId: number,
  changes: {
    username?: string;
    is_admin?: boolean;
    is_initial_password?: boolean;
    quota_bytes?: number;
    /** 重置密码：以目标用户名派生的客户端哈希提交（U3），留空则不修改。 */
    passwordHash?: string;
  },
): Promise<UserResponse> {
  const payload: Record<string, unknown> = { ...changes };
  if (changes.passwordHash !== undefined) payload.password = changes.passwordHash;
  delete payload.passwordHash;
  return api.patch<UserResponse>(`/api/v2/users/${userId}`, payload);
}

export function deleteUser(userId: number): Promise<DeleteUserResponse> {
  return api.del<DeleteUserResponse>(`/api/v2/users/${userId}`);
}

/** 配额 GiB ↔ bytes 换算：输入 GiB 值四舍五入到整 bytes；负值/NaN 返回 null。 */
export function gibToBytes(gib: number): number | null {
  if (!Number.isFinite(gib) || gib < 0) return null;
  return Math.round(gib * GIB);
}

export function bytesToGib(bytes: number): number {
  return Math.round((bytes / GIB) * 100) / 100;
}
