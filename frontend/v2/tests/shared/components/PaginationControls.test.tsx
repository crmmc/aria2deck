import { act, render, screen, fireEvent } from "@testing-library/react";
import { PaginationControls } from "@/shared/components/PaginationControls";
import {
  buildPageItems,
  MIN_PAGE_CAPACITY,
} from "@/shared/components/pagination-utils";
import { ToolbarGroup, ToolbarSearchInput, ToolbarShell } from "@/shared/components/Toolbar";

describe("buildPageItems", () => {
  it("returns all pages when total fits capacity", () => {
    expect(buildPageItems(1, 5, MIN_PAGE_CAPACITY)).toEqual([1, 2, 3, 4, 5]);
  });

  it("uses ellipsis windows for large page counts", () => {
    const items = buildPageItems(10, 30, MIN_PAGE_CAPACITY);
    expect(items[0]).toBe(1);
    expect(items[items.length - 1]).toBe(30);
    expect(items).toContain("ellipsis");
  });

  it("centers the window around the current page", () => {
    const items = buildPageItems(15, 40, MIN_PAGE_CAPACITY) as number[];
    const numbers = items.filter((i) => typeof i === "number");
    expect(numbers).toContain(15);
    expect(numbers.length).toBeGreaterThanOrEqual(5);
  });

  it("clamps capacity to the minimum", () => {
    expect(buildPageItems(1, 100, 1)).toEqual(buildPageItems(1, 100, MIN_PAGE_CAPACITY));
  });
});

