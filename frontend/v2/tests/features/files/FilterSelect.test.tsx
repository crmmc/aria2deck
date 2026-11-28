import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { FilterSelect } from "@/features/files/components/FilterSelect";

const OPTIONS = [
  { value: "zip", label: "zip" },
  { value: "mp4", label: "mp4" },
  { value: "mkv", label: "mkv", disabled: true },
  { value: "pdf", label: "pdf" },
];

function setup(overrides: Partial<Parameters<typeof FilterSelect>[0]> = {}) {
  const onChange = jest.fn();
  const utils = render(
    <FilterSelect
      options={OPTIONS}
      value="zip"
      onChange={onChange}
      ariaLabel="指标"
      {...overrides}
    />,
  );
  return { onChange, ...utils };
}

describe("FilterSelect", () => {
  it("显示选中值；无值时显示占位", () => {
    const { rerender } = setup();
    expect(screen.getByRole("button", { name: "指标" })).toHaveTextContent("zip");
    rerender(<FilterSelect options={OPTIONS} value="" onChange={jest.fn()} ariaLabel="指标" />);
    expect(screen.getByRole("button", { name: "指标" })).toHaveTextContent("选择…");
    expect(screen.getByRole("button", { name: "指标" })).toHaveClass("is-placeholder");
  });

  it("点击打开列表，点选后回调并关闭", async () => {
    const user = userEvent.setup();
    const { onChange } = setup();
    await user.click(screen.getByRole("button", { name: "指标" }));
    expect(screen.getByRole("listbox")).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "zip" })).toHaveClass("is-selected");
    await user.click(screen.getByRole("option", { name: "pdf" }));
    expect(onChange).toHaveBeenCalledWith("pdf");
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  });

  it("禁选项点击无效且列表保持打开", async () => {
    const user = userEvent.setup();
    const { onChange } = setup();
    await user.click(screen.getByRole("button", { name: "指标" }));
    await user.click(screen.getByRole("option", { name: "mkv" }));
    expect(onChange).not.toHaveBeenCalled();
    expect(screen.getByRole("listbox")).toBeInTheDocument();
  });

  it("键盘：方向键打开与循环移动（跳过禁选），Enter 选中", async () => {
    const user = userEvent.setup();
    const { onChange } = setup();
    const trigger = screen.getByRole("button", { name: "指标" });
    trigger.focus();
    await user.keyboard("{ArrowDown}");
    expect(screen.getByRole("listbox")).toBeInTheDocument();
    // 初始活动项是选中的 zip(0)；下移到 mp4(1)，再下移跳过禁选的 mkv(2) 到 pdf(3)
    await user.keyboard("{ArrowDown}");
    expect(screen.getByRole("option", { name: "mp4" })).toHaveClass("is-active");
    await user.keyboard("{ArrowDown}");
    expect(screen.getByRole("option", { name: "pdf" })).toHaveClass("is-active");
    // 继续下移循环回 zip(0)
    await user.keyboard("{ArrowDown}");
    expect(screen.getByRole("option", { name: "zip" })).toHaveClass("is-active");
    await user.keyboard("{ArrowUp}");
    expect(screen.getByRole("option", { name: "pdf" })).toHaveClass("is-active");
    await user.keyboard("{Enter}");
    expect(onChange).toHaveBeenCalledWith("pdf");
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  });

  it("Esc 关闭列表并还原焦点到触发器", async () => {
    const user = userEvent.setup();
    setup();
    const trigger = screen.getByRole("button", { name: "指标" });
    await user.click(trigger);
    expect(screen.getByRole("listbox")).toHaveFocus();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    expect(trigger).toHaveFocus();
  });

  it("外部点击关闭列表", async () => {
    const user = userEvent.setup();
    setup();
    await user.click(screen.getByRole("button", { name: "指标" }));
    expect(screen.getByRole("listbox")).toBeInTheDocument();
    await user.click(document.body);
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  });

  it("同一时刻只展开一个列表", async () => {
    const user = userEvent.setup();
    const { onChange } = setup();
    render(
      <FilterSelect options={OPTIONS} value="" onChange={jest.fn()} ariaLabel="规则" />,
    );
    await user.click(screen.getByRole("button", { name: "指标" }));
    expect(screen.getByRole("listbox")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "规则" }));
    expect(screen.getAllByRole("listbox")).toHaveLength(1);
    expect(screen.getByRole("button", { name: "规则" })).toHaveAttribute("aria-expanded", "true");
    expect(onChange).not.toHaveBeenCalled();
  });

  it("卸载时移除选项列表", async () => {
    const user = userEvent.setup();
    const { unmount } = setup();
    await user.click(screen.getByRole("button", { name: "指标" }));
    expect(screen.getByRole("listbox")).toBeInTheDocument();
    unmount();
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  });

  it("再次点击触发器收起列表", async () => {
    const user = userEvent.setup();
    setup();
    const trigger = screen.getByRole("button", { name: "指标" });
    await user.click(trigger);
    expect(screen.getByRole("listbox")).toBeInTheDocument();
    await user.click(trigger);
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  });

  it("打开时按方向键不重复打开，其他键无效果", async () => {
    const user = userEvent.setup();
    setup();
    const trigger = screen.getByRole("button", { name: "指标" });
    await user.click(trigger);
    trigger.focus();
    await user.keyboard("{ArrowDown}");
    await user.keyboard("a");
    expect(screen.getAllByRole("listbox")).toHaveLength(1);
  });

  it("全部选项禁用时方向键与 Enter 均不生效", async () => {
    const user = userEvent.setup();
    const onChange = jest.fn();
    render(
      <FilterSelect
        options={[{ value: "a", label: "a", disabled: true }, { value: "b", label: "b", disabled: true }]}
        value=""
        onChange={onChange}
        ariaLabel="指标"
      />,
    );
    await user.click(screen.getByRole("button", { name: "指标" }));
    expect(screen.getByRole("listbox")).toBeInTheDocument();
    await user.keyboard("{ArrowDown}");
    await user.keyboard("{Enter}");
    expect(onChange).not.toHaveBeenCalled();
    expect(screen.getByRole("listbox")).toBeInTheDocument();
  });
});
