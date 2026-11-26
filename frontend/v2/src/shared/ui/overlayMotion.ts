import { gsap } from "gsap";

export interface OverlayInOptions {
  origin?: { x: number; y: number };
  scale?: number;
  x?: number;
  y?: number;
  duration?: number;
  ease?: string;
  onStart?: () => void;
  onComplete?: () => void;
}

export interface OverlayOutOptions {
  origin?: { x: number; y: number };
  scale?: number;
  x?: number;
  y?: number;
  duration?: number;
  ease?: string;
  onComplete?: () => void;
}

export interface OverlayTriggerMotionResult {
  origin: { x: number; y: number };
  x: number;
  y: number;
  scale: number;
}

const ANIMATING_CLASS = "overlay-animating";

export function prefersReducedMotion() {
  return typeof window !== "undefined" &&
    typeof window.matchMedia === "function" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

function canAnimate() {
  return !prefersReducedMotion() &&
    typeof gsap !== "undefined" &&
    typeof gsap?.set === "function" &&
    typeof gsap?.to === "function" &&
    typeof gsap?.killTweensOf === "function";
}

function stopAnimations(el: HTMLElement) {
  if (typeof gsap !== "undefined" && typeof gsap?.killTweensOf === "function") {
    gsap.killTweensOf(el);
  }
  el.classList.remove(ANIMATING_CLASS);
}

export function overlayIn(el: HTMLElement | null, {
  origin,
  scale = 0.86,
  x = 0,
  y = 0,
  duration = 0.32,
  ease = "back.out(1.25)",
  onStart,
  onComplete,
}: OverlayInOptions = {}) {
  if (!el) return;
  if (origin) el.style.transformOrigin = `${origin.x}px ${origin.y}px`;

  stopAnimations(el);
  if (!canAnimate()) {
    el.style.opacity = "";
    el.style.transform = "";
    onComplete?.();
    return;
  }

  el.classList.add(ANIMATING_CLASS);
  gsap.set(el, { opacity: 0, scale, x, y });
  gsap.to(el, {
    opacity: 1,
    scale: 1,
    x: 0,
    y: 0,
    duration,
    ease,
    clearProps: "transform,opacity",
    onStart,
    onComplete: () => {
      el.classList.remove(ANIMATING_CLASS);
      onComplete?.();
    },
  });
}

export function overlayTriggerMotion(
  el: HTMLElement,
  trigger: Element | null | undefined,
): OverlayTriggerMotionResult | null {
  if (!trigger?.getBoundingClientRect) return null;
  const from = trigger.getBoundingClientRect();
  if (!from.width || !from.height) return null;
  const to = el.getBoundingClientRect();
  return {
    origin: { x: to.width / 2, y: to.height / 2 },
    x: from.left + from.width / 2 - (to.left + to.width / 2),
    y: from.top + from.height / 2 - (to.top + to.height / 2),
    scale: Math.max(0.16, Math.min(0.5, from.width / to.width)),
  };
}

export function overlayOut(el: HTMLElement | null, {
  origin,
  scale = 0.94,
  x = 0,
  y,
  duration = 0.14,
  ease = "power1.in",
  onComplete,
}: OverlayOutOptions = {}) {
  if (!el) {
    onComplete?.();
    return;
  }

  if (origin) el.style.transformOrigin = `${origin.x}px ${origin.y}px`;
  stopAnimations(el);
  if (!canAnimate()) {
    onComplete?.();
    return;
  }

  el.classList.add(ANIMATING_CLASS);
  gsap.to(el, {
    opacity: 0,
    scale,
    x,
    ...(y === undefined ? {} : { y }),
    duration,
    ease,
    onComplete: () => {
      el.classList.remove(ANIMATING_CLASS);
      onComplete?.();
    },
  });
}

export function overlayReverseOut(el: HTMLElement | null, {
  origin,
  scale = 0.86,
  x = 0,
  y = 0,
  duration = 0.32,
  ease = "back.out(1.25)",
  onComplete,
}: Omit<OverlayInOptions, "onStart"> = {}) {
  overlayOut(el, {
    origin,
    scale,
    x,
    y,
    duration,
    ease: ease.replace(".out", ".in"),
    onComplete,
  });
}
