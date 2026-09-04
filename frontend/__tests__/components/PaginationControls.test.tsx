import { act, fireEvent, render, screen } from "@testing-library/react";
import {
  buildPageItems,
  measurePageCapacity,
  MIN_PAGE_CAPACITY,
  PaginationControls,
} from "@/components/ui/PaginationControls";

type RenderOptions = {
  currentPage?: number;
  pageSize?: number;
  totalFiles?: number;
  onPageChange?: (page: number) => void;
  onPageSizeChange?: (size: number) => void;
  withPageSize?: boolean;
};

function renderControls({
  currentPage = 1,
  pageSize = 10,
  totalFiles = 30,
  onPageChange = jest.fn(),
  onPageSizeChange = jest.fn(),
  withPageSize = true,
}: RenderOptions = {}) {
  const props = {
    currentPage,
    pageSize,
    totalFiles,
    onPageChange,
    ...(withPageSize ? { onPageSizeChange } : {}),
  };
  const renderResult = render(<PaginationControls {...props} />);
  return { renderResult, onPageChange, onPageSizeChange };
}

function pageButton(page: number) {
  return screen.queryByRole("button", { name: String(page) });
}

function ellipsisCount() {
  return screen.queryAllByText("…").length;
}

describe("buildPageItems", () => {
  it("totalPages ≤ capacity 时全部展示", () => {
    expect(buildPageItems(4, 7, 7)).toEqual([1, 2, 3, 4, 5, 6, 7]);
    expect(buildPageItems(1, 5, 11)).toEqual([1, 2, 3, 4, 5]);
  });

  it("capacity 低于下限时按 7 处理，中部为当前页 ±1", () => {
    expect(buildPageItems(5, 10, 3)).toEqual([1, "ellipsis", 4, 5, 6, "ellipsis", 10]);
  });

  it("大容量时中间窗口更宽并以当前页为中心", () => {
    // capacity=11 → 首末 2 + 省略号至多 2 + 中间 7
    expect(buildPageItems(10, 20, 11)).toEqual(
      [1, "ellipsis", 7, 8, 9, 10, 11, 12, 13, "ellipsis", 20],
    );
  });

  it("当前页靠前时窗口贴向首页，只保留右侧省略号", () => {
    expect(buildPageItems(1, 10, 7)).toEqual([1, 2, 3, 4, 5, "ellipsis", 10]);
    expect(buildPageItems(2, 20, 11)).toEqual(
      [1, 2, 3, 4, 5, 6, 7, 8, 9, "ellipsis", 20],
    );
  });

  it("当前页靠后时窗口贴向末页，只保留左侧省略号", () => {
    expect(buildPageItems(10, 10, 7)).toEqual([1, "ellipsis", 6, 7, 8, 9, 10]);
    expect(buildPageItems(19, 20, 11)).toEqual(
      [1, "ellipsis", 12, 13, 14, 15, 16, 17, 18, 19, 20],
    );
  });

  it("页码项数量不超过 capacity", () => {
    const items = buildPageItems(10, 50, 11);
    expect(items.length).toBeLessThanOrEqual(11);
  });
});

