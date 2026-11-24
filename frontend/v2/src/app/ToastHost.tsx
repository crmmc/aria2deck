import { AnimatedToast } from "@/shared/components/AnimatedToast";
import { ToastPortal } from "@/shared/components/ToastPortal";
import { useToast } from "@/shared/auth/hooks";

export function ToastHost() {
  const { toasts, dismiss } = useToast();
  if (toasts.length === 0) return null;
  return (
    <ToastPortal className="toast-host" role="status" inlineFallback>
      {toasts.map((toast) => (
        <AnimatedToast
          key={toast.id}
          className={`toast toast-${toast.kind}`}
          message={toast.text}
          type={toast.kind}
          duration={5000}
          onDismiss={() => dismiss(toast.id)}
          showClose
        />
      ))}
    </ToastPortal>
  );
}
