import { api } from "@/shared/api/client";
import { fetchAllPages } from "@/shared/api/pagination";
import { passwordClientHash } from "@/shared/lib/crypto";
import type {
  DeleteUserResponse,
  UserListResponse,
  UserResponse,
} from "@/shared/api/types";

/** 单页上限 100（后端 le=100）；归档参考 无分页界面，数据层顺序聚合全部页（C21）。 */
const USERS_PAGE_SIZE = 100;

export const KIB = 1024;
export const MIB = 1024 ** 2;
export const GIB = 1024 ** 3;

export async function fetchUsersAll(): Promise<UserResponse[]> {
  const users = await fetchAllPages((page) => fetchUsersPage(page, USERS_PAGE_SIZE));
  // 归档参考 后端列表本身就过滤 pending_delete 用户（repositories/auth.py:239-248），V2 不过滤：
  // 在数据层对齐 归档参考，避免把正在后台清理的账号当成可管理用户展示。
  return users.filter((user) => !user.pending_delete);
}

async function fetchUsersPage(page: number, pageSize: number): Promise<UserListResponse> {
  const params = new URLSearchParams({ page: String(page), page_size: String(pageSize) });
  return api.get<UserListResponse>(`/api/v2/users?${params.toString()}`);
}

export async function createUser(input: {
  username: string;
  password: string;
  isAdmin: boolean;
  quotaBytes: number;
}): Promise<UserResponse> {
  return api.post<UserResponse>("/api/v2/users", {
    username: input.username,
    // wire 用目标用户名派生的客户端哈希（与登录侧同形态），不传明文。
    password: await passwordClientHash(input.username, input.password),
    is_admin: input.isAdmin,
    // 归档参考 新建用户一律标记初始密码；V2 默认 false，必须显式传 true。
    is_initial_password: true,
    quota_bytes: input.quotaBytes,
  });
}

export interface UserUpdatePlan {
  /** 提交后的用户名：客户端密码派生用它作盐（归档参考 用「可能已修改」的目标用户名）。 */
  username: string;
  /** 只含真实变化字段（V2 局部更新）。 */
  changes: { username?: string; is_admin?: boolean; quota_bytes?: number };
  /** 新密码明文；null 表示本次不改密码。 */
  password: string | null;
  /** 改密码时是否标记初始密码：管理员改他人为 true，改自己为 false（归档参考 user_service 同规则）。 */
  markInitialPassword: boolean;
}

export async function updateUser(
  userId: number,
  plan: UserUpdatePlan,
): Promise<UserResponse> {
  const payload: Record<string, unknown> = { ...plan.changes };
  if (plan.password !== null) {
    payload.password = await passwordClientHash(plan.username, plan.password);
    payload.is_initial_password = plan.markInitialPassword;
  }
  return api.patch<UserResponse>(`/api/v2/users/${userId}`, payload);
}

export function deleteUser(userId: number): Promise<DeleteUserResponse> {
  return api.del<DeleteUserResponse>(`/api/v2/users/${userId}`);
}

const QUOTA_UNIT_MULTIPLIER: Record<string, number> = { KB: KIB, MB: MIB, GB: GIB };

/** 归档参考表单口径：数值 × 单位倍率，非正数/非法输入返回 null。四舍五入到整字节，V2 契约只收整数。 */
export function parseQuotaBytes(value: string, unit: string): number | null {
  const multiplier = QUOTA_UNIT_MULTIPLIER[unit];
  if (multiplier === undefined) return null;
  const bytes = Number.parseFloat(value) * multiplier;
  return Number.isFinite(bytes) && bytes > 0 ? Math.round(bytes) : null;
}

/** 管理员编辑表单固定使用 GB，保留原始换算字符串，避免小配额被精度截断。 */
export function formatQuotaForForm(bytes: number): { quotaValue: string; quotaUnit: string } {
  return { quotaValue: String(bytes / GIB), quotaUnit: "GB" };
}

/** 配额 GiB ↔ bytes 换算：输入 GiB 值四舍五入到整 bytes；负值/NaN 返回 null。 */
export function gibToBytes(gib: number): number | null {
  if (!Number.isFinite(gib) || gib < 0) return null;
  return Math.round(gib * GIB);
}
