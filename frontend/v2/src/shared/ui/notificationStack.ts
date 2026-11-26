import { createContext, useContext } from "react";
import { gsap } from "gsap";

import { prefersReducedMotion } from "@/shared/ui/overlayMotion";

/** 位移小于这个值时视为没动，避免亚像素误差触发无意义的补间。 */
const REFLOW_MIN_DELTA_PX = 0.5;
const REFLOW_DURATION_SECONDS = 0.24;

/** 每个栈项上一次的视口纵坐标。元素卸载后由浏览器回收，不会滞留。 */
const stackTops = new WeakMap<Element, number>();

function stackItems(stack: HTMLElement): HTMLElement[] {
  return Array.from(stack.children).filter(
    (child): child is HTMLElement => child instanceof HTMLElement,
  );
}

/**
 * FLIP：按上一次记录的坐标补间到当前位置。
 * 新出现的元素只记录不补间（它的入场动画由提示自己负责）。
 */
export function animateStackReflow(stack: HTMLElement): void {
  const reduceMotion = prefersReducedMotion();
  for (const item of stackItems(stack)) {
    const top = item.getBoundingClientRect().top;
    const previous = stackTops.get(item);
    stackTops.set(item, top);
    if (previous === undefined || reduceMotion) continue;
    const delta = previous - top;
    if (Math.abs(delta) < REFLOW_MIN_DELTA_PX) continue;
    gsap.fromTo(
      item,
      { y: delta },
      { y: 0, duration: REFLOW_DURATION_SECONDS, ease: "power2.out", clearProps: "transform" },
    );
  }
}

/** 整个栈换宿主（body ↔ 弹窗）后重建坐标基线，避免整栈播放一次跳变。 */
export function resetStackTops(stack: HTMLElement): void {
  for (const item of stackItems(stack)) {
    stackTops.set(item, item.getBoundingClientRect().top);
  }
}

export const NotificationStackContext = createContext<HTMLElement | null>(null);

export function useNotificationStack(): HTMLElement {
  const stack = useContext(NotificationStackContext);
  if (stack === null) {
    throw new Error("通知组件必须在 NotificationStackProvider 内使用");
  }
  return stack;
}
