import { useContext } from "react";

import { AuthContext, type AuthState, ToastContext, type ToastState } from "./contexts";

export function useAuth(): AuthState {
  const context = useContext(AuthContext);
  if (context === null) throw new Error("useAuth 必须在 AuthProvider 内使用");
  return context;
}

export function useToast(): ToastState {
  const context = useContext(ToastContext);
  if (context === null) throw new Error("useToast 必须在 ToastProvider 内使用");
  return context;
}
