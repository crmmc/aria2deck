import { useLayoutEffect, useRef, useState, useSyncExternalStore, type ReactNode } from "react";
import { createPortal } from "react-dom";

import { getTopModalDialog, subscribeToModalLayers } from "@/shared/ui/modalLayers";

type ToastPortalProps = {
  children: ReactNode;
  className: string;
  role?: "status";
  inlineFallback?: boolean;
};

export function ToastPortal({ children, className, role, inlineFallback = false }: ToastPortalProps) {
  const fallbackRef = useRef<HTMLSpanElement>(null);
  const topDialog = useSyncExternalStore(subscribeToModalLayers, getTopModalDialog);
  const [container] = useState(() => {
    const element = document.createElement("div");
    element.className = className;
    element.setAttribute("aria-live", "polite");
    if (role) element.setAttribute("role", role);
    return element;
  });

  useLayoutEffect(() => {
    const activeElement = document.activeElement;
    const focusedElement = activeElement instanceof HTMLElement && container.contains(activeElement)
      ? activeElement
      : null;
    if (topDialog) {
      topDialog.appendChild(container);
      focusedElement?.focus();
      return;
    }
    const fallback = fallbackRef.current;
    if (inlineFallback) {
      if (!fallback?.parentNode) return;
      fallback.parentNode.insertBefore(container, fallback);
      focusedElement?.focus();
      return;
    }
    document.body.appendChild(container);
    focusedElement?.focus();
  }, [container, inlineFallback, topDialog]);

  useLayoutEffect(() => () => container.remove(), [container]);

  return (
    <>
      {inlineFallback ? <span ref={fallbackRef} hidden /> : null}
      {createPortal(children, container)}
    </>
  );
}
