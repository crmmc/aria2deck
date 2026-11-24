import React, { useState } from "react";
import { render, screen, fireEvent, act, waitFor } from "@testing-library/react";
import { ModalOverlay } from "@/shared/components/ModalOverlay";
import { ToastProvider } from "@/shared/components/Toast";
import { useToast } from "@/shared/components/toast-context";
import { requestModalClose } from "@/shared/ui/modalClose";

function TestComponent() {
  const { showToast, showConfirm } = useToast();
  return (
    <div>
      <button onClick={() => showToast("Test message")}>Show Info</button>
      <button onClick={() => showToast("Success!", "success")}>Show Success</button>
      <button onClick={() => showToast("Error!", "error")}>Show Error</button>
      <button onClick={() => showToast("Warning!", "warning")}>Show Warning</button>
      <button
        onClick={async () => {
          const result = await showConfirm({ message: "Confirm?" });
          document.body.setAttribute("data-confirm-result", String(result));
        }}
      >
        Show Confirm
      </button>
      <button
        onClick={async () => {
          const result = await showConfirm({
            title: "Custom Title",
            message: "Custom message",
            confirmText: "Yes",
            cancelText: "No",
            danger: true,
          });
          document.body.setAttribute("data-confirm-result", String(result));
        }}
      >
        Show Custom Confirm
      </button>
    </div>
  );
}

function LayeredToastComponent() {
  const { showToast, showConfirm } = useToast();
  const [outerOpen, setOuterOpen] = useState(false);
  return (
    <>
      <button onClick={() => setOuterOpen(true)}>打开外层</button>
      <button onClick={() => showToast("层级提示", "warning")}>显示层级提示</button>
      {outerOpen ? (
        <ModalOverlay onClose={() => setOuterOpen(false)} ariaLabel="外层弹窗">
          <button onClick={() => void showConfirm({ title: "内层确认", message: "继续？" })}>
            打开内层
          </button>
          <button onClick={(event) => requestModalClose(event.currentTarget)}>关闭外层</button>
        </ModalOverlay>
      ) : null}
    </>
  );
}

describe("useToast", () => {
  it("throws error when used outside ToastProvider", () => {
    const consoleError = jest.spyOn(console, "error").mockImplementation(() => {});

    function BadComponent() {
      useToast();
      return null;
    }

    expect(() => render(<BadComponent />)).toThrow(
      "useToast must be used within ToastProvider"
    );

    consoleError.mockRestore();
  });
});

