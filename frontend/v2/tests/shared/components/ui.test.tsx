import { useEffect } from "react";
import { fireEvent, render, screen } from "@testing-library/react";

import {
  ConfirmDialog,
  EmptyState,
  ErrorState,
  LoadingState,
  Modal,
  Pagination,
  StatusBadge,
} from "@/shared/components/ui";

describe("Modal", () => {
  it("focuses the panel, closes on Escape and restores previous focus", () => {
    const onClose = jest.fn();
    // 前置焦点元素挂在渲染树外，卸载 Modal 后才能验证焦点归还。
    const outside = document.createElement("button");
    outside.textContent = "前置按钮";
    document.body.appendChild(outside);
    outside.focus();
    const { unmount } = render(
      <Modal title="测试弹窗" onClose={onClose}>
        <p>内容</p>
      </Modal>,
    );
    const panel = screen.getByRole("dialog", { name: "测试弹窗" });
    expect(panel).toHaveFocus();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
    unmount();
    expect(outside).toHaveFocus();
    outside.remove();
  });

  it("closes when clicking the backdrop but not the panel", () => {
    const onClose = jest.fn();
    render(
      <Modal title="背板" onClose={onClose}>
        <p>内容</p>
      </Modal>,
    );
    fireEvent.click(screen.getByRole("dialog"));
    expect(onClose).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("dialog").parentElement!);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("closes via the close button and renders footer", () => {
    const onClose = jest.fn();
    render(
      <Modal title="带底栏" onClose={onClose} footer={<button type="button">底部动作</button>}>
        <p>内容</p>
      </Modal>,
    );
    fireEvent.click(screen.getByRole("button", { name: "关闭" }));
    expect(onClose).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "底部动作" })).toBeInTheDocument();
  });

  it("traps Tab focus inside the dialog and wraps at both ends", () => {
    const outside = document.createElement("button");
    outside.textContent = "对话框外按钮";
    document.body.appendChild(outside);
    try {
      render(
        <Modal title="焦点陷阱" onClose={jest.fn()} footer={<button type="button">确认</button>}>
          <input aria-label="输入" />
        </Modal>,
      );
      const dialog = screen.getByRole("dialog", { name: "焦点陷阱" });
      expect(dialog).toHaveFocus(); // 初始焦点在面板。

      fireEvent.keyDown(document, { key: "Tab" }); // 面板 → 第一个可聚焦元素。
      expect(screen.getByRole("button", { name: "关闭" })).toHaveFocus();
      fireEvent.keyDown(document, { key: "Tab" }); // 关闭 → 输入框。
      expect(screen.getByLabelText("输入")).toHaveFocus();
      fireEvent.keyDown(document, { key: "Tab" }); // 输入框 → 确认。
      expect(screen.getByRole("button", { name: "确认" })).toHaveFocus();
      fireEvent.keyDown(document, { key: "Tab" }); // 末尾回绕到第一个。
      expect(screen.getByRole("button", { name: "关闭" })).toHaveFocus();
      fireEvent.keyDown(document, { key: "Tab", shiftKey: true }); // Shift+Tab 从头部回绕到末尾。
      expect(screen.getByRole("button", { name: "确认" })).toHaveFocus();

      expect(document.activeElement === outside).toBe(false);
    } finally {
      outside.remove();
    }
  });

  it("ignores non-Tab keys and steps backwards with Shift+Tab inside the panel", () => {
    render(
      <Modal title="按键处理" onClose={jest.fn()} footer={<button type="button">确认</button>}>
        <input aria-label="输入" />
      </Modal>,
    );

    // 非 Tab 键不拦截（否则对话框里的输入框无法正常输入）。
    fireEvent.keyDown(document, { key: "a" });
    expect(screen.getByRole("dialog", { name: "按键处理" })).toHaveFocus();

    fireEvent.keyDown(document, { key: "Tab", shiftKey: true }); // 面板在列表外 → 末尾元素。
    expect(screen.getByRole("button", { name: "确认" })).toHaveFocus();
    fireEvent.keyDown(document, { key: "Tab", shiftKey: true }); // 末尾 → 前一个元素。
    expect(screen.getByLabelText("输入")).toHaveFocus();
    fireEvent.keyDown(document, { key: "Tab", shiftKey: true }); // 中间元素 → 前一个。
    expect(screen.getByRole("button", { name: "关闭" })).toHaveFocus();
  });

  it("ignores a Tab keydown that arrives while the panel is being torn down", () => {
    const outside = document.createElement("button");
    outside.textContent = "外部按钮";
    document.body.appendChild(outside);
    outside.focus();

    function DispatchDuringTeardown() {
      // 同级节点先于 Modal 卸载：模拟同一次提交里弹出的迟到按键事件。
      useEffect(() => {
        return () => {
          document.dispatchEvent(new KeyboardEvent("keydown", { key: "Tab", bubbles: true }));
        };
      }, []);
      return null;
    }

    try {
      const { unmount } = render(
        <>
          <DispatchDuringTeardown />
          <Modal title="卸载中" onClose={jest.fn()}>
            <p>内容</p>
          </Modal>
        </>,
      );
      expect(screen.getByRole("dialog", { name: "卸载中" })).toBeInTheDocument();

      expect(() => unmount()).not.toThrow();
      // 面板已拆下：迟到的 Tab 既不报错也不把焦点拉回已卸载的弹窗。
      expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
      expect(outside).toHaveFocus();
    } finally {
      outside.remove();
    }
  });

  it("pulls focus back into the panel when it has escaped to the page", () => {
    render(
      <Modal title="焦点回转" onClose={jest.fn()} footer={<button type="button">确认</button>}>
        <p>纯文本内容</p>
      </Modal>,
    );
    const dialog = screen.getByRole("dialog", { name: "焦点回转" });
    const outside = document.createElement("button");
    outside.textContent = "外部按钮";
    document.body.appendChild(outside);
    try {
      outside.focus();
      expect(outside).toHaveFocus();

      fireEvent.keyDown(document, { key: "Tab" });
      // 焦点在面板外时从第一个可聚焦元素重新开始。
      expect(screen.getByRole("button", { name: "关闭" })).toHaveFocus();
      expect(dialog.contains(document.activeElement)).toBe(true);
    } finally {
      outside.remove();
    }
  });
});

