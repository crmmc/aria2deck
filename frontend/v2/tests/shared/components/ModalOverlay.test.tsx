import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { gsap } from "gsap";

import { ModalOverlay } from "@/shared/components/ModalOverlay";
import { requestModalClose } from "@/shared/ui/modalClose";

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

describe("ModalOverlay", () => {
  let originalShowModal: HTMLDialogElement["showModal"] | undefined;
  let originalClose: HTMLDialogElement["close"] | undefined;
  let showModalMock: jest.Mock<void, []>;
  let closeMock: jest.Mock<void, []>;

  beforeEach(() => {
    jest.clearAllMocks();
    setReducedMotion(false);
    originalShowModal = HTMLDialogElement.prototype.showModal;
    originalClose = HTMLDialogElement.prototype.close;
    showModalMock = jest.fn(function showModal(this: HTMLDialogElement) {
      this.setAttribute("open", "");
    });
    closeMock = jest.fn(function close(this: HTMLDialogElement) {
      this.removeAttribute("open");
    });
    Object.defineProperty(HTMLDialogElement.prototype, "showModal", {
      configurable: true,
      value: showModalMock,
    });
    Object.defineProperty(HTMLDialogElement.prototype, "close", {
      configurable: true,
      value: closeMock,
    });
  });

  afterEach(() => {
    document.querySelectorAll("dialog").forEach((dialog) => dialog.removeAttribute("open"));
    if (originalShowModal) {
      Object.defineProperty(HTMLDialogElement.prototype, "showModal", {
        configurable: true,
        value: originalShowModal,
      });
    } else {
      Reflect.deleteProperty(HTMLDialogElement.prototype, "showModal");
    }
    if (originalClose) {
      Object.defineProperty(HTMLDialogElement.prototype, "close", {
        configurable: true,
        value: originalClose,
      });
    } else {
      Reflect.deleteProperty(HTMLDialogElement.prototype, "close");
    }
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

  it("opens a native modal, focuses the first control and sets the initial frame before entering", () => {
    const { unmount } = render(
      <ModalOverlay onClose={jest.fn()} ariaLabel="测试弹窗" contentClassName="modal-content">
        <button type="button">Inside</button>
      </ModalOverlay>,
    );

    const dialog = screen.getByRole("dialog", { name: "测试弹窗" });
    const content = document.querySelector(".modal-content");
    expect(dialog.tagName).toBe("DIALOG");
    expect(dialog).toHaveAttribute("aria-modal", "true");
    expect(dialog.style.opacity).toBe("");
    expect(dialog.querySelector(".modal-backdrop-button")).toBeInTheDocument();
    expect(showModalMock).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Inside" })).toHaveFocus();
    expect(setMock).toHaveBeenCalledWith(content, { opacity: 0, scale: 0.96, x: 0, y: 0 });
    expect(setMock.mock.invocationCallOrder[0]).toBeLessThan(toMock.mock.invocationCallOrder[0]);
    expect(lastToVars()).toMatchObject({ duration: 0.26, ease: "back.out(1.15)" });

    unmount();
    expect(killMock).toHaveBeenCalledWith(content);
    expect(closeMock).toHaveBeenCalledTimes(1);
  });

  it("command hides the dialog before showModal, reveals it on the next frame and omits the backdrop button", () => {
    let opacityAtShowModal = "";
    showModalMock.mockImplementation(function showCommandModal(this: HTMLDialogElement) {
      opacityAtShowModal = this.style.opacity;
      this.setAttribute("open", "");
    });
    const revealFrames: FrameRequestCallback[] = [];
    const requestFrameSpy = jest.spyOn(window, "requestAnimationFrame").mockImplementation((callback) => {
      revealFrames.push(callback);
      return 17;
    });
    const cancelFrameSpy = jest.spyOn(window, "cancelAnimationFrame").mockImplementation(() => undefined);
    const onClose = jest.fn();
    try {
      const { unmount } = render(
        <ModalOverlay
          onClose={onClose}
          ariaLabel="快捷操作"
          motionVariant="command"
          contentClassName="command-shell"
        >
          <button type="button">Inside</button>
        </ModalOverlay>,
      );
      const dialog = screen.getByRole("dialog", { name: "快捷操作" });
      expect(opacityAtShowModal).toBe("0");
      expect(dialog.style.opacity).toBe("0");
      expect(dialog.querySelector(".modal-backdrop-button")).toBeNull();
      expect(dialog.firstElementChild).toHaveClass("command-shell");
      expect(screen.getByRole("button", { name: "Inside" })).toHaveFocus();
      expect(requestFrameSpy).toHaveBeenCalledTimes(1);
      expect(revealFrames).toHaveLength(1);

      act(() => revealFrames[0](0));
      expect(dialog.style.opacity).toBe("1");
      fireEvent(dialog, new Event("cancel", { bubbles: false, cancelable: true }));
      expect(lastToVars()).toMatchObject({
        opacity: 0,
        scale: 0.86,
        x: 0,
        y: 0,
        duration: 0.34,
        ease: "back.in(1.2)",
      });
      completeExit();
      expect(onClose).toHaveBeenCalledTimes(1);
      expect(dialog.style.opacity).toBe("");
      unmount();
    } finally {
      requestFrameSpy.mockRestore();
      cancelFrameSpy.mockRestore();
    }
  });

  it("command cancels a pending reveal and clears hidden opacity on unmount", () => {
    const requestFrameSpy = jest.spyOn(window, "requestAnimationFrame").mockImplementation(() => 23);
    const cancelFrameSpy = jest.spyOn(window, "cancelAnimationFrame").mockImplementation(() => undefined);
    try {
      const { unmount } = render(
        <ModalOverlay onClose={jest.fn()} ariaLabel="快捷操作" motionVariant="command">
          <button type="button">Inside</button>
        </ModalOverlay>,
      );
      const dialog = screen.getByRole("dialog", { name: "快捷操作" });
      expect(dialog.style.opacity).toBe("0");
      expect(requestFrameSpy).toHaveBeenCalledTimes(1);

      unmount();
      expect(cancelFrameSpy).toHaveBeenCalledWith(23);
      expect(dialog.style.opacity).toBe("");
    } finally {
      requestFrameSpy.mockRestore();
      cancelFrameSpy.mockRestore();
    }
  });

  it("dialog enters from and reverses to the same trigger center", () => {
    const rectSpy = jest.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      const shell = this.classList.contains("modal-content");
      const rect = shell
        ? { left: 200, top: 100, width: 400, height: 300 }
        : { left: 250, top: 180, width: 100, height: 40 };
      return {
        ...rect,
        bottom: rect.top + rect.height,
        right: rect.left + rect.width,
        x: rect.left,
        y: rect.top,
        toJSON: () => rect,
      };
    });
    const onClose = jest.fn();
    try {
      render(
        <ModalOverlay
          onClose={onClose}
          ariaLabel="测试弹窗"
          contentClassName="modal-content"
          trigger={document.createElement("button")}
        >
          <button type="button">Inside</button>
        </ModalOverlay>,
      );
      expect(setMock).toHaveBeenCalledWith(document.querySelector(".modal-content"), {
        opacity: 0,
        scale: 0.25,
        x: -100,
        y: -50,
      });
      expect(document.querySelector<HTMLElement>(".modal-content")?.style.transformOrigin).toBe("200px 150px");

      fireEvent.click(document.querySelector(".modal-backdrop-button")!);
      expect(onClose).not.toHaveBeenCalled();
      expect(lastToVars()).toMatchObject({
        opacity: 0,
        scale: 0.25,
        x: -100,
        y: -50,
        duration: 0.26,
        ease: "back.in(1.15)",
      });
      completeExit();
      expect(onClose).toHaveBeenCalledTimes(1);
    } finally {
      rectSpy.mockRestore();
    }
  });

  it("command reverses along the same trigger path and ignores repeated close requests", () => {
    const rectSpy = jest.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      const shell = this.classList.contains("command-shell");
      const rect = shell
        ? { left: 200, top: 100, width: 400, height: 300 }
        : { left: 250, top: 180, width: 100, height: 40 };
      return {
        ...rect,
        bottom: rect.top + rect.height,
        right: rect.left + rect.width,
        x: rect.left,
        y: rect.top,
        toJSON: () => rect,
      };
    });
    const onClose = jest.fn();
    try {
      render(
        <ModalOverlay
          onClose={onClose}
          ariaLabel="快捷操作"
          motionVariant="command"
          contentClassName="command-shell"
          trigger={document.createElement("button")}
        >
          <button type="button">Inside</button>
        </ModalOverlay>,
      );
      const dialog = screen.getByRole("dialog", { name: "快捷操作" });
      const content = document.querySelector<HTMLElement>(".command-shell");
      const entryTweenCount = toMock.mock.calls.length;
      expect(setMock).toHaveBeenCalledWith(content, { opacity: 0, scale: 0.25, x: -100, y: -50 });

      fireEvent(dialog, new Event("cancel", { bubbles: false, cancelable: true }));
      fireEvent(dialog, new Event("cancel", { bubbles: false, cancelable: true }));

      expect(toMock).toHaveBeenCalledTimes(entryTweenCount + 1);
      expect(onClose).not.toHaveBeenCalled();
      expect(content?.style.transformOrigin).toBe("200px 150px");
      expect(lastToVars()).toMatchObject({
        opacity: 0,
        scale: 0.25,
        x: -100,
        y: -50,
        duration: 0.34,
        ease: "back.in(1.2)",
      });
      completeExit();
      expect(onClose).toHaveBeenCalledTimes(1);
    } finally {
      rectSpy.mockRestore();
    }
  });

  it("runs one exit before onClose and ignores repeated close requests", () => {
    const onClose = jest.fn();
    render(
      <ModalOverlay onClose={onClose} ariaLabel="测试弹窗" contentClassName="modal-content">
        <button type="button">Inside</button>
      </ModalOverlay>,
    );
    const dialog = screen.getByRole("dialog", { name: "测试弹窗" });
    const content = document.querySelector(".modal-content");

    fireEvent.click(content!);
    expect(onClose).not.toHaveBeenCalled();
    fireEvent.click(document.querySelector(".modal-backdrop-button")!);
    fireEvent(dialog, new Event("cancel", { bubbles: false, cancelable: true }));
    expect(onClose).not.toHaveBeenCalled();
    expect(lastToVars()).toMatchObject({
      opacity: 0,
      scale: 0.96,
      x: 0,
      y: 0,
      duration: 0.26,
      ease: "back.in(1.15)",
    });

    completeExit();
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("provides requestModalClose for business buttons without bypassing the exit", () => {
    const onClose = jest.fn();
    render(
      <ModalOverlay onClose={onClose} ariaLabel="测试弹窗">
        <button type="button" onClick={(event) => requestModalClose(event.currentTarget)}>
          动画关闭
        </button>
      </ModalOverlay>,
    );
    fireEvent.click(screen.getByRole("button", { name: "动画关闭" }));
    expect(onClose).not.toHaveBeenCalled();
    completeExit();
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("restores trigger focus after Escape closes and unmounts the dialog", async () => {
    function Harness() {
      const [open, setOpen] = useState(false);
      return (
        <>
          <button type="button" onClick={() => setOpen(true)}>打开</button>
          {open ? (
            <ModalOverlay onClose={() => setOpen(false)} ariaLabel="测试弹窗">
              <button type="button">Inside</button>
            </ModalOverlay>
          ) : null}
        </>
      );
    }
    render(<Harness />);
    const trigger = screen.getByRole("button", { name: "打开" });
    trigger.focus();
    fireEvent.click(trigger);
    const dialog = screen.getByRole("dialog", { name: "测试弹窗" });
    fireEvent(dialog, new Event("cancel", { bubbles: false, cancelable: true }));
    completeExit();
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(trigger).toHaveFocus();
  });

  it("keeps Tab inside the visible content", () => {
    render(
      <ModalOverlay onClose={jest.fn()} ariaLabel="测试弹窗">
        <button type="button">第一个</button>
        <button type="button">第二个</button>
      </ModalOverlay>,
    );
    const dialog = screen.getByRole("dialog", { name: "测试弹窗" });
    const first = screen.getByRole("button", { name: "第一个" });
    const last = screen.getByRole("button", { name: "第二个" });
    last.focus();
    fireEvent.keyDown(dialog, { key: "Tab" });
    expect(first).toHaveFocus();
    fireEvent.keyDown(dialog, { key: "Tab", shiftKey: true });
    expect(last).toHaveFocus();
  });

  it("opens and closes immediately with reduced motion", () => {
    setReducedMotion(true);
    const onClose = jest.fn();
    render(
      <ModalOverlay onClose={onClose} ariaLabel="测试弹窗">
        <button type="button">Inside</button>
      </ModalOverlay>,
    );
    expect(toMock).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Inside" })).toHaveFocus();
    fireEvent.click(document.querySelector(".modal-backdrop-button")!);
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(toMock).not.toHaveBeenCalled();
  });

  it("closeDisabled blocks backdrop and Escape without starting an exit", () => {
    const onClose = jest.fn();
    const { rerender } = render(
      <ModalOverlay onClose={onClose} ariaLabel="忙碌弹窗" closeDisabled>
        <button type="button">Inside</button>
      </ModalOverlay>,
    );
    const dialog = screen.getByRole("dialog", { name: "忙碌弹窗" });
    const entryCount = toMock.mock.calls.length;
    fireEvent.click(document.querySelector(".modal-backdrop-button")!);
    fireEvent(dialog, new Event("cancel", { bubbles: false, cancelable: true }));
    expect(toMock).toHaveBeenCalledTimes(entryCount);
    expect(onClose).not.toHaveBeenCalled();

    rerender(
      <ModalOverlay onClose={onClose} ariaLabel="忙碌弹窗" closeDisabled={false}>
        <button type="button">Inside</button>
      </ModalOverlay>,
    );
    fireEvent.click(document.querySelector(".modal-backdrop-button")!);
    expect(toMock).toHaveBeenCalledTimes(entryCount + 1);
    completeExit();
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("separates a dismiss request from a confirmed business close", () => {
    const onClose = jest.fn();
    const onRequestClose = jest.fn();
    render(
      <ModalOverlay
        onClose={onClose}
        onRequestClose={onRequestClose}
        ariaLabel="需要确认的弹窗"
      >
        <button type="button" onClick={(event) => requestModalClose(event.currentTarget)}>
          确认关闭
        </button>
      </ModalOverlay>,
    );
    fireEvent.click(document.querySelector(".modal-backdrop-button")!);
    expect(onRequestClose).toHaveBeenCalledTimes(1);
    expect(onClose).not.toHaveBeenCalled();
    expect(lastToVars()).toMatchObject({ opacity: 1 });

    fireEvent.click(screen.getByRole("button", { name: "确认关闭" }));
    expect(onClose).not.toHaveBeenCalled();
    completeExit();
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(onRequestClose).toHaveBeenCalledTimes(1);
  });

});
