import { useCallback, useEffect, useLayoutEffect, useRef } from "react";
import { gsap } from "gsap";

import { Icon, type IconName } from "@/shared/components/Icon";
import type { ToastType } from "@/shared/components/toast-context";
import { overlayIn, overlayReverseOut } from "@/shared/ui/overlayMotion";

const TOAST_MOTION = {
  x: 16,
  y: 0,
  scale: 0.96,
  duration: 0.24,
  ease: "back.out(1.2)",
};

const TOAST_ICONS: Record<ToastType, IconName> = {
  success: "check",
  error: "x",
  warning: "alert",
  info: "info",
};

type AnimatedToastProps = {
  message: string;
  type: ToastType;
  className: string;
  duration: number;
  onDismiss: () => void;
  showClose?: boolean;
};

export function AnimatedToast({
  message,
  type,
  className,
  duration,
  onDismiss,
  showClose = false,
}: AnimatedToastProps) {
  const toastRef = useRef<HTMLDivElement>(null);
  const onDismissRef = useRef(onDismiss);
  const closingRef = useRef(false);
  onDismissRef.current = onDismiss;

  const dismiss = useCallback(() => {
    if (closingRef.current) return;
    closingRef.current = true;
    overlayReverseOut(toastRef.current, {
      ...TOAST_MOTION,
      onComplete: () => onDismissRef.current(),
    });
  }, []);

  useLayoutEffect(() => {
    const toast = toastRef.current;
    overlayIn(toast, TOAST_MOTION);
    return () => {
      if (!toast) return;
      gsap.killTweensOf(toast);
      toast.classList.remove("overlay-animating");
    };
  }, []);

  useEffect(() => {
    const timer = window.setTimeout(dismiss, duration);
    return () => window.clearTimeout(timer);
  }, [dismiss, duration]);

  return (
    <div ref={toastRef} className={`notification-card ${className}`} role={type === "error" ? "alert" : "status"}>
      <span className="toast-icon"><Icon name={TOAST_ICONS[type]} className="icon-sm" /></span>
      <span className="toast-message">{message}</span>
      {showClose ? (
        <button type="button" className="notification-card-close" aria-label="关闭提示" onClick={dismiss}>
          <Icon name="x" className="icon-sm" />
        </button>
      ) : null}
    </div>
  );
}
