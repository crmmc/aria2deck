"use client";

import {
  createContext,
  useContext,
  useState,
  useEffect,
  useCallback,
} from "react";
import { useRouter, usePathname } from "next/navigation";
import { api, authEvents } from "./api";
import type { User } from "@/types";

type AuthContextType = {
  user: User | null;
  loading: boolean;
  sidebarExpanded: boolean;
  setSidebarExpanded: (expanded: boolean) => void;
  logout: () => Promise<void>;
  refreshUser: () => Promise<void>;
};

const AuthContext = createContext<AuthContextType | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);
  const [sidebarExpanded, setSidebarExpanded] = useState(false);
  const [initialized, setInitialized] = useState(false);

  const refreshUser = useCallback(async () => {
    try {
      const u = await api.me();
      setUser(u);
    } catch {
      setUser(null);
    }
  }, []);

  useEffect(() => {
    if (initialized) return;

    api
      .me()
      .then((u) => {
        setUser(u);
        setInitialized(true);
      })
      .catch(() => {
        setUser(null);
        setInitialized(true);
      })
      .finally(() => setLoading(false));
  }, [initialized]);

  useEffect(() => {
    if (!loading && !user && pathname !== "/login") {
      router.push("/login");
    }
  }, [loading, user, pathname, router]);

  // 监听 401 错误，自动跳转登录页
  useEffect(() => {
    return authEvents.onUnauthorized(() => {
      setUser(null);
      if (pathname !== "/login") {
        router.push("/login");
      }
    });
  }, [pathname, router]);

  const logout = useCallback(async () => {
    await api.logout();
    setUser(null);
    router.push("/login");
  }, [router]);

  return (
    <AuthContext.Provider
      value={{
        user,
        loading,
        sidebarExpanded,
        setSidebarExpanded,
        logout,
        refreshUser,
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