describe("PaginationControls", () => {
  const originalResizeObserver = global.ResizeObserver;

  afterEach(() => {
    global.ResizeObserver = originalResizeObserver;
  });

  it("无 ResizeObserver 时回退 capacity=7", () => {
    // @ts-expect-error 模拟旧浏览器 / jsdom 缺 ResizeObserver
    delete global.ResizeObserver;
    renderControls({ totalFiles: 200, pageSize: 10, currentPage: 10 });
    [1, 9, 10, 11, 20].forEach((p) => expect(pageButton(p)).toBeInTheDocument());
    [8, 12].forEach((p) => expect(pageButton(p)).not.toBeInTheDocument());
    expect(ellipsisCount()).toBe(2);
  });

  it("总页数 ≤ 7 时全部展示页码，无省略号", () => {
    renderControls({ totalFiles: 70, pageSize: 10, currentPage: 4 });
    for (let p = 1; p <= 7; p += 1) {
      expect(pageButton(p)).toBeInTheDocument();
    }
    expect(ellipsisCount()).toBe(0);
  });

  it("总页数 > 7 且当前页在中部：首尾固定 + 当前页前后各 1 页 + 两侧省略号", () => {
    renderControls({ totalFiles: 100, pageSize: 10, currentPage: 5 });
    [1, 4, 5, 6, 10].forEach((p) => expect(pageButton(p)).toBeInTheDocument());
    [2, 3, 7, 8, 9].forEach((p) => expect(pageButton(p)).not.toBeInTheDocument());
    expect(ellipsisCount()).toBe(2);
  });

  it("当前页为首页：窗口贴左，仅右侧省略号", () => {
    renderControls({ totalFiles: 100, pageSize: 10, currentPage: 1 });
    [1, 2, 3, 4, 5, 10].forEach((p) => expect(pageButton(p)).toBeInTheDocument());
    [6, 9].forEach((p) => expect(pageButton(p)).not.toBeInTheDocument());
    expect(ellipsisCount()).toBe(1);
  });

  it("当前页为末页：窗口贴右，仅左侧省略号", () => {
    renderControls({ totalFiles: 100, pageSize: 10, currentPage: 10 });
    [1, 6, 7, 8, 9, 10].forEach((p) => expect(pageButton(p)).toBeInTheDocument());
    [2, 5].forEach((p) => expect(pageButton(p)).not.toBeInTheDocument());
    expect(ellipsisCount()).toBe(1);
  });

  it("ResizeObserver 回调会按容器宽度重算页码窗口", () => {
    type ObserverCallback = (entries: Array<{ target: Element }>) => void;
    const instances: Array<{ callback: ObserverCallback; observe: jest.Mock; disconnect: jest.Mock }> = [];

    class MockResizeObserver {
      observe: jest.Mock;
      disconnect: jest.Mock;
      constructor(callback: ObserverCallback) {
        this.observe = jest.fn();
        this.disconnect = jest.fn();
        instances.push({ callback, observe: this.observe, disconnect: this.disconnect });
      }
    }

    global.ResizeObserver = MockResizeObserver as unknown as typeof ResizeObserver;

    const { renderResult } = renderControls({
      totalFiles: 200,
      pageSize: 10,
      currentPage: 10,
    });

    expect(instances).toHaveLength(1);
    expect(instances[0].observe).toHaveBeenCalled();
    [1, 9, 10, 11, 20].forEach((p) => expect(pageButton(p)).toBeInTheDocument());
    expect(pageButton(8)).not.toBeInTheDocument();

    const container = renderResult.container.querySelector(".pagination-controls") as HTMLDivElement;
    Object.defineProperty(container, "clientWidth", { configurable: true, value: 432 });
    Object.defineProperty(HTMLElement.prototype, "offsetWidth", {
      configurable: true,
      get() {
        return 0;
      },
    });

    try {
      act(() => {
        instances[0].callback([{ target: container }]);
      });
      expect(measurePageCapacity(container)).toBe(11);
      [1, 7, 8, 9, 10, 11, 12, 13, 20].forEach((p) => expect(pageButton(p)).toBeInTheDocument());
      [6, 14].forEach((p) => expect(pageButton(p)).not.toBeInTheDocument());
      expect(ellipsisCount()).toBe(2);
    } finally {
      delete (HTMLElement.prototype as { offsetWidth?: unknown }).offsetWidth;
    }
  });

  it("首页/末页直达按钮位于 ‹ › 外侧，边界 disabled", () => {
    const mid = renderControls({
      totalFiles: 100,
      pageSize: 10,
      currentPage: 5,
    });
    const { onPageChange } = mid;
    const first = screen.getByRole("button", { name: "首页" });
    const last = screen.getByRole("button", { name: "末页" });
    expect(first).toBeEnabled();
    expect(last).toBeEnabled();
    fireEvent.click(first);
    expect(onPageChange).toHaveBeenCalledWith(1);
    fireEvent.click(last);
    expect(onPageChange).toHaveBeenCalledWith(10);
    mid.renderResult.unmount();

    const head = renderControls({ totalFiles: 100, pageSize: 10, currentPage: 1 });
    expect(screen.getByRole("button", { name: "首页" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "末页" })).toBeEnabled();
    head.renderResult.unmount();

    renderControls({ totalFiles: 100, pageSize: 10, currentPage: 10 });
    expect(screen.getByRole("button", { name: "首页" })).toBeEnabled();
    expect(screen.getByRole("button", { name: "末页" })).toBeDisabled();
  });

  it("跳页输入：回车提交有效页码并清空输入", () => {
    const { onPageChange } = renderControls({
      totalFiles: 100,
      pageSize: 10,
      currentPage: 1,
    });
    const input = screen.getByLabelText("跳至第几页") as HTMLInputElement;
    fireEvent.change(input, { target: { value: "7" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onPageChange).toHaveBeenCalledWith(7);
    expect(input.value).toBe("");
  });

  it("跳页输入：越界钳制到 [1, totalPages]", () => {
    const { onPageChange } = renderControls({
      totalFiles: 100,
      pageSize: 10,
      currentPage: 3,
    });
    const input = screen.getByLabelText("跳至第几页");
    fireEvent.change(input, { target: { value: "99" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onPageChange).toHaveBeenCalledWith(10);

    fireEvent.change(input, { target: { value: "-5" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onPageChange).toHaveBeenCalledWith(1);
  });

  it("跳页输入：非数字或空值忽略，不触发翻页", () => {
    const { onPageChange } = renderControls({
      totalFiles: 100,
      pageSize: 10,
      currentPage: 2,
    });
    const input = screen.getByLabelText("跳至第几页");
    fireEvent.change(input, { target: { value: "" } });
    fireEvent.keyDown(input, { key: "Enter" });
    fireEvent.change(input, { target: { value: "abc" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onPageChange).not.toHaveBeenCalled();
  });

  it("跳页输入：失焦提交", () => {
    const { onPageChange } = renderControls({
      totalFiles: 100,
      pageSize: 10,
      currentPage: 1,
    });
    const input = screen.getByLabelText("跳至第几页");
    fireEvent.change(input, { target: { value: "4" } });
    fireEvent.blur(input);
    expect(onPageChange).toHaveBeenCalledWith(4);
  });

  it("跳页输入：跳转到当前页不触发回调", () => {
    const { onPageChange } = renderControls({
      totalFiles: 100,
      pageSize: 10,
      currentPage: 3,
    });
    const input = screen.getByLabelText("跳至第几页");
    fireEvent.change(input, { target: { value: "3" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onPageChange).not.toHaveBeenCalled();
  });

  it("未传 onPageSizeChange 时隐藏每页条数 select", () => {
    renderControls({ totalFiles: 30, withPageSize: false });
    expect(screen.queryByLabelText("每页条数")).not.toBeInTheDocument();
    expect(screen.getByText("共 30 项")).toBeInTheDocument();
  });

  it("传入 onPageSizeChange 时展示 select 并回调", () => {
    const { onPageSizeChange, onPageChange } = renderControls({
      totalFiles: 30,
      currentPage: 2,
    });
    fireEvent.change(screen.getByLabelText("每页条数"), { target: { value: "20" } });
    expect(onPageSizeChange).toHaveBeenCalledWith(20);
    expect(onPageChange).toHaveBeenCalledWith(1);
  });

  it("MIN_PAGE_CAPACITY 保持 7", () => {
    expect(MIN_PAGE_CAPACITY).toBe(7);
  });
});