describe("ToastProvider", () => {
  let originalShowModal: HTMLDialogElement["showModal"] | undefined;
  let originalClose: HTMLDialogElement["close"] | undefined;

  beforeEach(() => {
    jest.useFakeTimers();
    originalShowModal = HTMLDialogElement.prototype.showModal;
    originalClose = HTMLDialogElement.prototype.close;
    Object.defineProperty(HTMLDialogElement.prototype, "showModal", {
      configurable: true,
      value: jest.fn(function showModal(this: HTMLDialogElement) {
        this.setAttribute("open", "");
      }),
    });
    Object.defineProperty(HTMLDialogElement.prototype, "close", {
      configurable: true,
      value: jest.fn(function close(this: HTMLDialogElement) {
        this.removeAttribute("open");
      }),
    });
  });

  afterEach(() => {
    document.querySelectorAll("dialog").forEach((d) => d.removeAttribute("open"));
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
    jest.useRealTimers();
    document.body.removeAttribute("data-confirm-result");
  });

  it("renders children", () => {
    render(
      <ToastProvider>
        <div data-testid="child">Child content</div>
      </ToastProvider>
    );

    expect(screen.getByTestId("child")).toBeInTheDocument();
  });

  it("shows info toast", () => {
    render(
      <ToastProvider>
        <TestComponent />
      </ToastProvider>
    );

    fireEvent.click(screen.getByText("Show Info"));
    expect(screen.getByText("Test message")).toBeInTheDocument();
    expect(document.querySelector(".toast-info")).toBeTruthy();
    expect(document.querySelector(".toast-container")?.parentElement).toBe(document.body);
  });

  it("shows success toast with check icon", () => {
    render(
      <ToastProvider>
        <TestComponent />
      </ToastProvider>
    );

    fireEvent.click(screen.getByText("Show Success"));
    const toast = document.querySelector(".toast-success");
    expect(toast).toBeTruthy();
    expect(toast?.querySelector('[data-icon="check"]')).toBeInTheDocument();
  });

  it("shows error toast with cross icon", () => {
    render(
      <ToastProvider>
        <TestComponent />
      </ToastProvider>
    );

    fireEvent.click(screen.getByText("Show Error"));
    const toast = document.querySelector(".toast-error");
    expect(toast).toBeTruthy();
    expect(toast?.querySelector('[data-icon="x"]')).toBeInTheDocument();
  });

  it("shows warning toast with warning icon", () => {
    render(
      <ToastProvider>
        <TestComponent />
      </ToastProvider>
    );

    fireEvent.click(screen.getByText("Show Warning"));
    const toast = document.querySelector(".toast-warning");
    expect(toast).toBeTruthy();
    expect(toast?.querySelector('[data-icon="alert"]')).toBeInTheDocument();
  });

  it("auto-dismisses toasts after 3 seconds", () => {
    render(
      <ToastProvider>
        <TestComponent />
      </ToastProvider>
    );

    fireEvent.click(screen.getByText("Show Info"));
    expect(screen.getByText("Test message")).toBeInTheDocument();

    act(() => {
      jest.advanceTimersByTime(3000);
    });

    expect(screen.queryByText("Test message")).not.toBeInTheDocument();
  });

  it("follows the top modal through nested confirmation without resetting the 3 second lifetime", () => {
    render(
      <ToastProvider>
        <LayeredToastComponent />
      </ToastProvider>,
    );
    const openOuter = screen.getByRole("button", { name: "打开外层" });
    openOuter.focus();
    fireEvent.click(openOuter);
    fireEvent.click(screen.getByRole("button", { name: "显示层级提示" }));

    const outer = screen.getByRole("dialog", { name: "外层弹窗" });
    const getToastContainer = () => document.querySelector(".toast-container");
    expect(getToastContainer()?.parentElement).toBe(outer);

    act(() => jest.advanceTimersByTime(1000));
    const openInner = screen.getByRole("button", { name: "打开内层" });
    fireEvent.click(openInner);
    const inner = screen.getByRole("dialog", { name: "内层确认" });
    expect(inner.parentElement).toBe(document.body);
    expect(getToastContainer()?.parentElement).toBe(inner);

    act(() => jest.advanceTimersByTime(1000));
    fireEvent.click(screen.getByRole("button", { name: "确定" }));
    expect(screen.queryByRole("dialog", { name: "内层确认" })).not.toBeInTheDocument();
    expect(getToastContainer()?.parentElement).toBe(outer);
    expect(openInner).toHaveFocus();

    act(() => jest.advanceTimersByTime(500));
    fireEvent.click(screen.getByRole("button", { name: "关闭外层" }));
    expect(screen.queryByRole("dialog", { name: "外层弹窗" })).not.toBeInTheDocument();
    expect(getToastContainer()?.parentElement).toBe(document.body);
    expect(openOuter).toHaveFocus();
    expect(screen.getByText("层级提示")).toBeInTheDocument();

    act(() => jest.advanceTimersByTime(500));
    expect(screen.queryByText("层级提示")).not.toBeInTheDocument();
  });

  it("confirm resolves true when confirmed", async () => {
    render(
      <ToastProvider>
        <TestComponent />
      </ToastProvider>
    );

    fireEvent.click(screen.getByText("Show Confirm"));
    expect(screen.getByText("Confirm?")).toBeInTheDocument();

    fireEvent.click(screen.getByText("确定"));
    await screen.findByText("Show Confirm");

    expect(document.body.getAttribute("data-confirm-result")).toBe("true");
  });

  it("confirm resolves false when cancelled", async () => {
    render(
      <ToastProvider>
        <TestComponent />
      </ToastProvider>
    );

    fireEvent.click(screen.getByText("Show Confirm"));
    fireEvent.click(screen.getByText("取消"));

    await screen.findByText("Show Confirm");
    expect(document.body.getAttribute("data-confirm-result")).toBe("false");
  });

  it("supports custom confirm options", async () => {
    render(
      <ToastProvider>
        <TestComponent />
      </ToastProvider>
    );

    fireEvent.click(screen.getByText("Show Custom Confirm"));
    expect(screen.getByText("Custom Title")).toBeInTheDocument();
    expect(screen.getByText("Custom message")).toBeInTheDocument();

    fireEvent.click(screen.getByText("Yes"));
    await screen.findByText("Show Custom Confirm");
    expect(document.body.getAttribute("data-confirm-result")).toBe("true");
  });
});

describe("ToastProvider 确认框关闭途径", () => {
  let originalShowModal: HTMLDialogElement["showModal"] | undefined;
  let originalClose: HTMLDialogElement["close"] | undefined;

  beforeEach(() => {
    originalShowModal = HTMLDialogElement.prototype.showModal;
    originalClose = HTMLDialogElement.prototype.close;
    Object.defineProperty(HTMLDialogElement.prototype, "showModal", {
      configurable: true,
      value: jest.fn(function showModal(this: HTMLDialogElement) {
        this.setAttribute("open", "");
      }),
    });
    Object.defineProperty(HTMLDialogElement.prototype, "close", {
      configurable: true,
      value: jest.fn(function close(this: HTMLDialogElement) {
        this.removeAttribute("open");
      }),
    });
  });

  afterEach(() => {
    document.querySelectorAll("dialog").forEach((d) => d.removeAttribute("open"));
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
    document.body.removeAttribute("data-confirm-result");
  });

  it("背景点击（onClose 路径）以 false 结束等待中的确认", async () => {
    render(
      <ToastProvider>
        <TestComponent />
      </ToastProvider>
    );
    fireEvent.click(screen.getByText("Show Confirm"));
    expect(screen.getByText("Confirm?")).toBeInTheDocument();

    fireEvent.click(document.querySelector(".modal-backdrop-button")!);

    await screen.findByText("Show Confirm");
    await waitFor(() => expect(document.body.getAttribute("data-confirm-result")).toBe("false"));
    expect(screen.queryByText("Confirm?")).not.toBeInTheDocument();
  });

  it("原生 cancel 事件同样以 false 结束确认", async () => {
    render(
      <ToastProvider>
        <TestComponent />
      </ToastProvider>
    );
    fireEvent.click(screen.getByText("Show Confirm"));
    const dialog = screen.getByRole("dialog", { name: "确认操作" });
    fireEvent(dialog, new Event("cancel", { bubbles: false, cancelable: true }));

    await screen.findByText("Show Confirm");
    await waitFor(() => expect(document.body.getAttribute("data-confirm-result")).toBe("false"));
  });

  it("确认框无标题时使用兜底 aria-label", () => {
    render(
      <ToastProvider>
        <TestComponent />
      </ToastProvider>
    );
    fireEvent.click(screen.getByText("Show Confirm"));
    expect(screen.getByRole("dialog", { name: "确认操作" })).toBeInTheDocument();
  });
});
