import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

import {
  AuthContext,
  ToastContext,
  type AuthState,
  type ToastKind,
  type ToastMessage,
  type ToastState,
} from "@/app/contexts";
import { ToastHost } from "@/app/ToastHost";
import { api, ApiRequestError, setUnauthorizedHandler } from "@/shared/api/client";
import { setSiteTitle } from "@/shared/lib/siteTitle";
import type { PublicSiteInfoResponse, UserResponse } from "@/shared/api/types";

let nextToastId = 1;

export function AppProviders({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<UserResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [toasts, setToasts] = useState<ToastMessage[]>([]);
  const userRef = useRef<UserResponse | null>(null);
  userRef.current = user;

  const refresh = useCallback(async () => {
    try {
      const me = await api.get<UserResponse>("/api/v2/auth/me");
      setUser(me);
    } catch (caught) {
      // 网络瞬断不误登出（G9）：保留当前会话，横幅由 client 的网络健康信号驱动。
      if (caught instanceof ApiRequestError && caught.status === 0) return;
      setUser(null);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // 站点标题（G1）：公开站点信息拉取一次，失败回退默认标题。
  useEffect(() => {
    let cancelled = false;
    api
      .get<PublicSiteInfoResponse>("/api/v2/public/site-info")
      .then((info) => {
        if (cancelled) return;
        setSiteTitle(info.site_title);
        document.title = info.site_title.trim() === "" ? "Aria2Deck" : info.site_title;
      })
      .catch(() => {
        /* 失败保持默认标题 */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const logout = useCallback(async () => {
    setUser(null);
    try {
      await api.post("/api/v2/auth/logout");
    } catch {
      /* 会话可能已失效，忽略 */
    }
  }, []);

  const login = useCallback(async (username: string, password: string) => {
    const me = await api.post<UserResponse>("/api/v2/auth/login", {
      username,
      password,
    });
    setUser(me);
    return me;
  }, []);

  // 401 统一处理：client 检测到 401 时清除用户（路由守卫随后跳 /login）。
  useEffect(() => {
    setUnauthorizedHandler(() => {
      if (userRef.current !== null) setUser(null);
    });
    return () => setUnauthorizedHandler(null);
  }, []);

  const authValue = useMemo<AuthState>(
    () => ({ user, loading, login, logout, refresh }),
    [user, loading, login, logout, refresh],
  );

  const dismiss = useCallback((id: number) => {
    setToasts((current) => current.filter((toast) => toast.id !== id));
  }, []);

  const push = useCallback(
    (kind: ToastKind, text: string) => {
      const id = nextToastId++;
      setToasts((current) => [...current, { id, kind, text }]);
      window.setTimeout(() => dismiss(id), 5000);
    },
    [dismiss],
  );

  const toastValue = useMemo<ToastState>(
    () => ({ toasts, push, dismiss }),
    [toasts, push, dismiss],
  );

  return (
    <AuthContext.Provider value={authValue}>
      <ToastContext.Provider value={toastValue}>
        {children}
        <ToastHost />
      </ToastContext.Provider>
    </AuthContext.Provider>
  );
}
