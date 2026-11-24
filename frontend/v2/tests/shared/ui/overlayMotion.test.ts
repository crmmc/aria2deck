import { gsap } from "gsap";

import {
  overlayIn,
  overlayOut,
  overlayReverseOut,
  overlayTriggerMotion,
} from "@/shared/ui/overlayMotion";

jest.mock("gsap", () => ({
  gsap: {
    killTweensOf: jest.fn(),
    set: jest.fn(),
    to: jest.fn(),
  },
}));

const killMock = jest.mocked(gsap.killTweensOf);
const setMock = jest.mocked(gsap.set);
const toMock = jest.mocked(gsap.to);
const originalMatchMedia = window.matchMedia;

function setReducedMotion(matches: boolean) {
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    writable: true,
    value: jest.fn(() => ({ matches })),
  });
}

function lastToVars(): gsap.TweenVars {
  const call: unknown = toMock.mock.calls.at(-1);
  const vars: unknown = Array.isArray(call) ? call[1] : undefined;
  if (typeof vars !== "object" || vars === null) {
    throw new Error("GSAP to 未收到动画参数");
  }
  return vars as gsap.TweenVars;
}

function setRect(element: Element, rect: { left: number; top: number; width: number; height: number }) {
  element.getBoundingClientRect = () => ({
    ...rect,
    bottom: rect.top + rect.height,
    right: rect.left + rect.width,
    x: rect.left,
    y: rect.top,
    toJSON: () => rect,
  });
}

beforeEach(() => {
  jest.clearAllMocks();
  setReducedMotion(false);
});

afterEach(() => {
  document.body.replaceChildren();
  if (originalMatchMedia) {
    Object.defineProperty(window, "matchMedia", {
      configurable: true,
      writable: true,
      value: originalMatchMedia,
    });
  } else {
    Reflect.deleteProperty(window, "matchMedia");
  }
});

