import { act, fireEvent, render, screen } from "@testing-library/react";
import { gsap } from "gsap";

import { AnimatedToast } from "@/shared/components/AnimatedToast";

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
  if (typeof vars !== "object" || vars === null) throw new Error("GSAP to 未收到动画参数");
  return vars as gsap.TweenVars;
}

function completeExit() {
  act(() => lastToVars().onComplete?.());
}

describe("AnimatedToast", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    jest.useFakeTimers();
    setReducedMotion(false);
  });

  afterEach(() => {
    jest.useRealTimers();
    Object.defineProperty(window, "matchMedia", {
      configurable: true,
      writable: true,
      value: originalMatchMedia,
    });
  });

  it("sets the first frame, starts auto exit at the exact duration, then dismisses once", () => {
    const onDismiss = jest.fn();
    render(
      <AnimatedToast
        message="已保存"
        type="success"
        className="toast-item toast-success"
        duration={3000}
        onDismiss={onDismiss}
      />,
    );
    const toast = screen.getByRole("status");
    expect(setMock).toHaveBeenCalledWith(toast, { opacity: 0, scale: 0.96, x: 16, y: 0 });
    expect(lastToVars()).toMatchObject({ duration: 0.24, ease: "back.out(1.2)" });
    expect(toast.querySelector('[data-icon="check"]')).toBeInTheDocument();

    act(() => jest.advanceTimersByTime(2999));
    expect(toMock).toHaveBeenCalledTimes(1);
    expect(onDismiss).not.toHaveBeenCalled();
    act(() => jest.advanceTimersByTime(1));
    expect(lastToVars()).toMatchObject({
      opacity: 0,
      x: 16,
      y: 0,
      scale: 0.96,
      duration: 0.24,
      ease: "back.in(1.2)",
    });
    expect(onDismiss).not.toHaveBeenCalled();

    completeExit();
    expect(onDismiss).toHaveBeenCalledTimes(1);
    act(() => jest.advanceTimersByTime(6000));
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it("manual close shares the same exit and ignores duplicate clicks", () => {
    const onDismiss = jest.fn();
    render(
      <AnimatedToast
        message="保存失败"
        type="error"
        className="toast toast-error"
        duration={5000}
        onDismiss={onDismiss}
        showClose
      />,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("保存失败");
    const close = screen.getByRole("button", { name: "关闭提示" });
    fireEvent.click(close);
    const exitCount = toMock.mock.calls.length;
    expect(onDismiss).not.toHaveBeenCalled();
    fireEvent.click(close);
    expect(toMock).toHaveBeenCalledTimes(exitCount);
    act(() => jest.advanceTimersByTime(5000));
    expect(toMock).toHaveBeenCalledTimes(exitCount);

    completeExit();
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it("cleans its timer and animation on unmount without a late dismiss", () => {
    const onDismiss = jest.fn();
    const { unmount } = render(
      <AnimatedToast
        message="临时提示"
        type="info"
        className="toast-item toast-info"
        duration={3000}
        onDismiss={onDismiss}
      />,
    );
    const toast = screen.getByRole("status");
    unmount();
    expect(killMock).toHaveBeenCalledWith(toast);
    expect(toast).not.toHaveClass("overlay-animating");
    act(() => jest.advanceTimersByTime(10000));
    expect(onDismiss).not.toHaveBeenCalled();
    expect(toMock).toHaveBeenCalledTimes(1);
  });

  it("reduced motion skips both animations but preserves auto dismiss timing", () => {
    setReducedMotion(true);
    const onDismiss = jest.fn();
    render(
      <AnimatedToast
        message="减少动画"
        type="warning"
        className="toast-item toast-warning"
        duration={3000}
        onDismiss={onDismiss}
      />,
    );
    expect(setMock).not.toHaveBeenCalled();
    expect(toMock).not.toHaveBeenCalled();
    act(() => jest.advanceTimersByTime(3000));
    expect(onDismiss).toHaveBeenCalledTimes(1);
    expect(toMock).not.toHaveBeenCalled();
  });
});
