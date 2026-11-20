import { fireEvent, render, screen } from "@testing-library/react";

import { FileToolbar, type BrowseContext } from "@/features/files/components/FileToolbar";
import type { BrowseItemView } from "@/features/files/model";

const BROWSE: BrowseContext = { fileId: 7, fileName: "素材", path: ["2026", "11月"] };
const ITEMS: BrowseItemView[] = [
  { name: "a.png", size: 100, isDirectory: false },
  { name: "子目录", size: 0, isDirectory: true },
];

function baseProps() {
  return {
    isInsideFolder: false,
    browseContext: null as BrowseContext | null,
    browseContents: [] as BrowseItemView[],
    selectedBrowseFiles: new Set<string>(),
    selectedFiles: new Set<number>(),
    selectedSize: 0,
    sortedFilesLength: 0,
    sortField: "created_at" as const,
    sortOrder: "desc" as const,
    toolbarSearchKeyword: "",
    searchGlobal: false,
    searchLoading: false,
    isBatchOperating: false,
    onReturnToRoot: jest.fn(),
    onNavigateToBreadcrumb: jest.fn(),
    onSortFieldChange: jest.fn(),
    onSortOrderChange: jest.fn(),
    onToolbarSearchKeywordChange: jest.fn(),
    onToolbarSearchKeyDown: jest.fn(),
    onSearchGlobalChange: jest.fn(),
    onSearchSubmit: jest.fn(),
    onToggleAllBrowseFiles: jest.fn(),
    onBrowseBatchDownload: jest.fn(),
    onToggleSelectAll: jest.fn(),
    onBatchDownload: jest.fn(),
    onBatchDelete: jest.fn(),
    onOpenPackDialog: jest.fn(),
  };
}

describe("FileToolbar 面包屑", () => {
  it("根目录态只显示根目录标签，没有返回按钮", () => {
    const props = baseProps();
    render(<FileToolbar {...props} />);
    const crumb = document.querySelector(".path-breadcrumb");
    expect(crumb?.textContent).toContain("根目录");
    expect(screen.queryByRole("button", { name: /根目录/ })).not.toBeInTheDocument();
  });

  it("目录态显示层级、项目数与返回回调", () => {
    const props = baseProps();
    render(
      <FileToolbar
        {...props}
        isInsideFolder
        browseContext={BROWSE}
        browseContents={ITEMS}
      />,
    );
    const crumb = document.querySelector(".path-breadcrumb") as HTMLElement;
    expect(crumb.textContent).toContain("根目录");
    expect(crumb.textContent).toContain("素材");
    expect(crumb.textContent).toContain("2026");
    expect(crumb.textContent).toContain("11月");
    expect(crumb.textContent).toContain("(2 项)");

    fireEvent.click(screen.getByRole("button", { name: /根目录/ }));
    expect(props.onReturnToRoot).toHaveBeenCalled();

    // 文件夹名 → index -1（回到文件夹根）
    fireEvent.click(screen.getByRole("button", { name: "素材" }));
    expect(props.onNavigateToBreadcrumb).toHaveBeenCalledWith(-1);

    // 各级目录段 → 对应 index
    fireEvent.click(screen.getByRole("button", { name: "2026" }));
    expect(props.onNavigateToBreadcrumb).toHaveBeenCalledWith(0);
    fireEvent.click(screen.getByRole("button", { name: "11月" }));
    expect(props.onNavigateToBreadcrumb).toHaveBeenCalledWith(1);
  });
});