describe("overlayIn", () => {
  it("同步设置初始帧，再按 Demo 默认参数执行回弹入场", () => {
    const el = document.createElement("div");
    const onStart = jest.fn();
    const onComplete = jest.fn(() => {
      expect(el).not.toHaveClass("overlay-animating");
    });

    overlayIn(el, { origin: { x: 120, y: 80 }, onStart, onComplete });

    expect(el.style.transformOrigin).toBe("120px 80px");
    expect(el).toHaveClass("overlay-animating");
    expect(killMock).toHaveBeenCalledWith(el);
    expect(setMock).toHaveBeenCalledWith(el, { opacity: 0, scale: 0.86, x: 0, y: 0 });
    expect(setMock.mock.invocationCallOrder[0]).toBeLessThan(toMock.mock.invocationCallOrder[0]);
    expect(lastToVars()).toMatchObject({
      opacity: 1,
      scale: 1,
      x: 0,
      y: 0,
      duration: 0.32,
      ease: "back.out(1.25)",
      clearProps: "transform,opacity",
      onStart,
    });
    expect(onStart).not.toHaveBeenCalled();
    expect(onComplete).not.toHaveBeenCalled();

    lastToVars().onStart?.();
    expect(onStart).toHaveBeenCalledTimes(1);
    lastToVars().onComplete?.();
    expect(onComplete).toHaveBeenCalledTimes(1);
    expect(el).not.toHaveClass("overlay-animating");
  });

  it("传递自定义位移、缩放、时长和缓动，并支持空元素", () => {
    const el = document.createElement("div");
    const onComplete = jest.fn();

    overlayIn(null, { onComplete });
    expect(onComplete).not.toHaveBeenCalled();
    expect(setMock).not.toHaveBeenCalled();

    overlayIn(el, { scale: 0.96, x: 24, y: -8, duration: 0.26, ease: "back.out(1.12)" });
    expect(setMock).toHaveBeenCalledWith(el, { opacity: 0, scale: 0.96, x: 24, y: -8 });
    expect(lastToVars()).toMatchObject({ duration: 0.26, ease: "back.out(1.12)" });
  });

  it("减少动态效果时立即可见、完成回调且不启动动画", () => {
    const el = document.createElement("div");
    el.classList.add("overlay-animating");
    el.style.opacity = "0";
    el.style.transform = "scale(0.5)";
    const onStart = jest.fn();
    const onComplete = jest.fn();
    setReducedMotion(true);

    overlayIn(el, { onStart, onComplete });

    expect(el.style.opacity).toBe("");
    expect(el.style.transform).toBe("");
    expect(el).not.toHaveClass("overlay-animating");
    expect(onStart).not.toHaveBeenCalled();
    expect(onComplete).toHaveBeenCalledTimes(1);
    expect(killMock).toHaveBeenCalledWith(el);
    expect(setMock).not.toHaveBeenCalled();
    expect(toMock).not.toHaveBeenCalled();
  });

  it("GSAP 不可用时立即可见并完成回调", () => {
    const descriptor = Object.getOwnPropertyDescriptor(gsap, "to");
    expect(descriptor).toBeDefined();
    Object.defineProperty(gsap, "to", { configurable: true, value: undefined });
    const el = document.createElement("div");
    el.style.opacity = "0";
    el.style.transform = "translateX(20px)";
    const onComplete = jest.fn();

    try {
      overlayIn(el, { onComplete });
      expect(el.style.opacity).toBe("");
      expect(el.style.transform).toBe("");
      expect(onComplete).toHaveBeenCalledTimes(1);
      expect(setMock).not.toHaveBeenCalled();
    } finally {
      Object.defineProperty(gsap, "to", descriptor!);
    }
  });

  it("重复调用会停止旧动画，只由新动画负责最终清理", () => {
    const el = document.createElement("div");
    const firstComplete = jest.fn();
    const secondComplete = jest.fn();

    overlayIn(el, { onComplete: firstComplete });
    overlayIn(el, { onComplete: secondComplete });

    expect(killMock).toHaveBeenCalledTimes(2);
    expect(setMock).toHaveBeenCalledTimes(2);
    expect(el).toHaveClass("overlay-animating");
    expect(firstComplete).not.toHaveBeenCalled();

    lastToVars().onComplete?.();
    expect(secondComplete).toHaveBeenCalledTimes(1);
    expect(el).not.toHaveClass("overlay-animating");
  });
});

describe("overlayTriggerMotion", () => {
  it("根据触发器和浮层中心计算位移、原点与缩放", () => {
    const el = document.createElement("div");
    const trigger = document.createElement("button");
    setRect(el, { left: 200, top: 100, width: 400, height: 300 });
    setRect(trigger, { left: 250, top: 180, width: 100, height: 40 });

    expect(overlayTriggerMotion(el, trigger)).toEqual({
      origin: { x: 200, y: 150 },
      x: -100,
      y: -50,
      scale: 0.25,
    });
  });

  it.each([
    { triggerWidth: 8, expectedScale: 0.16 },
    { triggerWidth: 320, expectedScale: 0.5 },
  ])("将缩放限制在 Demo 范围内：$triggerWidth px", ({ triggerWidth, expectedScale }) => {
    const el = document.createElement("div");
    const trigger = document.createElement("button");
    setRect(el, { left: 0, top: 0, width: 400, height: 300 });
    setRect(trigger, { left: 0, top: 0, width: triggerWidth, height: 40 });

    expect(overlayTriggerMotion(el, trigger)?.scale).toBe(expectedScale);
  });

  it("触发器缺失、隐藏或任一尺寸为零时回退为空", () => {
    const el = document.createElement("div");
    const trigger = document.createElement("button");
    const zeroWidth = document.createElement("button");
    const zeroHeight = document.createElement("button");
    setRect(zeroWidth, { left: 0, top: 0, width: 0, height: 40 });
    setRect(zeroHeight, { left: 0, top: 0, width: 40, height: 0 });

    expect(overlayTriggerMotion(el, null)).toBeNull();
    expect(overlayTriggerMotion(el, trigger)).toBeNull();
    expect(overlayTriggerMotion(el, zeroWidth)).toBeNull();
    expect(overlayTriggerMotion(el, zeroHeight)).toBeNull();
  });
});

