import { useState } from "react";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { FilterPopover } from "@/features/files/components/FilterPopover";
import { matchesCondition, type FilterCondition, type FileView } from "@/features/files/model";

const MB = 1024 * 1024;
const FILES: FileView[] = [
  { id: 1, name: "small.zip", size: MB, isDirectory: false, createdAtMs: 0 },
  { id: 2, name: "big.zip", size: 200 * MB, isDirectory: false, createdAtMs: 0 },
  { id: 3, name: "movie.mp4", size: 300 * MB, isDirectory: false, createdAtMs: 0 },
  { id: 4, name: "素材", size: 0, isDirectory: true, createdAtMs: 0 },
];

beforeEach(() => {
  // 每个用例重置为减少动态效果；动画用例自行覆盖为 matches: false
  Object.defineProperty(window, "matchMedia", {
    configurable: true,
    writable: true,
    value: jest.fn((query: string) => ({ matches: query === "(prefers-reduced-motion: reduce)", media: query })),
  });
});

function condition(overrides: Partial<FilterCondition>): FilterCondition {
  return { id: 1, field: "type", rule: "eq", value: "", ...overrides };
}

function Harness({
  initial = [],
  closing = false,
  trigger = null,
  onRequestClose = jest.fn(),
  onClosed = jest.fn(),
}: {
  initial?: FilterCondition[];
  closing?: boolean;
  trigger?: HTMLElement | null;
  onRequestClose?: jest.Mock;
  onClosed?: jest.Mock;
}) {
  const [conditions, setConditions] = useState(initial);
  const visibleCount = FILES.filter((file) => conditions.every((c) => matchesCondition(file, c))).length;
  return (
    <FilterPopover
      conditions={conditions}
      files={FILES}
      visibleCount={visibleCount}
      trigger={trigger}
      closing={closing}
      onChange={setConditions}
      onRequestClose={onRequestClose}
      onClosed={onClosed}
    />
  );
}

describe("FilterPopover 条件编辑", () => {
  it("无条件时显示提示；添加条件后未选指标禁止继续添加", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    expect(screen.getByText(/尚未添加条件/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "＋ 添加条件" }));
    expect(screen.getByRole("button", { name: "条件 1 字段" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "条件 1 指标" })).toHaveTextContent("选择…");
    expect(screen.getByRole("button", { name: "＋ 添加条件" })).toBeDisabled();
  });

  it("选择规则与指标后即时显示隐藏计数，可继续添加且行间出现 AND", async () => {
    const user = userEvent.setup();
    render(<Harness />);
    await user.click(screen.getByRole("button", { name: "＋ 添加条件" }));
    await user.click(screen.getByRole("button", { name: "条件 1 规则" }));
    await user.click(screen.getByRole("option", { name: "≠" }));
    await user.click(screen.getByRole("button", { name: "条件 1 指标" }));
    await user.click(screen.getByRole("option", { name: "zip" }));
    // small.zip 与 big.zip 被隐藏，剩 movie.mp4 与素材文件夹
    expect(screen.getByText(/已隐藏 2 项/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "＋ 添加条件" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "＋ 添加条件" }));
    expect(screen.getByText("AND")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "条件 2 字段" })).toBeInTheDocument();
  });

  it("换字段时规则与指标重置", async () => {
    const user = userEvent.setup();
    render(<Harness initial={[condition({ rule: "eq", value: "zip" })]} />);
    await user.click(screen.getByRole("button", { name: "条件 1 字段" }));
    await user.click(screen.getByRole("option", { name: "大小" }));
    expect(screen.getByRole("button", { name: "条件 1 规则" })).toHaveTextContent("≥");
    expect(screen.getByRole("button", { name: "条件 1 指标" })).toHaveTextContent("选择…");
  });

  it("换规则后指标若冲突则清空", async () => {
    const user = userEvent.setup();
    render(<Harness initial={[
      condition({ id: 1, field: "size", rule: "gt", value: String(100 * MB) }),
      condition({ id: 2, field: "size", rule: "gt", value: String(500 * MB) }),
    ]} />);
    // 行1 从 ≥ 换成 < 后，100MB ≤ 其他行下限 500MB，指标被清空
    await user.click(screen.getByRole("button", { name: "条件 1 规则" }));
    await user.click(screen.getByRole("option", { name: "<" }));
    expect(screen.getByRole("button", { name: "条件 1 指标" })).toHaveTextContent("选择…");
  });

  it("已有生效的类型 = 条件时，其他行的 = 规则禁选", async () => {
    const user = userEvent.setup();
    render(<Harness initial={[
      condition({ id: 1, rule: "eq", value: "zip" }),
      condition({ id: 2, rule: "ne", value: "" }),
    ]} />);
    await user.click(screen.getByRole("button", { name: "条件 2 规则" }));
    expect(screen.getByRole("option", { name: "=" })).toHaveAttribute("aria-disabled", "true");
    expect(screen.getByRole("option", { name: "≠" })).not.toHaveAttribute("aria-disabled");
  });

  it("删除条件行后剩余行内容正确", async () => {
    const user = userEvent.setup();
    render(<Harness initial={[
      condition({ id: 1, rule: "ne", value: "zip" }),
      condition({ id: 2, rule: "eq", value: "mp4" }),
    ]} />);
    await user.click(screen.getByRole("button", { name: "删除条件 1" }));
    expect(screen.getByRole("button", { name: "条件 1 指标" })).toHaveTextContent("mp4");
    expect(screen.queryByRole("button", { name: "条件 2 字段" })).not.toBeInTheDocument();
  });

  it("清除筛选恢复无条件状态", async () => {
    const user = userEvent.setup();
    render(<Harness initial={[condition({ rule: "ne", value: "zip" })]} />);
    await user.click(screen.getByRole("button", { name: "清除筛选" }));
    expect(screen.getByText(/尚未添加条件/)).toBeInTheDocument();
  });
});

