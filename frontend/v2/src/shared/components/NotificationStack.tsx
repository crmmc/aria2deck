"use client";

import { useLayoutEffect, useState, useSyncExternalStore, type ReactNode } from "react";
import { createPortal } from "react-dom";

import {
  NotificationStackContext,
  resetStackTops,
  useNotificationStack,
  animateStackReflow,
} from "@/shared/ui/notificationStack";
import { getTopModalDialog, subscribeToModalLayers } from "@/shared/ui/modalLayers";

type NotificationStackProviderProps = {
  children: ReactNode;
};

/**
 * 唯一的右上角通知栈：常驻横幅与临时 toast 都挂在同一个纵向容器里，
 * 因此彼此不会覆盖。栈整体跟随最上层 <dialog>，保证模态期间仍可读。
 */
export function NotificationStackProvider({ children }: NotificationStackProviderProps) {
  const topDialog = useSyncExternalStore(subscribeToModalLayers, getTopModalDialog);
  const [stack] = useState(() => {
    const element = document.createElement("div");
    element.className = "notification-stack";
    element.setAttribute("aria-live", "polite");
    return element;
  });

  useLayoutEffect(() => {
    const target: Element = topDialog ?? document.body;
    if (stack.parentElement === target) return;
    const activeElement = document.activeElement;
    const focused = activeElement instanceof HTMLElement && stack.contains(activeElement)
      ? activeElement
      : null;
    target.appendChild(stack);
    resetStackTops(stack);
    focused?.focus();
  }, [stack, topDialog]);

  useLayoutEffect(() => () => stack.remove(), [stack]);

  return (
    <NotificationStackContext.Provider value={stack}>
      {children}
    </NotificationStackContext.Provider>
  );
}

type NotificationStackItemProps = {
  /** 越小越靠上；同值按挂载顺序排列。 */
  order: number;
  role?: "status";
  children: ReactNode;
};

/** 单条通知的包装层：承载顺序与补间位移，隔离提示自身的入场 transform。 */
export function NotificationStackItem({ order, role, children }: NotificationStackItemProps) {
  const stack = useNotificationStack();
  const [item] = useState(() => {
    const element = document.createElement("div");
    element.className = "notification-stack-item";
    return element;
  });

  useLayoutEffect(() => {
    item.style.order = String(order);
    if (role) item.setAttribute("role", role);
    else item.removeAttribute("role");
  }, [item, order, role]);

  useLayoutEffect(() => {
    stack.appendChild(item);
    animateStackReflow(stack);
    return () => {
      item.remove();
      animateStackReflow(stack);
    };
  }, [stack, item]);

  return createPortal(children, item);
}