describe("ConfirmDialog", () => {
  it("confirms and cancels through footer buttons", () => {
    const onConfirm = jest.fn();
    const onCancel = jest.fn();
    render(
      <ConfirmDialog
        title="危险操作"
        message="确定执行？"
        confirmLabel="执行"
        danger
        onConfirm={onConfirm}
        onCancel={onCancel}
      />,
    );
    expect(screen.getByRole("button", { name: "执行" })).toHaveClass("danger-button");
    fireEvent.click(screen.getByRole("button", { name: "执行" }));
    expect(onConfirm).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "取消" }));
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("uses the primary button style when not dangerous", () => {
    render(
      <ConfirmDialog
        title="普通确认"
        message="继续？"
        confirmLabel="继续"
        onConfirm={jest.fn()}
        onCancel={jest.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: "继续" })).toHaveClass("primary-button");
  });
});

describe("Pagination", () => {
  it("disables prev on first page and next on last page", () => {
    const onChange = jest.fn();
    const { rerender } = render(
      <Pagination page={1} pageSize={20} total={40} onChange={onChange} />,
    );
    expect(screen.getByLabelText("上一页")).toBeDisabled();
    expect(screen.getByLabelText("下一页")).not.toBeDisabled();
    expect(screen.getByText("第 1 / 2 页 · 共 40 条")).toBeInTheDocument();
    fireEvent.click(screen.getByLabelText("下一页"));
    expect(onChange).toHaveBeenCalledWith(2);

    rerender(<Pagination page={2} pageSize={20} total={40} onChange={onChange} />);
    expect(screen.getByLabelText("上一页")).not.toBeDisabled();
    expect(screen.getByLabelText("下一页")).toBeDisabled();
    fireEvent.click(screen.getByLabelText("上一页"));
    expect(onChange).toHaveBeenCalledWith(1);
  });

  it("keeps a single page when total is zero", () => {
    render(<Pagination page={1} pageSize={20} total={0} onChange={jest.fn()} />);
    expect(screen.getByText("第 1 / 1 页 · 共 0 条")).toBeInTheDocument();
  });
});

describe("states and badges", () => {
  it("EmptyState renders optional hint", () => {
    render(<EmptyState title="空空如也" hint="提示文字" />);
    expect(screen.getByText("空空如也")).toBeInTheDocument();
    expect(screen.getByText("提示文字")).toBeInTheDocument();
  });

  it("EmptyState omits hint paragraph when absent", () => {
    render(<EmptyState title="只有标题" />);
    expect(screen.getByText("只有标题")).toBeInTheDocument();
    expect(screen.queryByRole("paragraph")).not.toBeInTheDocument();
  });

  it("LoadingState exposes aria-busy", () => {
    render(<LoadingState label="读取中…" />);
    expect(screen.getByText("读取中…")).toHaveAttribute("aria-busy", "true");
  });

  it("ErrorState shows message and optional retry", () => {
    const onRetry = jest.fn();
    render(<ErrorState message="出错了" onRetry={onRetry} />);
    expect(screen.getByRole("alert")).toHaveTextContent("出错了");
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it("ErrorState works without retry", () => {
    render(<ErrorState message="只读错误" />);
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });

  it("StatusBadge uses label fallback and tone mapping", () => {
    const { container } = render(
      <>
        <StatusBadge status="active" label="下载中" />
        <StatusBadge status="unknown-status" />
      </>,
    );
    expect(screen.getByText("下载中")).toBeInTheDocument();
    expect(screen.getByText("unknown-status")).toBeInTheDocument();
    const badges = container.querySelectorAll(".badge");
    expect(badges[0]).toHaveClass("tone-ok");
    expect(badges[1]).toHaveClass("tone-muted"); // 未知状态回退
  });
});
