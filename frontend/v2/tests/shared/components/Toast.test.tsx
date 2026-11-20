import React from "react";
import { render, screen, fireEvent, act } from "@testing-library/react";
import { ToastProvider } from "@/shared/components/Toast";
import { useToast } from "@/shared/components/toast-context";

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
    expect(toast?.querySelector(".toast-icon")?.textContent).toBe("✓");
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
    expect(toast?.querySelector(".toast-icon")?.textContent).toBe("✕");
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
    expect(toast?.querySelector(".toast-icon")?.textContent).toBe("⚠");
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
    expect(document.body.getAttribute("data-confirm-result")).toBe("false");
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
    expect(document.body.getAttribute("data-confirm-result")).toBe("false");
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
