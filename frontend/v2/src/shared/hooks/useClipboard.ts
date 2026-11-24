import { useCallback } from "react";

import { useToast } from "@/shared/components/toast-context";
import { copyText } from "@/shared/lib/clipboard";

type CopyOptions = {
  successMessage?: string;
  errorMessage?: string;
  onSuccess?: () => void;
};

/** 归档参考 useClipboard：复制成功/失败以 归档参考 Toast 文案反馈；写用 copyText（含降级路径）。 */
export function useClipboard() {
  const { showToast } = useToast();

  return useCallback(
    async (text: string, options: CopyOptions = {}) => {
      const {
        successMessage = "链接已复制",
        errorMessage = "复制失败",
        onSuccess,
      } = options;
      const copied = await copyText(text);
      if (copied) {
        onSuccess?.();
        showToast(successMessage, "success");
      } else {
        showToast(errorMessage, "error");
      }
      return copied;
    },
    [showToast],
  );
}
