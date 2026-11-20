import { createContext } from "react";

import type { UserResponse } from "@/shared/api/types";

/** Context 定义（组件与 hooks 分离，满足 fast refresh 约束）。 */

export interface AuthState {
  user: UserResponse | null;
  loading: boolean;
  /** 归档参考语义：初始化时网络/服务器错误的可重试失败信息（401 走守卫跳转，不算 error）。 */
  error: string | null;
  login: (username: string, password: string) => Promise<UserResponse>;
  logout: () => Promise<void>;
  refresh: () => Promise<void>;
  /** 归档参考语义：失败后重试初始化（清 error 并重新拉取）。 */
  retryAuth: () => void;
}

export const AuthContext = createContext<AuthState | null>(null);

export type ToastKind = "success" | "error" | "info";

export interface ToastMessage {
  id: number;
  kind: ToastKind;
  text: string;
}

export interface ToastState {
  toasts: ToastMessage[];
  push: (kind: ToastKind, text: string) => void;
  dismiss: (id: number) => void;
}

export const ToastContext = createContext<ToastState | null>(null);
