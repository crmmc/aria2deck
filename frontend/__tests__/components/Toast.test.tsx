import React from "react";
import { render, screen, fireEvent, act, waitFor } from "@testing-library/react";
import { ToastProvider, useToast } from "@/components/Toast";

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
  beforeEach(() => {
    jest.useFakeTimers();
  });

  afterEach(() => {
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

  it("shows info toast", async () => {
    render(
      <ToastProvider>
        <TestComponent />
      </ToastProvider>
    );

    fireEvent.click(screen.getByText("Show Info"));
    
    await waitFor(() => {
      expect(screen.getByText("Test message")).toBeInTheDocument();
    });
  });

  it("shows success toast with correct icon", async () => {
    render(
      <ToastProvider>
        <TestComponent />
      </ToastProvider>
    );

    fireEvent.click(screen.getByText("Show Success"));
    
    await waitFor(() => {
      expect(screen.getByText("Success!")).toBeInTheDocument();
      expect(screen.getByText("✓")).toBeInTheDocument();
    });
  });

  it("shows error toast with correct icon", async () => {
    render(
      <ToastProvider>
        <TestComponent />
      </ToastProvider>
    );

    fireEvent.click(screen.getByText("Show Error"));
    
    await waitFor(() => {
      expect(screen.getByText("Error!")).toBeInTheDocument();
      expect(screen.getByText("✕")).toBeInTheDocument();
    });
  });

  it("shows warning toast with correct icon", async () => {
    render(
      <ToastProvider>
        <TestComponent />
      </ToastProvider>
    );

    fireEvent.click(screen.getByText("Show Warning"));
    
    await waitFor(() => {
      expect(screen.getByText("Warning!")).toBeInTheDocument();
      expect(screen.getByText("⚠")).toBeInTheDocument();
    });
  });

  it("auto-dismisses toast after 3 seconds", async () => {
    render(
      <ToastProvider>
        <TestComponent />
      </ToastProvider>
    );

    fireEvent.click(screen.getByText("Show Info"));
    
    await waitFor(() => {
      expect(screen.getByText("Test message")).toBeInTheDocument();
    });

    act(() => {
      jest.advanceTimersByTime(3000);
    });

    await waitFor(() => {
      expect(screen.queryByText("Test message")).not.toBeInTheDocument();
    });
  });

  it("shows confirm dialog", async () => {
    render(
      <ToastProvider>
        <TestComponent />
      </ToastProvider>
    );

    fireEvent.click(screen.getByText("Show Confirm"));
    
    await waitFor(() => {
      expect(screen.getByText("Confirm?")).toBeInTheDocument();
      expect(screen.getByText("取消")).toBeInTheDocument();
      expect(screen.getByText("确定")).toBeInTheDocument();
    });
  });

  it("resolves true when confirm button clicked", async () => {
    render(
      <ToastProvider>
        <TestComponent />
      </ToastProvider>
    );

    fireEvent.click(screen.getByText("Show Confirm"));
    
    await waitFor(() => {
      expect(screen.getByText("确定")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByText("确定"));

    await waitFor(() => {
      expect(document.body.getAttribute("data-confirm-result")).toBe("true");
    });
  });

  it("resolves false when cancel button clicked", async () => {
    render(
      <ToastProvider>
        <TestComponent />
      </ToastProvider>
    );

    fireEvent.click(screen.getByText("Show Confirm"));
    
    await waitFor(() => {
      expect(screen.getByText("取消")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByText("取消"));

    await waitFor(() => {
      expect(document.body.getAttribute("data-confirm-result")).toBe("false");
    });
  });

  it("resolves false when overlay clicked", async () => {
    render(
      <ToastProvider>
        <TestComponent />
      </ToastProvider>
    );

    fireEvent.click(screen.getByText("Show Confirm"));
    
    await waitFor(() => {
      expect(screen.getByText("Confirm?")).toBeInTheDocument();
    });

    const overlay = document.querySelector(".confirm-overlay");
    expect(overlay).toBeInTheDocument();
    fireEvent.click(overlay!);

    await waitFor(() => {
      expect(document.body.getAttribute("data-confirm-result")).toBe("false");
    });
  });

  it("shows custom confirm dialog options", async () => {
    render(
      <ToastProvider>
        <TestComponent />
      </ToastProvider>
    );

    fireEvent.click(screen.getByText("Show Custom Confirm"));
    
    await waitFor(() => {
      expect(screen.getByText("Custom Title")).toBeInTheDocument();
      expect(screen.getByText("Custom message")).toBeInTheDocument();
      expect(screen.getByText("Yes")).toBeInTheDocument();
      expect(screen.getByText("No")).toBeInTheDocument();
    });
  });

  it("applies danger style to confirm button", async () => {
    render(
      <ToastProvider>
        <TestComponent />
      </ToastProvider>
    );

    fireEvent.click(screen.getByText("Show Custom Confirm"));
    
    await waitFor(() => {
      const confirmButton = screen.getByText("Yes");
      expect(confirmButton).toHaveStyle({ background: "var(--danger)" });
    });
  });

  it("does not close confirm when clicking inside content", async () => {
    render(
      <ToastProvider>
        <TestComponent />
      </ToastProvider>
    );

    fireEvent.click(screen.getByText("Show Confirm"));
    
    await waitFor(() => {
      expect(screen.getByText("Confirm?")).toBeInTheDocument();
    });

    const content = document.querySelector(".confirm-content");
    fireEvent.click(content!);

    expect(screen.getByText("Confirm?")).toBeInTheDocument();
  });
});