describe("FilterPopover 关闭行为", () => {
  it("外部点击与 Esc 触发关闭请求", async () => {
    const user = userEvent.setup();
    const onRequestClose = jest.fn();
    render(<Harness onRequestClose={onRequestClose} />);
    await user.click(document.body);
    expect(onRequestClose).toHaveBeenCalledTimes(1);
    await user.keyboard("{Escape}");
    expect(onRequestClose).toHaveBeenCalledTimes(2);
  });

  it("点击下拉选项与列表内 Esc 都不触发浮层关闭", async () => {
    const user = userEvent.setup();
    const onRequestClose = jest.fn();
    render(<Harness initial={[condition({ rule: "eq", value: "" })]} onRequestClose={onRequestClose} />);
    await user.click(screen.getByRole("button", { name: "条件 1 指标" }));
    // 列表内 Esc 只关列表
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    expect(onRequestClose).not.toHaveBeenCalled();
    // 点选选项（portal 在浮层外）不关闭浮层
    await user.click(screen.getByRole("button", { name: "条件 1 指标" }));
    await user.click(screen.getByRole("option", { name: "zip" }));
    expect(onRequestClose).not.toHaveBeenCalled();
    expect(screen.getByRole("dialog", { name: "筛选" })).toBeInTheDocument();
  });

  it("closing 时按减少动态效果立即完成关闭", () => {
    const onClosed = jest.fn();
    const { rerender } = render(<Harness onClosed={onClosed} />);
    rerender(<Harness onClosed={onClosed} closing />);
    expect(onClosed).toHaveBeenCalled();
  });

  it("非减少动态效果时收起动画经 timer 完成并清理", () => {
    Object.defineProperty(window, "matchMedia", {
      configurable: true,
      writable: true,
      value: jest.fn(() => ({ matches: false })),
    });
    jest.useFakeTimers();
    try {
      const onClosed = jest.fn();
      const { rerender, unmount } = render(<Harness onClosed={onClosed} />);
      rerender(<Harness onClosed={onClosed} closing />);
      expect(onClosed).not.toHaveBeenCalled();
      jest.advanceTimersByTime(200);
      expect(onClosed).toHaveBeenCalled();
      unmount();
    } finally {
      jest.useRealTimers();
    }
  });

  it("animationend 直接触发关闭，不等待 timer", () => {
    Object.defineProperty(window, "matchMedia", {
      configurable: true,
      writable: true,
      value: jest.fn(() => ({ matches: false })),
    });
    jest.useFakeTimers();
    try {
      const onClosed = jest.fn();
      const { rerender } = render(<Harness onClosed={onClosed} />);
      rerender(<Harness onClosed={onClosed} closing />);
      const root = document.querySelector(".filter-pop") as HTMLElement;
      root.dispatchEvent(new window.Event("animationend"));
      expect(onClosed).toHaveBeenCalled();
    } finally {
      jest.useRealTimers();
    }
  });

  it("animationend 与 timer 重复触发时关闭只执行一次", () => {
    Object.defineProperty(window, "matchMedia", {
      configurable: true,
      writable: true,
      value: jest.fn(() => ({ matches: false })),
    });
    jest.useFakeTimers();
    try {
      const onClosed = jest.fn();
      const { rerender } = render(<Harness onClosed={onClosed} />);
      rerender(<Harness onClosed={onClosed} closing />);
      const root = document.querySelector(".filter-pop") as HTMLElement;
      root.dispatchEvent(new window.Event("animationend"));
      jest.advanceTimersByTime(400);
      root.dispatchEvent(new window.Event("animationend"));
      expect(onClosed).toHaveBeenCalledTimes(1);
    } finally {
      jest.useRealTimers();
    }
  });

  it("焦点在浮层内时关闭后焦点还给入口按钮", async () => {
    const user = userEvent.setup();
    const trigger = document.createElement("button");
    document.body.appendChild(trigger);
    const onClosed = jest.fn();
    const { rerender } = render(<Harness trigger={trigger} onClosed={onClosed} />);
    await user.click(screen.getByRole("button", { name: "＋ 添加条件" }));
    expect(screen.getByRole("button", { name: "条件 1 字段" })).toBeInTheDocument();
    (document.querySelector(".cond-remove") as HTMLElement).focus();
    rerender(<Harness trigger={trigger} onClosed={onClosed} closing />);
    expect(onClosed).toHaveBeenCalled();
    expect(trigger).toHaveFocus();
    trigger.remove();
  });
});
