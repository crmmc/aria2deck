import { AnimatedToast } from "@/shared/components/AnimatedToast";
import { NotificationStackItem } from "@/shared/components/NotificationStack";
import { useToast } from "@/shared/auth/hooks";

/** 同时可见上限：超出的排队，等有空位再入场（此时才开始计时）。 */
const MAX_VISIBLE_TOASTS = 5;

export function ToastHost() {
  const { toasts, dismiss } = useToast();
  const visibleToasts = toasts.slice(0, MAX_VISIBLE_TOASTS);
  return (
    <>
      {visibleToasts.map((toast, index) => (
        <NotificationStackItem
          key={toast.id}
          order={1000 + visibleToasts.length - 1 - index}
          role="status"
        >
          <AnimatedToast
            className={`toast toast-${toast.kind}`}
            message={toast.text}
            type={toast.kind}
            duration={5000}
            onDismiss={() => dismiss(toast.id)}
            showClose
          />
        </NotificationStackItem>
      ))}
    </>
  );
}
