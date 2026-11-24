import { render, screen } from "@testing-library/react";
import { renderToString } from "react-dom/server";
import { DialogShell } from "@/shared/components/DialogShell";
import { EmptyState } from "@/shared/components/EmptyState";

describe("DialogShell", () => {
  let originalShowModal: HTMLDialogElement["showModal"] | undefined;

  beforeEach(() => {
    originalShowModal = HTMLDialogElement.prototype.showModal;
    Object.defineProperty(HTMLDialogElement.prototype, "showModal", {
      configurable: true,
      value: jest.fn(function showModal(this: HTMLDialogElement) {
        this.setAttribute("open", "");
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
  });

  it("renders inline when portal=false", () => {
    render(
      <DialogShell onClose={jest.fn()} ariaLabel="内联弹窗" portal={false}>
        <p>内联内容</p>
      </DialogShell>
    );
    expect(screen.getByRole("dialog", { name: "内联弹窗" })).toBeInTheDocument();
    expect(screen.getByText("内联内容")).toBeInTheDocument();
  });

  it("renders via portal by default after mount", () => {
    render(
      <div data-testid="host">
        <DialogShell onClose={jest.fn()} ariaLabel="门户弹窗">
          <p>门户内容</p>
        </DialogShell>
      </div>
    );
    const dialog = screen.getByRole("dialog", { name: "门户弹窗" });
    expect(dialog.parentElement).toBe(document.body);
  });

  it("renders nothing when server-rendered before mount", () => {
    // useMounted 在 SSR 快照下返回 false：激活前不输出任何弹窗节点。
    const html = renderToString(
      <div data-testid="host">
        <DialogShell onClose={jest.fn()} ariaLabel="服务端弹窗">
          <p>服务端内容</p>
        </DialogShell>
      </div>,
    );
    expect(html).toBe('<div data-testid="host"></div>');
  });
});

describe("EmptyState", () => {
  it("renders icon, title, description and children", () => {
    render(
      <EmptyState
        icon={<span data-testid="icon">📁</span>}
        title="暂无文件"
        description="下载完成后显示"
      >
        <button type="button">去下载</button>
      </EmptyState>
    );
    expect(screen.getByTestId("icon")).toBeInTheDocument();
    expect(screen.getByText("暂无文件")).toBeInTheDocument();
    expect(screen.getByText("下载完成后显示")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "去下载" })).toBeInTheDocument();
    expect(document.querySelector(".empty-state")).toBeTruthy();
  });

  it("renders minimal state without optional parts", () => {
    render(<EmptyState title="空" />);
    expect(screen.getByText("空")).toBeInTheDocument();
    expect(document.querySelector(".empty-state-icon")).toBeNull();
  });
});
