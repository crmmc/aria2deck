import { useCallback } from "react";

import { useToast } from "@/components/Toast";

type CopyOptions = {
  successMessage?: string;
  errorMessage?: string;
  onSuccess?: () => void;
};

export function useClipboard() {
  const { showToast } = useToast();

  return useCallback(
    async (text: string, options: CopyOptions = {}) => {
      const {
        successMessage = "链接已复制",
        errorMessage = "复制失败",
        onSuccess,
      } = options;
      try {
        await navigator.clipboard.writeText(text);
        onSuccess?.();
        showToast(successMessage, "success");
        return true;
      } catch {
        showToast(errorMessage, "error");
        return false;
      }
    },
    [showToast]
  );
}