describe("overlayOut", () => {
  it("按 power1.in 收回，并在卸载回调前清理动画状态", () => {
    const el = document.createElement("div");
    document.body.append(el);
    const onComplete = jest.fn(() => {
      expect(el).not.toHaveClass("overlay-animating");
      el.remove();
    });

    overlayOut(el, { x: 12, onComplete });

    expect(el).toHaveClass("overlay-animating");
    expect(lastToVars()).toMatchObject({
      opacity: 0,
      scale: 0.94,
      x: 12,
      duration: 0.14,
      ease: "power1.in",
    });
    expect(lastToVars()).not.toHaveProperty("y");
    expect(onComplete).not.toHaveBeenCalled();

    lastToVars().onComplete?.();
    expect(onComplete).toHaveBeenCalledTimes(1);
    expect(el.isConnected).toBe(false);
    expect(el).not.toHaveClass("overlay-animating");
  });

  it("按入场初始参数执行完整反向退出", () => {
    const el = document.createElement("div");

    overlayOut(el, {
      origin: { x: 200, y: 150 },
      scale: 0.25,
      x: -100,
      y: -50,
      duration: 0.34,
      ease: "back.in(1.2)",
    });

    expect(el.style.transformOrigin).toBe("200px 150px");
    expect(lastToVars()).toMatchObject({
      opacity: 0,
      scale: 0.25,
      x: -100,
      y: -50,
      duration: 0.34,
      ease: "back.in(1.2)",
    });
  });

  it("减少动态效果时不启动退出动画并立即完成", () => {
    const el = document.createElement("div");
    el.classList.add("overlay-animating");
    const onComplete = jest.fn();
    setReducedMotion(true);

    overlayOut(el, { onComplete });

    expect(onComplete).toHaveBeenCalledTimes(1);
    expect(el).not.toHaveClass("overlay-animating");
    expect(killMock).toHaveBeenCalledWith(el);
    expect(toMock).not.toHaveBeenCalled();
  });

  it("空元素仍执行退出完成回调", () => {
    const onComplete = jest.fn();

    overlayOut(null, { onComplete });

    expect(onComplete).toHaveBeenCalledTimes(1);
    expect(killMock).not.toHaveBeenCalled();
    expect(toMock).not.toHaveBeenCalled();
  });
});

describe("overlayReverseOut", () => {
  it("使用与 overlayIn 相同的默认初始帧和反向缓动", () => {
    const el = document.createElement("div");
    const onComplete = jest.fn();

    overlayReverseOut(el, { onComplete });

    expect(lastToVars()).toMatchObject({
      opacity: 0,
      scale: 0.86,
      x: 0,
      y: 0,
      duration: 0.32,
      ease: "back.in(1.25)",
    });
    expect(onComplete).not.toHaveBeenCalled();
    lastToVars().onComplete?.();
    expect(onComplete).toHaveBeenCalledTimes(1);
    expect(el).not.toHaveClass("overlay-animating");
  });

  it.each([
    { entryEase: "back.out(1.2)", exitEase: "back.in(1.2)" },
    { entryEase: "power2.out", exitEase: "power2.in" },
  ])("保留完整入场参数，把 $entryEase 反向为 $exitEase", ({ entryEase, exitEase }) => {
    const el = document.createElement("div");

    overlayReverseOut(el, {
      origin: { x: 200, y: 150 },
      scale: 0.25,
      x: -100,
      y: -50,
      duration: 0.34,
      ease: entryEase,
    });

    expect(el.style.transformOrigin).toBe("200px 150px");
    expect(lastToVars()).toMatchObject({
      opacity: 0,
      scale: 0.25,
      x: -100,
      y: -50,
      duration: 0.34,
      ease: exitEase,
    });
  });
});
