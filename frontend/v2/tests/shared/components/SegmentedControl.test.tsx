import { fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";

import {
  SegmentedControl,
  type SegmentedOption,
} from "@/shared/components/SegmentedControl";
import { syncSegmentIndicator } from "@/shared/ui/segmentIndicator";

type Filter = "all" | "active" | "failed";

const OPTIONS: readonly SegmentedOption<Filter>[] = [
  { value: "all", label: "当前任务", count: 3 },
  { value: "active", label: "进行中", count: 2 },
  { value: "failed", label: "失败", count: 1 },
];

function Harness({ onChange = jest.fn() }: { onChange?: (value: Filter) => void }) {
  const [value, setValue] = useState<Filter>("all");
  return (
    <SegmentedControl
      ariaLabel="任务筛选"
      options={OPTIONS}
      value={value}
      onChange={(next) => {
        setValue(next);
        onChange(next);
      }}
    />
  );
}

const originalMatchMedia = window.matchMedia;
const originalResizeObserver = Object.getOwnPropertyDescriptor(
  globalThis,
  "ResizeObserver",
);
const originalOffsetDescriptors = {
  offsetWidth: Object.getOwnPropertyDescriptor(HTMLElement.prototype, "offsetWidth"),
  offsetHeight: Object.getOwnPropertyDescriptor(HTMLElement.prototype, "offsetHeight"),
  offsetLeft: Object.getOwnPropertyDescriptor(HTMLElement.prototype, "offsetLeft"),
  offsetTop: Object.getOwnPropertyDescriptor(HTMLElement.prototype, "offsetTop"),
};
const originalRequestAnimationFrame = Object.getOwnPropertyDescriptor(
  window,
  "requestAnimationFrame",
);
const originalCancelAnimationFrame = Object.getOwnPropertyDescriptor(
  window,
  "cancelAnimationFrame",
);

let reducedMotion = false;
let hidden = false;
let lefts: Record<Filter, number>;
let widths: Record<Filter, number>;
let frames: Map<number, FrameRequestCallback>;
let nextFrameId: number;

function flushFrames() {
  const callbacks = [...frames.values()];
  frames.clear();
  callbacks.forEach((callback) => callback(0));
}

function indicator(): HTMLElement {
  return document.querySelector(".segment-indicator") as HTMLElement;
}

beforeEach(() => {
  reducedMotion = false;
  hidden = false;
  lefts = { all: 2, active: 86, failed: 186 };
  widths = { all: 80, active: 96, failed: 72 };
  frames = new Map();
  nextFrameId = 1;

  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    writable: true,
    value: jest.fn(() => ({ matches: reducedMotion })),
  });
  Object.defineProperty(window, "requestAnimationFrame", {
    configurable: true,
    writable: true,
    value: jest.fn((callback: FrameRequestCallback) => {
      const id = nextFrameId++;
      frames.set(id, callback);
      return id;
    }),
  });
  Object.defineProperty(window, "cancelAnimationFrame", {
    configurable: true,
    writable: true,
    value: jest.fn((id: number) => {
      frames.delete(id);
    }),
  });

  Object.defineProperty(HTMLElement.prototype, "offsetWidth", {
    configurable: true,
    get(this: HTMLElement) {
      if (!this.classList.contains("segment")) return 0;
      return hidden ? 0 : widths[this.dataset.value as Filter];
    },
  });
  Object.defineProperty(HTMLElement.prototype, "offsetHeight", {
    configurable: true,
    get: () => 30,
  });
  Object.defineProperty(HTMLElement.prototype, "offsetLeft", {
    configurable: true,
    get(this: HTMLElement) {
      return lefts[this.dataset.value as Filter];
    },
  });
  Object.defineProperty(HTMLElement.prototype, "offsetTop", {
    configurable: true,
    get: () => 2,
  });
});

afterEach(() => {
  jest.restoreAllMocks();
  for (const [key, descriptor] of Object.entries(originalOffsetDescriptors)) {
    if (descriptor) Object.defineProperty(HTMLElement.prototype, key, descriptor);
    else Reflect.deleteProperty(HTMLElement.prototype, key);
  }
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    writable: true,
    value: originalMatchMedia,
  });
  for (const [key, descriptor] of [
    ["ResizeObserver", originalResizeObserver],
  ] as const) {
    if (descriptor) Object.defineProperty(globalThis, key, descriptor);
    else Reflect.deleteProperty(globalThis, key);
  }
  for (const [key, descriptor] of [
    ["requestAnimationFrame", originalRequestAnimationFrame],
    ["cancelAnimationFrame", originalCancelAnimationFrame],
  ] as const) {
    if (descriptor) Object.defineProperty(window, key, descriptor);
    else Reflect.deleteProperty(window, key);
  }
});

