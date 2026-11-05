import { createContext } from "react";

import type { UserResponse } from "@/shared/api/types";

/** Context 定义（组件与 hooks 分离，满足 fast refresh 约束）。 */

export interface AuthState {
  user: UserResponse | null;
  loading: boolean;
  login: (username: string, password: string) => Promise<UserResponse>;
  logout: () => Promise<void>;
  refresh: () => Promise<void>;
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
