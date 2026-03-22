"use client";

import { useEffect } from "react";
import { AuthProvider } from "@/lib/AuthContext";
import { ToastProvider } from "@/components/Toast";

export function Providers({ children }: { children: React.ReactNode }) {
  useEffect(() => {
    const el = document.getElementById("app-loading");
    if (el) {
      el.style.opacity = "0";
      setTimeout(() => el.remove(), 300);
    }
  }, []);

  return (
    <ToastProvider>
      <AuthProvider>{children}</AuthProvider>
    </ToastProvider>
  );
}