describe("PaginationControls", () => {
  it("navigates with arrow buttons and disables at boundaries", () => {
    const onPageChange = jest.fn();
    render(
      <PaginationControls currentPage={1} pageSize={10} totalFiles={25} onPageChange={onPageChange} />
    );
    expect(screen.getByRole("button", { name: "首页" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "上一页" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "下一页" }));
    expect(onPageChange).toHaveBeenCalledWith(2);
    fireEvent.click(screen.getByRole("button", { name: "末页" }));
    expect(onPageChange).toHaveBeenCalledWith(3);
    expect(screen.getByText("共 25 项")).toBeInTheDocument();
  });

  it("中间页首页/上一页可用，下一页/末页在末页禁用", () => {
    const onPageChange = jest.fn();
    render(
      <PaginationControls currentPage={3} pageSize={10} totalFiles={30} onPageChange={onPageChange} />
    );
    fireEvent.click(screen.getByRole("button", { name: "首页" }));
    expect(onPageChange).toHaveBeenCalledWith(1);
    fireEvent.click(screen.getByRole("button", { name: "上一页" }));
    expect(onPageChange).toHaveBeenCalledWith(2);
    expect(screen.getByRole("button", { name: "下一页" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "末页" })).toBeDisabled();
  });

  it("jumps to a clamped page on Enter", () => {
    const onPageChange = jest.fn();
    render(
      <PaginationControls currentPage={1} pageSize={10} totalFiles={50} onPageChange={onPageChange} />
    );
    const jump = screen.getByLabelText("跳至第几页");
    fireEvent.change(jump, { target: { value: "99" } });
    fireEvent.keyDown(jump, { key: "Enter" });
    expect(onPageChange).toHaveBeenCalledWith(5);
  });

  it("blur 提交跳页并把非法输入归零，不触发翻页", () => {
    const onPageChange = jest.fn();
    render(
      <PaginationControls currentPage={2} pageSize={10} totalFiles={50} onPageChange={onPageChange} />
    );
    const jump = screen.getByLabelText("跳至第几页") as HTMLInputElement;
    // 空输入：直接返回，不清空也不翻页。
    fireEvent.change(jump, { target: { value: "" } });
    fireEvent.blur(jump);
    expect(onPageChange).not.toHaveBeenCalled();
    // 当前页相同：不回调，但输入被清空。
    fireEvent.change(jump, { target: { value: "2" } });
    fireEvent.blur(jump);
    expect(onPageChange).not.toHaveBeenCalled();
    expect(jump.value).toBe("");
    // 0 与负数被夹到第 1 页。
    fireEvent.change(jump, { target: { value: "-5" } });
    fireEvent.blur(jump);
    expect(onPageChange).toHaveBeenCalledWith(1);
  });

  it("resets to page 1 and notifies on page-size change", () => {
    const onPageChange = jest.fn();
    const onPageSizeChange = jest.fn();
    render(
      <PaginationControls
        currentPage={3}
        pageSize={10}
        totalFiles={100}
        onPageChange={onPageChange}
        onPageSizeChange={onPageSizeChange}
      />
    );
    fireEvent.change(screen.getByLabelText("每页条数"), { target: { value: "50" } });
    expect(onPageSizeChange).toHaveBeenCalledWith(50);
    expect(onPageChange).toHaveBeenCalledWith(1);
  });

  it("不传 onPageSizeChange 时不渲染每页条数选择器", () => {
    render(<PaginationControls currentPage={1} pageSize={10} totalFiles={10} onPageChange={jest.fn()} />);
    expect(screen.queryByLabelText("每页条数")).not.toBeInTheDocument();
  });

  it("点击其他页码翻页，点击当前页码不重复回调", () => {
    const onPageChange = jest.fn();
    render(<PaginationControls currentPage={1} pageSize={10} totalFiles={30} onPageChange={onPageChange} />);
    const pageButtons = screen.getAllByRole("button").filter((b) => /^\d+$/.test(b.textContent ?? ""));
    expect(pageButtons.map((b) => b.textContent)).toEqual(["1", "2", "3"]);
    fireEvent.click(screen.getByRole("button", { name: "1" }));
    expect(onPageChange).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "3" }));
    expect(onPageChange).toHaveBeenCalledWith(3);
  });

  it("当前页高亮为 primary，其他页为 secondary", () => {
    render(<PaginationControls currentPage={2} pageSize={10} totalFiles={30} onPageChange={jest.fn()} />);
    expect(screen.getByRole("button", { name: "2" }).className).toContain("primary");
    expect(screen.getByRole("button", { name: "1" }).className).toContain("secondary");
  });

  it("页数超出容量时渲染省略号占位", () => {
    render(<PaginationControls currentPage={50} pageSize={10} totalFiles={1000} onPageChange={jest.fn()} />);
    const ellipses = document.querySelectorAll("[aria-hidden='true']");
    const ellipsisTexts = Array.from(ellipses).filter((el) => el.textContent === "…");
    expect(ellipsisTexts).toHaveLength(2);
  });

  it("ResizeObserver 缺失时回退到最小容量", () => {
    const original = (globalThis as { ResizeObserver?: unknown }).ResizeObserver;
    Reflect.deleteProperty(globalThis, "ResizeObserver");
    try {
      render(<PaginationControls currentPage={1} pageSize={10} totalFiles={1000} onPageChange={jest.fn()} />);
      // 最小容量 7：窗口内页码数量按 7 格计算。
      const pageButtons = screen.getAllByRole("button").filter((b) => /^\d+$/.test(b.textContent ?? ""));
      expect(pageButtons).toHaveLength(6);
    } finally {
      if (original !== undefined) {
        Object.defineProperty(globalThis, "ResizeObserver", { value: original, configurable: true });
      }
    }
  });

  it("ResizeObserver 变化时按容器宽度重算容量", () => {
    const callbacks: (() => void)[] = [];
    const original = (globalThis as { ResizeObserver?: unknown }).ResizeObserver;
    class FakeResizeObserver {
      constructor(callback: () => void) {
        callbacks.push(callback);
      }
      observe() {}
      disconnect() {}
    }
    Object.defineProperty(globalThis, "ResizeObserver", {
      value: FakeResizeObserver,
      configurable: true,
      writable: true,
    });
    try {
      const { unmount } = render(
        <PaginationControls currentPage={1} pageSize={10} totalFiles={1000} onPageChange={jest.fn()} />,
      );
      // jsdom 里 clientWidth 为 0，固定占用为 0 => 容量被抬到最小值。
      const before = screen.getAllByRole("button").filter((b) => /^\d+$/.test(b.textContent ?? ""));
      expect(before).toHaveLength(6);

      const container = document.querySelector(".pagination-controls") as HTMLElement;
      Object.defineProperty(container, "clientWidth", { value: 360, configurable: true });
      act(() => {
        callbacks.forEach((cb) => cb());
      });

      const after = screen
        .getAllByRole("button")
        .filter((b) => /^\d+$/.test(b.textContent ?? "") && Number(b.textContent) <= 1000);
      // 360 / 36 = 10 格容量 => 内层 8 个 slot => 去掉两端与省略号后可见 8 个页码。
      expect(after.length).toBeGreaterThan(before.length);
      unmount();
    } finally {
      if (original === undefined) {
        Reflect.deleteProperty(globalThis, "ResizeObserver");
      } else {
        Object.defineProperty(globalThis, "ResizeObserver", { value: original, configurable: true });
      }
    }
  });
});

describe("Toolbar", () => {
  it("renders shell with filter-toolbar classes and group", () => {
    render(
      <ToolbarShell className="extra">
        <ToolbarGroup className="left">内容</ToolbarGroup>
      </ToolbarShell>
    );
    const shell = document.querySelector(".filter-toolbar.inline-filter-toolbar.extra");
    expect(shell).toBeTruthy();
    expect(document.querySelector(".filter-group.left")).toBeTruthy();
  });

  it("search input forwards changes", () => {
    const onChange = jest.fn();
    render(
      <ToolbarSearchInput value="" placeholder="搜索…" ariaLabel="搜索任务" onChange={onChange} />
    );
    fireEvent.change(screen.getByLabelText("搜索任务"), { target: { value: "ab" } });
    expect(onChange).toHaveBeenCalledWith("ab");
  });
});
