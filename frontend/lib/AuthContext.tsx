"use client";

import {
  createContext,
  useContext,
  useState,
  useEffect,
  useCallback,
  useMemo,
  useRef,
} from "react";
import { useRouter, usePathname } from "next/navigation";
import { api, authEvents, ApiError } from "./api";
import type { User } from "@/types";

type AuthContextType = {
  user: User | null;
  loading: boolean;
  error: string | null;
  siteTitle: string;
  logout: () => Promise<void>;
  refreshUser: () => Promise<void>;
  retryAuth: () => void;
};

const AuthContext = createContext<AuthContextType | null>(null);
const DEFAULT_SITE_TITLE = "aria2 控制器";

type AuthState = {
  user: User | null;
  loading: boolean;
  error: string | null;
  siteTitle: string;
};

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const { push } = useRouter();
  const pathname = usePathname();
  const [authState, setAuthState] = useState<AuthState>({
    user: null,
    loading: true,
    error: null,
    siteTitle: DEFAULT_SITE_TITLE,
  });
  const initializedRef = useRef(false);
  const mountedRef = useRef(false);
  const pathnameRef = useRef(pathname);

  useEffect(() => {
    pathnameRef.current = pathname;
  }, [pathname]);

  const shouldRedirectToLogin = useCallback(
    () => pathnameRef.current !== "/login" && !pathnameRef.current.startsWith("/s/"),
    []
  );

  const refreshUser = useCallback(async () => {
    try {
      const u = await api.me();
      setAuthState((prev) => ({ ...prev, user: u, error: null }));
    } catch (err) {
      if (err instanceof ApiError && err.isUnauthorized) {
        setAuthState((prev) => ({ ...prev, user: null }));
      }
      // 其他错误不清除用户状态
    }
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
    };
  }, []);

  const initializeAuth = useCallback(async () => {
    if (initializedRef.current) return;
    if (!mountedRef.current) return;
    initializedRef.current = true;

    const siteInfoPromise = api
      .getSiteInfo()
      .then((info) => info.site_title)
      .catch((err: unknown) => {
        console.warn("加载站点标题失败", err);
        return DEFAULT_SITE_TITLE;
      });
    const userPromise = api.me();

    const [siteTitle, userResult] = await Promise.all([
      siteInfoPromise,
      userPromise.then(
        (u) => ({ ok: true as const, user: u }),
        (err: unknown) => ({ ok: false as const, err })
      ),
    ]);

    if (mountedRef.current) {
      if (siteTitle !== DEFAULT_SITE_TITLE) {
        document.title = siteTitle;
      }

      if (userResult.ok) {
        setAuthState({
          user: userResult.user,
          error: null,
          loading: false,
          siteTitle,
        });
      } else {
        let nextError: string | null = null;
        const err = userResult.err;
        if (err instanceof ApiError) {
          if (err.isUnauthorized) {
            // 401: 未登录或会话过期
            if (shouldRedirectToLogin()) {
              push("/login");
            }
          } else if (err.isNetworkError) {
            // 网络错误: 保留可能的用户状态，显示错误
            nextError = "无法连接服务器，请检查网络连接";
          } else {
            // 其他服务器错误 (500 等)
            nextError = `服务器错误: ${err.message}`;
          }
        } else {
          nextError = "未知错误";
        }

        setAuthState({
          user: null,
          error: nextError,
          loading: false,
          siteTitle,
        });
      }
    }
  }, [push, shouldRedirectToLogin]);

  const retryAuth = useCallback(() => {
    initializedRef.current = false;
    setAuthState((prev) => ({ ...prev, error: null, loading: true }));
    void initializeAuth();
  }, [initializeAuth]);

  useEffect(() => {
    void initializeAuth();
  }, [initializeAuth]);

  // 监听 401 错误，自动跳转登录页
  useEffect(() => {
    return authEvents.onUnauthorized(() => {
      setAuthState((prev) => ({ ...prev, user: null }));
      if (shouldRedirectToLogin()) {
        push("/login");
      }
    });
  }, [push, shouldRedirectToLogin]);

  useEffect(() => {
    if (authState.siteTitle && authState.siteTitle !== DEFAULT_SITE_TITLE) {
      document.title = authState.siteTitle;
    }
  }, [pathname, authState.siteTitle]);

  const logout = useCallback(async () => {
    try {
      await api.logout();
    } catch (err) {
      console.error("退出登录请求失败，已执行本地登出", err);
    }
    setAuthState((prev) => ({ ...prev, user: null }));
    push("/login");
  }, [push]);

  const contextValue = useMemo(
    () => ({
      user: authState.user,
      loading: authState.loading,
      error: authState.error,
      siteTitle: authState.siteTitle,
      logout,
      refreshUser,
      retryAuth,
    }),
    [
      authState,
      logout,
      refreshUser,
      retryAuth,
    ]
  );

  return (
    <AuthContext.Provider value={contextValue}>
      {children}
    </AuthContext.Provider>
  );
}

export function useAuth() {
  const context = useContext(AuthContext);
  if (!context) {
    throw new Error("useAuth 必须在 AuthProvider 内使用");
  }
  return context;
}
