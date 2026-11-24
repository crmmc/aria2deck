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
} from "@/shared/auth/contexts";
import { ToastHost } from "@/app/ToastHost";
import { api, ApiRequestError, setUnauthorizedHandler } from "@/shared/api/client";
import { setSiteTitle } from "@/shared/lib/siteTitle";
import { ToastProvider } from "@/shared/components/Toast";
import { emitToast } from "@/shared/components/toast-context";
import type { PublicSiteInfoResponse, UserResponse } from "@/shared/api/types";

let nextToastId = 1;

export function AppProviders({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<UserResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [toasts, setToasts] = useState<ToastMessage[]>([]);
  const userRef = useRef<UserResponse | null>(null);
  userRef.current = user;
  const initSeq = useRef(0);
  const mountedRef = useRef(false);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  // 归档参考语义（§5.1）：初始化并行拉站点信息与 me；401 交给路由守卫跳登录，
  // 网络/服务器错误进入 error + 重试界面。
  const initialize = useCallback(async () => {
    const seq = ++initSeq.current;
    setLoading(true);
    setError(null);
    // 站点信息不阻塞登录就绪（归档参考语义：标题后至，认证就绪独立判定）。
    void api
      .get<PublicSiteInfoResponse>("/api/v2/public/site-info")
      .then((info) => {
        if (!mountedRef.current) return;
        setSiteTitle(info.site_title);
        document.title = info.site_title.trim() === "" ? "Aria2Deck" : info.site_title;
      })
      .catch(() => {
        /* 失败保持默认标题 */
      });
    const userPromise = api
      .get<UserResponse>("/api/v2/auth/me")
      .then(
        (me) => ({ ok: true as const, me }),
        (caught: unknown) => ({ ok: false as const, caught }),
      );
    const userResult = await userPromise;
    if (seq !== initSeq.current || !mountedRef.current) return;
    if (userResult.ok) {
      setUser(userResult.me);
      setError(null);
      setLoading(false);
      return;
    }
    const caught = userResult.caught;
    if (caught instanceof ApiRequestError && caught.status === 401) {
      // 未登录/会话过期：清用户，由 RequireAuth 守卫跳转。
      // 归档参考语义：从受保护路由启动时会话过期，登录页仍看到任务加载失败提示；
      // 直接打开 /login 或公开分享页则静默（归档参考 登录页基线无提示）。
      const path = window.location.pathname;
      if (path !== "/login" && !path.startsWith("/s/")) {
        emitToast("加载任务失败: 会话已过期，请重新登录", "error");
      }
      setUser(null);
      setError(null);
    } else if (caught instanceof ApiRequestError && caught.status === 0) {
      setUser(null);
      setError("无法连接服务器，请检查网络连接");
    } else if (caught instanceof ApiRequestError) {
      setUser(null);
      setError(`服务器错误: ${caught.message}`);
    } else {
      setUser(null);
      setError("未知错误");
    }
    setLoading(false);
  }, []);

  useEffect(() => {
    void initialize();
  }, [initialize]);

  const retryAuth = useCallback(() => {
    void initialize();
  }, [initialize]);

  /** 供已登录期间的会话刷新（如横幅重试）：网络瞬断不清用户。 */
  const refresh = useCallback(async () => {
    try {
      const me = await api.get<UserResponse>("/api/v2/auth/me");
      setUser(me);
      setError(null);
    } catch (caught) {
      if (caught instanceof ApiRequestError && caught.status === 0) return;
      if (caught instanceof ApiRequestError && caught.status === 401) {
        setUser(null);
        return;
      }
      setUser(null);
    }
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
    setError(null);
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
    () => ({ user, loading, error, login, logout, refresh, retryAuth }),
    [user, loading, error, login, logout, refresh, retryAuth],
  );

  const dismiss = useCallback((id: number) => {
    setToasts((current) => current.filter((toast) => toast.id !== id));
  }, []);

  const push = useCallback((kind: ToastKind, text: string) => {
    const id = nextToastId++;
    setToasts((current) => [...current, { id, kind, text }]);
  }, []);

  const toastValue = useMemo<ToastState>(
    () => ({ toasts, push, dismiss }),
    [toasts, push, dismiss],
  );

  return (
    <ToastProvider>
      <AuthContext.Provider value={authValue}>
        <ToastContext.Provider value={toastValue}>
          {children}
          <ToastHost />
        </ToastContext.Provider>
      </AuthContext.Provider>
    </ToastProvider>
  );
}
