"use client";

import {
  createContext,
  useContext,
  useState,
  useEffect,
  useCallback,
} from "react";
import { useRouter, usePathname } from "next/navigation";
import { api, authEvents, ApiError } from "./api";
import type { User } from "@/types";

type AuthContextType = {
  user: User | null;
  loading: boolean;
  error: string | null;  // 非 401 错误信息
  siteTitle: string;
  sidebarExpanded: boolean;
  setSidebarExpanded: (expanded: boolean) => void;
  logout: () => Promise<void>;
  refreshUser: () => Promise<void>;
  retryAuth: () => void;  // 重试认证
};

const AuthContext = createContext<AuthContextType | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const { push } = useRouter();
  const pathname = usePathname();
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [sidebarExpanded, setSidebarExpanded] = useState(false);
  const [initialized, setInitialized] = useState(false);
  const [isUnauthorized, setIsUnauthorized] = useState(false);
  const [siteTitle, setSiteTitle] = useState<string>('aria2 控制器');

  const refreshUser = useCallback(async () => {
    try {
      const u = await api.me();
      setUser(u);
      setError(null);
    } catch (err) {
      if (err instanceof ApiError && err.isUnauthorized) {
        setUser(null);
      }
      // 其他错误不清除用户状态
    }
  }, []);

  const retryAuth = useCallback(() => {
    setError(null);
    setIsUnauthorized(false);
    setInitialized(false);
    setLoading(true);
  }, []);

  useEffect(() => {
    if (initialized) return;

    api
      .me()
      .then((u) => {
        setUser(u);
        setError(null);
        setIsUnauthorized(false);
        setInitialized(true);
        // 获取网站标题（无需认证）
        api
          .getSiteInfo()
          .then((info) => {
            setSiteTitle(info.site_title);
            document.title = info.site_title;
          })
          .catch((err: unknown) => {
            console.warn("加载站点标题失败", err);
          });
      })
      .catch((err) => {
        if (err instanceof ApiError) {
          if (err.isUnauthorized) {
            // 401: 未登录或会话过期
            setUser(null);
            setIsUnauthorized(true);
          } else if (err.isNetworkError) {
            // 网络错误: 保留可能的用户状态，显示错误
            setError("无法连接服务器，请检查网络连接");
          } else {
            // 其他服务器错误 (500 等)
            setError(`服务器错误: ${err.message}`);
          }
        } else {
          setError("未知错误");
        }
        setInitialized(true);
      })
      .finally(() => setLoading(false));
  }, [initialized]);

  useEffect(() => {
    // 只有确认是 401 未授权时才跳转登录页
    if (!loading && isUnauthorized && pathname !== "/login" && !pathname.startsWith("/s/")) {
      push("/login");
    }
  }, [loading, isUnauthorized, pathname, push]);

  // 监听 401 错误，自动跳转登录页
  useEffect(() => {
    return authEvents.onUnauthorized(() => {
      setUser(null);
      setIsUnauthorized(true);
      if (pathname !== "/login" && !pathname.startsWith("/s/")) {
        push("/login");
      }
    });
  }, [pathname, push]);

  useEffect(() => {
    if (siteTitle && siteTitle !== "aria2 控制器") {
      document.title = siteTitle;
    }
  }, [pathname, siteTitle]);

  const logout = useCallback(async () => {
    try {
      await api.logout();
    } catch (err) {
      console.error("退出登录请求失败，已执行本地登出", err);
    }
    setUser(null);
    setIsUnauthorized(true);
    push("/login");
  }, [push]);

  return (
    <AuthContext.Provider
      value={{
        user,
        loading,
        error,
        siteTitle,
        sidebarExpanded,
        setSidebarExpanded,
        logout,
        refreshUser,
        retryAuth,
      }}
    >
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
