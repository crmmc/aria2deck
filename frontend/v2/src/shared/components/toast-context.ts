/** 归档参考 Toast/确认框上下文（从 Toast.tsx 拆分以满足 react-refresh 单导出约束）。 */

import { createContext, useContext } from "react";

export type ToastType = "success" | "error" | "info" | "warning";

export interface ToastContextType {
  showToast: (message: string, type?: ToastType) => void;
  showConfirm: (options: ConfirmOptions) => Promise<boolean>;
}

export interface ConfirmOptions {
  title?: string;
  message: string;
  confirmText?: string;
  cancelText?: string;
  danger?: boolean;
}

export interface ConfirmState extends ConfirmOptions {
  resolve: (value: boolean) => void;
}

export const ToastContext = createContext<ToastContextType | null>(null);

/**
 * 非 React 上下文（如 providers 初始化）需要弹 toast 时的发射桥：
 * ToastProvider 挂载时绑定实现，归档参考语义（会话 401 也要在登录页看到提示）。
 */
type ToastEmitter = (message: string, type?: ToastType) => void;

let toastEmitter: ToastEmitter | null = null;

export function bindToastEmitter(emitter: ToastEmitter | null): void {
  toastEmitter = emitter;
}

export function emitToast(message: string, type: ToastType = "info"): void {
  toastEmitter?.(message, type);
}

export function useToast() {
  const context = useContext(ToastContext);
  if (!context) {
    throw new Error("useToast must be used within ToastProvider");
  }
  return context;
}