describe("FileToolbar 排序与搜索", () => {
  it("排序下拉把 field-order 拆给两个回调", () => {
    const props = baseProps();
    render(<FileToolbar {...props} />);
    fireEvent.change(screen.getByLabelText("排序方式"), { target: { value: "size-asc" } });
    expect(props.onSortFieldChange).toHaveBeenCalledWith("size");
    expect(props.onSortOrderChange).toHaveBeenCalledWith("asc");
  });

  it("搜索输入、清除、全局开关与提交", () => {
    const props = baseProps();
    const { rerender } = render(<FileToolbar {...props} />);
    fireEvent.change(screen.getByLabelText("搜索文件"), { target: { value: "报告" } });
    expect(props.onToolbarSearchKeywordChange).toHaveBeenCalledWith("报告");
    fireEvent.keyDown(screen.getByLabelText("搜索文件"), { key: "Enter" });
    expect(props.onToolbarSearchKeyDown).toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "查询" }));
    expect(props.onSearchSubmit).toHaveBeenCalled();
    fireEvent.click(screen.getByLabelText("全局"));
    expect(props.onSearchGlobalChange).toHaveBeenCalledWith(true);

    // 有关键词才出现清除按钮。
    expect(screen.queryByRole("button", { name: "清除搜索" })).not.toBeInTheDocument();
    rerender(<FileToolbar {...props} toolbarSearchKeyword="报告" />);
    fireEvent.click(screen.getByRole("button", { name: "清除搜索" }));
    expect(props.onToolbarSearchKeywordChange).toHaveBeenCalledWith("");
  });

  it("查询中禁用按钮并改变文案", () => {
    render(<FileToolbar {...baseProps()} searchLoading />);
    const button = screen.getByRole("button", { name: "查询中..." });
    expect(button).toBeDisabled();
  });
});

describe("FileToolbar 批量操作（根目录）", () => {
  it("未选中时只显示全选", () => {
    const props = baseProps();
    render(<FileToolbar {...props} sortedFilesLength={3} />);
    expect(screen.queryByText(/已选/)).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "全选" }));
    expect(props.onToggleSelectAll).toHaveBeenCalled();
  });

  it("已选中时显示数量、体积与四个操作", () => {
    const props = baseProps();
    render(
      <FileToolbar {...props} selectedFiles={new Set([1, 2])} selectedSize={3072} sortedFilesLength={5} />,
    );
    expect(screen.getByText("已选 2 项 (3.0 KB)")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "批量下载" }));
    expect(props.onBatchDownload).toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "批量删除" }));
    expect(props.onBatchDelete).toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "打包" }));
    expect(props.onOpenPackDialog).toHaveBeenCalled();
  });

  it("全部选中时全选按钮变为取消全选", () => {
    const props = baseProps();
    render(<FileToolbar {...props} selectedFiles={new Set([1, 2])} sortedFilesLength={2} />);
    fireEvent.click(screen.getByRole("button", { name: "取消全选" }));
    expect(props.onToggleSelectAll).toHaveBeenCalled();
  });

  it("批量操作进行中禁用按钮并显示删除中", () => {
    render(
      <FileToolbar
        {...baseProps()}
        selectedFiles={new Set([1])}
        sortedFilesLength={3}
        isBatchOperating
      />,
    );
    expect(screen.getByRole("button", { name: "批量下载" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "删除中..." })).toBeDisabled();
    expect(screen.getByRole("button", { name: "打包" })).toBeDisabled();
  });
});

describe("FileToolbar 批量操作（目录内）", () => {
  it("目录内已选：显示数量与批量下载", () => {
    const props = baseProps();
    render(
      <FileToolbar
        {...props}
        isInsideFolder
        browseContext={BROWSE}
        browseContents={ITEMS}
        selectedBrowseFiles={new Set(["2026/11月/a.png"])}
      />,
    );
    expect(screen.getByText("已选 1 项")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "批量下载" }));
    expect(props.onBrowseBatchDownload).toHaveBeenCalled();
    // 目录内不出现根目录专用按钮。
    expect(screen.queryByRole("button", { name: "批量删除" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "打包" })).not.toBeInTheDocument();
  });

  it("目录内全选/取消全选随已选集合切换", () => {
    const props = baseProps();
    const keys = new Set(["2026/11月/a.png", "2026/11月/子目录"]);
    const { rerender } = render(
      <FileToolbar {...props} isInsideFolder browseContext={BROWSE} browseContents={ITEMS} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "全选" }));
    expect(props.onToggleAllBrowseFiles).toHaveBeenCalled();

    rerender(
      <FileToolbar
        {...props}
        isInsideFolder
        browseContext={BROWSE}
        browseContents={ITEMS}
        selectedBrowseFiles={keys}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "取消全选" }));
    expect(props.onToggleAllBrowseFiles).toHaveBeenCalledTimes(2);
  });

  it("目录为空时不显示全选按钮", () => {
    render(<FileToolbar {...baseProps()} isInsideFolder browseContext={BROWSE} />);
    expect(screen.queryByRole("button", { name: "全选" })).not.toBeInTheDocument();
  });
});
