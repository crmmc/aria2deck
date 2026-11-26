import { act, render, screen } from "@testing-library/react";
import { gsap } from "gsap";

import { animateStackReflow, resetStackTops } from "@/shared/ui/notificationStack";
import {
  NotificationStackItem,
  NotificationStackProvider,
} from "@/shared/components/NotificationStack";
import { setModalDialogOpen } from "@/shared/ui/modalLayers";

function createStack(tops: Record<string, number>): HTMLElement {
  const stack = document.createElement("div");
  stack.className = "notification-stack";
  for (const [name, top] of Object.entries(tops)) {
    const item = document.createElement("div");
    item.className = "notification-stack-item";
    item.dataset.name = name;
    const currentTop = top;
    item.getBoundingClientRect = () => ({ top: currentTop }) as DOMRect;
    stack.appendChild(item);
  }
  document.body.appendChild(stack);
  return stack;
}

function setTop(stack: HTMLElement, name: string, top: number) {
  const item = stack.querySelector<HTMLElement>(`[data-name="${name}"]`);
  if (item === null) throw new Error(`缺少栈项 ${name}`);
  item.getBoundingClientRect = () => ({ top }) as DOMRect;
}

function allowMotion() {
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    writable: true,
    value: jest.fn((query: string) => ({
      matches: false,
      media: query,
    })),
  });
}

describe("animateStackReflow", () => {
  let fromToSpy: jest.SpyInstance;

  beforeEach(() => {
    allowMotion();
    fromToSpy = jest.spyOn(gsap, "fromTo").mockImplementation(() => ({}) as never);
    document.body.innerHTML = "";
  });

  afterEach(() => {
    fromToSpy.mockRestore();
  });

  it("把移动过的元素从旧位置补间到新位置", () => {
    const stack = createStack({ a: 100, b: 200 });
    animateStackReflow(stack);

    setTop(stack, "a", 60);
    setTop(stack, "b", 160);
    animateStackReflow(stack);

    expect(fromToSpy).toHaveBeenCalledTimes(2);
    const firstArgs = fromToSpy.mock.calls[0];
    expect(firstArgs[1]).toEqual({ y: 40 });
    expect(firstArgs[2]).toMatchObject({ y: 0, duration: 0.24 });
  });

  it("新挂载的元素只记录位置，不做补间", () => {
    const stack = createStack({ a: 100 });
    animateStackReflow(stack);

    setTop(stack, "a", 60);
    const added = document.createElement("div");
    added.className = "notification-stack-item";
    added.getBoundingClientRect = () => ({ top: 160 }) as DOMRect;
    stack.appendChild(added);
    animateStackReflow(stack);

    expect(fromToSpy).toHaveBeenCalledTimes(1);
    expect(fromToSpy.mock.calls[0][0]).toBe(stack.querySelector('[data-name="a"]'));
  });

  it("位移小于半像素时不补间", () => {
    const stack = createStack({ a: 100 });
    animateStackReflow(stack);

    setTop(stack, "a", 100.2);
    animateStackReflow(stack);

    expect(fromToSpy).not.toHaveBeenCalled();
  });

  it("减少动效时只更新坐标，不做补间", () => {
    Object.defineProperty(window, "matchMedia", {
      configurable: true,
      writable: true,
      value: jest.fn((query: string) => ({
        matches: query === "(prefers-reduced-motion: reduce)",
        media: query,
      })),
    });
    const stack = createStack({ a: 100 });
    animateStackReflow(stack);

    setTop(stack, "a", 40);
    animateStackReflow(stack);

    expect(fromToSpy).not.toHaveBeenCalled();
  });

  it("resetStackTops 重设基线后不再补间", () => {
    const stack = createStack({ a: 100 });
    animateStackReflow(stack);

    setTop(stack, "a", 40);
    resetStackTops(stack);
    animateStackReflow(stack);

    expect(fromToSpy).not.toHaveBeenCalled();
  });
});

describe("NotificationStackProvider", () => {
  beforeEach(() => {
    document.body.innerHTML = "";
  });

  function renderStack() {
    return render(
      <NotificationStackProvider>
        <span>页面内容</span>
      </NotificationStackProvider>,
    );
  }

  it("无弹窗时栈挂在 document.body 上", () => {
    renderStack();
    const stack = document.querySelector(".notification-stack");
    expect(stack).not.toBeNull();
    expect(stack?.parentElement).toBe(document.body);
  });

  it("弹窗打开时整条栈搬进对话框，关闭后回到 body", () => {
    renderStack();
    const stack = document.querySelector(".notification-stack");
    const dialog = document.createElement("dialog");
    dialog.setAttribute("open", "");
    document.body.appendChild(dialog);

    act(() => {
      setModalDialogOpen(dialog, true);
    });
    expect(stack?.parentElement).toBe(dialog);

    act(() => {
      setModalDialogOpen(dialog, false);
    });
    expect(stack?.parentElement).toBe(document.body);
    dialog.remove();
  });

  it("搬迁后栈内已聚焦的元素重新获得焦点", () => {
    function Harness() {
      return (
        <NotificationStackProvider>
          <NotificationStackItem order={1}>
            <button type="button">栈内按钮</button>
          </NotificationStackItem>
        </NotificationStackProvider>
      );
    }
    render(<Harness />);
    const button = screen.getByRole("button", { name: "栈内按钮" });
    button.focus();
    expect(button).toHaveFocus();

    const dialog = document.createElement("dialog");
    dialog.setAttribute("open", "");
    document.body.appendChild(dialog);
    act(() => {
      setModalDialogOpen(dialog, true);
    });

    expect(button).toHaveFocus();
    act(() => {
      setModalDialogOpen(dialog, false);
    });
    dialog.remove();
  });

  it("栈项携带排序值与 role，卸载后从栈中移除", () => {
    function Harness({ order }: { order: number }) {
      return (
        <NotificationStackProvider>
          <NotificationStackItem order={order} role="status">
            <span>第 {order} 条</span>
          </NotificationStackItem>
        </NotificationStackProvider>
      );
    }
    const view = render(<Harness order={3} />);
    const item = document.querySelector(".notification-stack-item");
    expect(item).toHaveAttribute("role", "status");
    expect((item as HTMLElement).style.order).toBe("3");

    view.rerender(<Harness order={1} />);
    expect((document.querySelector(".notification-stack-item") as HTMLElement).style.order).toBe("1");

    view.unmount();
    expect(document.querySelectorAll(".notification-stack-item")).toHaveLength(0);
  });

  it("栈外的通知组件会明确报错", () => {
    const consoleError = jest.spyOn(console, "error").mockImplementation(() => {});
    function Broken() {
      return (
        <NotificationStackItem order={0}>
          <span>孤立提示</span>
        </NotificationStackItem>
      );
    }
    expect(() => render(<Broken />)).toThrow("通知组件必须在 NotificationStackProvider 内使用");
    consoleError.mockRestore();
  });
});