describe("SegmentedControl", () => {
  it("首次同步定位且无动画，下一帧恢复过渡", () => {
    render(<Harness />);

    expect(indicator()).toHaveStyle({
      transition: "none",
      width: "80px",
      height: "30px",
      transform: "translate(2px, 2px)",
    });
    expect(screen.getByRole("tab", { name: "当前任务 3" })).toHaveAttribute(
      "aria-selected",
      "true",
    );

    flushFrames();
    expect(indicator().style.transition).toBe("");
  });

  it("切换时通过尺寸与 transform 平滑滑动", () => {
    render(<Harness />);
    flushFrames();

    fireEvent.click(screen.getByRole("tab", { name: "进行中 2" }));

    expect(indicator().style.transition).toBe("");
    expect(indicator()).toHaveStyle({
      width: "96px",
      height: "30px",
      transform: "translate(86px, 2px)",
    });
    expect(screen.getByRole("tab", { name: "进行中 2" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
  });

  it("隐藏时不写 0 尺寸，恢复可见后首次定位仍无动画", () => {
    hidden = true;
    render(<Harness />);
    expect(indicator().getAttribute("style")).toBeNull();

    hidden = false;
    fireEvent(window, new Event("resize"));
    expect(indicator()).toHaveStyle({ transition: "none", width: "80px" });
    flushFrames();

    hidden = true;
    fireEvent.click(screen.getByRole("tab", { name: "失败 1" }));
    expect(indicator()).toHaveStyle({
      width: "80px",
      transform: "translate(2px, 2px)",
    });
    expect(indicator().style.width).not.toBe("0px");
  });

  it("窗口与 ResizeObserver 变化后重新同步位置", () => {
    let triggerObserver: () => void = () => undefined;
    const observe = jest.fn();
    const disconnect = jest.fn();
    class MockResizeObserver {
      constructor(callback: () => void) {
        triggerObserver = callback;
      }
      observe = observe;
      disconnect = disconnect;
    }
    Object.defineProperty(globalThis, "ResizeObserver", {
      configurable: true,
      writable: true,
      value: MockResizeObserver,
    });

    const view = render(<Harness />);
    flushFrames();
    expect(observe).toHaveBeenCalledTimes(4);

    lefts.all = 6;
    widths.all = 88;
    fireEvent(window, new Event("resize"));
    expect(indicator()).toHaveStyle({
      transition: "none",
      width: "88px",
      transform: "translate(6px, 2px)",
    });
    flushFrames();

    lefts.all = 10;
    widths.all = 92;
    triggerObserver();
    expect(indicator()).toHaveStyle({
      transition: "none",
      width: "92px",
      transform: "translate(10px, 2px)",
    });

    view.unmount();
    expect(disconnect).toHaveBeenCalledTimes(1);
  });

  it("卸载时移除 resize 监听、断开观察器并取消待执行帧", () => {
    const disconnect = jest.fn();
    class MockResizeObserver {
      observe = jest.fn();
      disconnect = disconnect;
    }
    Object.defineProperty(globalThis, "ResizeObserver", {
      configurable: true,
      writable: true,
      value: MockResizeObserver,
    });
    const removeListener = jest.spyOn(window, "removeEventListener");

    const view = render(<Harness />);
    view.unmount();

    expect(disconnect).toHaveBeenCalledTimes(1);
    expect(removeListener).toHaveBeenCalledWith("resize", expect.any(Function));
    expect(window.cancelAnimationFrame).toHaveBeenCalledWith(1);
  });

  it("支持方向键、Home/End、循环焦点与 roving tabindex", () => {
    const onChange = jest.fn();
    render(<Harness onChange={onChange} />);
    const tab = (name: string) => screen.getByRole("tab", { name });

    fireEvent.keyDown(tab("当前任务 3"), { key: "ArrowRight" });
    expect(tab("进行中 2")).toHaveFocus();
    expect(tab("进行中 2")).toHaveAttribute("tabindex", "0");
    expect(tab("当前任务 3")).toHaveAttribute("tabindex", "-1");

    fireEvent.keyDown(tab("进行中 2"), { key: "End" });
    expect(tab("失败 1")).toHaveFocus();
    fireEvent.keyDown(tab("失败 1"), { key: "ArrowRight" });
    expect(tab("当前任务 3")).toHaveFocus();
    fireEvent.keyDown(tab("当前任务 3"), { key: "ArrowLeft" });
    expect(tab("失败 1")).toHaveFocus();
    fireEvent.keyDown(tab("失败 1"), { key: "Home" });
    expect(tab("当前任务 3")).toHaveFocus();
    fireEvent.keyDown(tab("当前任务 3"), { key: "ArrowDown" });

    expect(onChange.mock.calls.map(([value]) => value)).toEqual([
      "active",
      "failed",
      "all",
      "failed",
      "all",
    ]);
  });

  it("radio 模式使用单选语义、不显示计数并保留键盘切换", () => {
    const onChange = jest.fn();
    render(
      <SegmentedControl
        ariaLabel="打包格式"
        mode="radio"
        options={OPTIONS}
        value="active"
        onChange={onChange}
      />,
    );

    expect(screen.getByRole("radiogroup", { name: "打包格式" })).toBeInTheDocument();
    const selected = screen.getByRole("radio", { name: "进行中" });
    const unselected = screen.getByRole("radio", { name: "当前任务" });
    expect(selected).toHaveAttribute("aria-checked", "true");
    expect(unselected).toHaveAttribute("aria-checked", "false");
    expect(selected).not.toHaveAttribute("aria-selected");
    expect(selected.querySelector("span")).toBeNull();

    fireEvent.click(screen.getByRole("radio", { name: "失败" }));
    fireEvent.keyDown(selected, { key: "Home" });
    expect(onChange.mock.calls).toEqual([["failed"], ["all"]]);
  });

  it("减少动态效果时切换也不启动滑动或帧回调", () => {
    reducedMotion = true;
    render(<Harness />);

    fireEvent.click(screen.getByRole("tab", { name: "失败 1" }));

    expect(indicator()).toHaveStyle({
      transition: "none",
      width: "72px",
      transform: "translate(186px, 2px)",
    });
    expect(window.requestAnimationFrame).not.toHaveBeenCalled();
  });

  it("缺少指示器或选中项时安全跳过同步", () => {
    const root = document.createElement("div");
    expect(syncSegmentIndicator(root)).toBe(false);

    root.innerHTML = '<span class="segment-indicator"></span>';
    expect(syncSegmentIndicator(root)).toBe(false);
  });
});
