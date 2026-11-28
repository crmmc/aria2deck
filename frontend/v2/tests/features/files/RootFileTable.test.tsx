import { fireEvent, render, screen } from "@testing-library/react";

import { RootFileTable } from "@/features/files/components/RootFileTable";
import type { FileView } from "@/features/files/model";

function file(overrides: Partial<FileView> = {}): FileView {
  return {
    id: 1,
    name: "a.txt",
    size: 2048,
    isDirectory: false,
    createdAtMs: new Date(2026, 10, 19, 9, 5).getTime(),
    ...overrides,
  };
}

const DIR = file({ id: 2, name: "素材", size: 0, isDirectory: true, createdAtMs: 2 });

function baseProps() {
  return {
    isMobile: false,
    loading: false,
    error: null as string | null,
    sortedFiles: [file(), DIR],
    selectedFiles: new Set<number>(),
    renaming: null as number | null,
    newName: "",
    downloadingFile: null as number | null,
    highlightUserFileId: null as number | null,
    onSort: jest.fn(),
    getSortIcon: jest.fn(() => "none" as const),
    onToggleSelectAll: jest.fn(),
    onToggleFileSelection: jest.fn(),
    onSelectionKeyDown: jest.fn(),
    onNewNameChange: jest.fn(),
    onRename: jest.fn(),
    onCancelRename: jest.fn(),
    onStartRename: jest.fn(),
    onEnterFolder: jest.fn(),
    onDownload: jest.fn(),
    onShare: jest.fn(),
    onDelete: jest.fn(),
    formatDate: jest.fn(() => "2026/11/19 09:05"),
  };
}

describe("RootFileTable 状态分支", () => {
  it("加载中显示占位", () => {
    render(<RootFileTable {...baseProps()} loading />);
    expect(screen.getByText("加载中...")).toBeInTheDocument();
  });

  it("错误优先于空列表显示", () => {
    render(<RootFileTable {...baseProps()} error="请求失败" sortedFiles={[]} />);
    expect(screen.getByText("请求失败")).toBeInTheDocument();
  });

  it("空列表显示空状态", () => {
    render(<RootFileTable {...baseProps()} sortedFiles={[]} />);
    expect(screen.getByText("暂无文件")).toBeInTheDocument();
    expect(screen.getByText("下载完成的文件将显示在这里")).toBeInTheDocument();
  });
});

describe("RootFileTable 桌面表格", () => {
  it("渲染文件名、大小、时间，并对表头排序回调", () => {
    const props = baseProps();
    render(<RootFileTable {...props} />);
    expect(screen.getByText("a.txt")).toBeInTheDocument();
    expect(screen.getByText("2.0 KB")).toBeInTheDocument();
    expect(screen.getAllByText("2026/11/19 09:05")).toHaveLength(2);
    expect(document.querySelector(".file-table-head")?.textContent).toContain("操作");
    expect(document.querySelectorAll(".file-row")).toHaveLength(2);

    fireEvent.click(screen.getByRole("button", { name: /名称/ }));
    expect(props.onSort).toHaveBeenCalledWith("name");
    fireEvent.click(screen.getByRole("button", { name: /大小/ }));
    expect(props.onSort).toHaveBeenCalledWith("size");
    fireEvent.click(screen.getByRole("button", { name: /添加时间/ }));
    expect(props.onSort).toHaveBeenCalledWith("created_at");
  });

  it("全选框：部分选中时不勾选，全选后勾选", () => {
    const props = baseProps();
    const { rerender } = render(<RootFileTable {...props} />);
    const selectAll = screen.getByLabelText("全选") as HTMLInputElement;
    expect(selectAll.checked).toBe(false);
    fireEvent.click(selectAll);
    expect(props.onToggleSelectAll).toHaveBeenCalled();

    rerender(<RootFileTable {...props} selectedFiles={new Set([1, 2])} />);
    expect((screen.getByLabelText("全选") as HTMLInputElement).checked).toBe(true);
  });

  it("行选择框反映选中集合并回调文件 id", () => {
    const props = baseProps();
    const { rerender } = render(<RootFileTable {...props} selectedFiles={new Set([1])} />);
    const boxes = screen.getAllByLabelText("选择文件") as HTMLInputElement[];
    expect(boxes[0].checked).toBe(true);
    expect(boxes[1].checked).toBe(false);
    fireEvent.click(boxes[1]);
    expect(props.onToggleFileSelection).toHaveBeenCalledWith(2, false);

    rerender(<RootFileTable {...props} selectedFiles={new Set()} />);
    expect((screen.getAllByLabelText("选择文件") as HTMLInputElement[])[0].checked).toBe(false);
  });

  it("目录行显示浏览按钮，文件行显示下载按钮", () => {
    const props = baseProps();
    render(<RootFileTable {...props} />);
    fireEvent.click(screen.getAllByRole("button", { name: "浏览" })[0]);
    expect(props.onEnterFolder).toHaveBeenCalledWith(DIR);
    // 目录名本身也是进入文件夹的入口。
    fireEvent.click(screen.getByRole("button", { name: "素材" }));
    expect(props.onEnterFolder).toHaveBeenCalledTimes(2);
    fireEvent.click(screen.getAllByRole("button", { name: "下载" })[0]);
    expect(props.onDownload).toHaveBeenCalledWith(file());
  });

  it("下载中禁用按钮并改变文案", () => {
    render(<RootFileTable {...baseProps()} downloadingFile={1} />);
    const button = screen.getByRole("button", { name: "下载中..." });
    expect(button).toBeDisabled();
  });

  it("重命名进入编辑态：回车确认、Esc 取消、按钮也生效", () => {
    const props = baseProps();
    const { rerender } = render(<RootFileTable {...props} renaming={1} newName="新名字.txt" />);

    const input = screen.getByLabelText("重命名文件") as HTMLInputElement;
    expect(input.value).toBe("新名字.txt");
    // 输入框挂载即聚焦。
    expect(document.activeElement).toBe(input);

    fireEvent.change(input, { target: { value: "改成这个.txt" } });
    expect(props.onNewNameChange).toHaveBeenCalledWith("改成这个.txt");

    fireEvent.keyDown(input, { key: "Enter" });
    expect(props.onRename).toHaveBeenCalledWith(file());
    fireEvent.keyDown(input, { key: "Escape" });
    expect(props.onCancelRename).toHaveBeenCalledTimes(1);
    fireEvent.keyDown(input, { key: "a" });
    expect(props.onRename).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: "确认重命名" }));
    expect(props.onRename).toHaveBeenCalledTimes(2);
    fireEvent.click(screen.getByRole("button", { name: "取消重命名" }));
    expect(props.onCancelRename).toHaveBeenCalledTimes(2);

    // 点击输入框不应冒泡到行/卡片。
    const rowClick = jest.fn();
    rerender(
      <div onClick={rowClick}>
        <RootFileTable {...props} renaming={1} newName="x" />
      </div>,
    );
    fireEvent.click(screen.getByLabelText("重命名文件"));
    expect(rowClick).not.toHaveBeenCalled();
  });

  it("行内操作按钮分别回调分享、删除、开始重命名", () => {
    const props = baseProps();
    render(<RootFileTable {...props} />);
    fireEvent.click(screen.getAllByRole("button", { name: "分享" })[0]);
    expect(props.onShare).toHaveBeenCalledWith(file());
    fireEvent.click(screen.getAllByRole("button", { name: "删除" })[0]);
    expect(props.onDelete).toHaveBeenCalledWith(file());
    fireEvent.click(screen.getAllByRole("button", { name: "重命名" })[0]);
    expect(props.onStartRename).toHaveBeenCalledWith(file());
  });

  it("高亮命中行带 data-file-id 与 aria-current", () => {
    render(<RootFileTable {...baseProps()} highlightUserFileId={2} />);
    const highlighted = document.querySelector('[data-file-id="2"]');
    expect(highlighted?.className).toContain("file-locate-highlight");
    expect(highlighted?.getAttribute("aria-current")).toBe("true");
    expect(document.querySelector('[data-file-id="1"]')?.getAttribute("aria-current")).toBeNull();
  });
});

describe("RootFileTable 多选交互", () => {
  it("桌面行复选框带连续 data-select-index，shift+点击把 shiftKey 传给上层", () => {
    const props = baseProps();
    render(<RootFileTable {...props} />);
    const boxes = screen.getAllByLabelText("选择文件") as HTMLInputElement[];
    expect(boxes.map((box) => box.dataset.selectIndex)).toEqual(["0", "1"]);

    fireEvent.click(boxes[0], { shiftKey: true });
    expect(props.onToggleFileSelection).toHaveBeenCalledWith(1, true);
  });

  it("列表容器把键盘事件交给 onSelectionKeyDown", () => {
    const props = baseProps();
    render(<RootFileTable {...props} />);

    fireEvent.keyDown(screen.getByTestId("file-list"), { key: "a", ctrlKey: true });
    expect(props.onSelectionKeyDown).toHaveBeenCalledTimes(1);
  });

  it("移动卡片同样带 data-select-index 并传递 shiftKey", () => {
    const props = baseProps();
    render(<RootFileTable {...props} isMobile />);
    const boxes = screen.getAllByLabelText(/^选择 /) as HTMLInputElement[];
    expect(boxes.map((box) => box.dataset.selectIndex)).toEqual(["0", "1"]);

    fireEvent.click(boxes[1], { shiftKey: true });
    expect(props.onToggleFileSelection).toHaveBeenCalledWith(2, true);
  });
});

describe("RootFileTable 移动卡片", () => {
  it("渲染卡片：大小与时间、浏览/重命名/分享/删除", () => {
    const props = baseProps();
    render(<RootFileTable {...props} isMobile />);
    expect(document.querySelectorAll(".mobile-file-card")).toHaveLength(2);
    expect(screen.getByText(/2\.0 KB.*2026\/11\/19 09:05/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "浏览" }));
    expect(props.onEnterFolder).toHaveBeenCalledWith(DIR);
    fireEvent.click(screen.getAllByRole("button", { name: "重命名" })[0]);
    expect(props.onStartRename).toHaveBeenCalledWith(file());
    fireEvent.click(screen.getAllByRole("button", { name: "分享" })[0]);
    expect(props.onShare).toHaveBeenCalledWith(file());
    fireEvent.click(screen.getAllByRole("button", { name: "删除" })[0]);
    expect(props.onDelete).toHaveBeenCalledWith(file());
  });

  it("目录名可点击进入文件夹，文件名不可点击", () => {
    const props = baseProps();
    render(<RootFileTable {...props} isMobile />);
    fireEvent.click(screen.getByRole("button", { name: "素材" }));
    expect(props.onEnterFolder).toHaveBeenCalledWith(DIR);
    expect(screen.queryByRole("button", { name: "a.txt" })).not.toBeInTheDocument();
  });

  it("移动端下载中禁用并改变文案", () => {
    // 列表里只有目录时没有可下载行，这里用两条文件行观察禁用/可用并存。
    const other = file({ id: 3, name: "b.txt", size: 10 });
    const props = baseProps();
    render(
      <RootFileTable {...props} isMobile sortedFiles={[file(), other]} downloadingFile={1} />,
    );
    expect(screen.getByRole("button", { name: "下载中..." })).toBeDisabled();
    const available = screen.getByRole("button", { name: "下载" });
    expect(available).toBeEnabled();
    fireEvent.click(available);
    expect(props.onDownload).toHaveBeenCalledWith(other);
  });

  it("移动端选择框回调文件 id", () => {
    const props = baseProps();
    render(<RootFileTable {...props} isMobile selectedFiles={new Set([2])} />);
    const boxes = screen.getAllByLabelText(/选择 /) as HTMLInputElement[];
    expect(boxes[0].checked).toBe(false);
    expect(boxes[1].checked).toBe(true);
    fireEvent.click(boxes[0]);
    expect(props.onToggleFileSelection).toHaveBeenCalledWith(1, false);
  });

  it("移动端重命名编辑态使用紧凑布局", () => {
    const props = baseProps();
    render(<RootFileTable {...props} isMobile renaming={1} newName="x.txt" />);
    const input = screen.getByLabelText("重命名文件") as HTMLInputElement;
    expect(input.value).toBe("x.txt");
    fireEvent.keyDown(input, { key: "Enter" });
    expect(props.onRename).toHaveBeenCalledWith(file());
  });

  it("移动端高亮卡片带 aria-current", () => {
    render(<RootFileTable {...baseProps()} isMobile highlightUserFileId={1} />);
    const cards = document.querySelectorAll(".mobile-file-card");
    expect(cards[0].getAttribute("aria-current")).toBe("true");
    expect(cards[0].className).toContain("file-locate-highlight");
    expect(cards[1].getAttribute("aria-current")).toBeNull();
  });
});

describe("RootFileTable 类型列", () => {
  it("渲染类型单元格并支持类型排序", () => {
    const props = baseProps();
    render(<RootFileTable {...props} />);
    expect(screen.getByText("txt")).toBeInTheDocument();
    const typeCells = [...document.querySelectorAll(".file-row .file-type")].map((el) => el.textContent);
    expect(typeCells).toEqual(["txt", "文件夹"]);
    fireEvent.click(screen.getByRole("button", { name: /类型/ }));
    expect(props.onSort).toHaveBeenCalledWith("type");
  });

  it("筛选至空时显示专属空态并可清除筛选", () => {
    const onClearFilter = jest.fn();
    render(<RootFileTable {...baseProps()} sortedFiles={[]} filteredEmpty onClearFilter={onClearFilter} />);
    expect(screen.getByText("当前页没有符合筛选条件的文件")).toBeInTheDocument();
    expect(screen.queryByText("暂无文件")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "清除筛选" }));
    expect(onClearFilter).toHaveBeenCalled();
  });
});

describe("RootFileTable 列宽调节", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  it("默认列宽经 CSS 自定义属性注入", () => {
    render(<RootFileTable {...baseProps()} />);
    const wrapper = screen.getByTestId("file-list");
    expect(wrapper.style.getPropertyValue("--file-cols-type")).toBe("84px");
    expect(wrapper.style.getPropertyValue("--file-cols-size")).toBe("92px");
    expect(wrapper.style.getPropertyValue("--file-cols-date")).toBe("146px");
    expect(wrapper.style.getPropertyValue("--file-cols-ops")).toBe("122px");
  });

  it("读取持久化列宽并钳制越界值", () => {
    window.localStorage.setItem("files.column_widths", JSON.stringify({ type: 120, size: 999 }));
    render(<RootFileTable {...baseProps()} />);
    const wrapper = screen.getByTestId("file-list");
    expect(wrapper.style.getPropertyValue("--file-cols-type")).toBe("120px");
    expect(wrapper.style.getPropertyValue("--file-cols-size")).toBe("200px");
  });

  it("损坏的本地数据回退默认值", () => {
    window.localStorage.setItem("files.column_widths", "not-json");
    render(<RootFileTable {...baseProps()} />);
    expect(screen.getByTestId("file-list").style.getPropertyValue("--file-cols-type")).toBe("84px");
  });

  it("拖拽更新预览、越界钳制、抬起后持久化", () => {
    render(<RootFileTable {...baseProps()} />);
    const wrapper = screen.getByTestId("file-list");
    const handle = screen.getByRole("separator", { name: "调整类型列宽" });
    // jsdom 无 PointerEvent 构造器，用 MouseEvent 携带 clientX 模拟 pointer 序列
    const pointer = (type: string, clientX: number) => new MouseEvent(type, { bubbles: true, cancelable: true, clientX });
    fireEvent(handle, pointer("pointerdown", 100));
    expect(document.body.classList.contains("col-resizing")).toBe(true);
    fireEvent(handle, pointer("pointermove", 130));
    expect(wrapper.style.getPropertyValue("--file-cols-type")).toBe("114px");
    fireEvent(handle, pointer("pointermove", 600));
    expect(wrapper.style.getPropertyValue("--file-cols-type")).toBe("200px");
    fireEvent(handle, pointer("pointerup", 600));
    expect(document.body.classList.contains("col-resizing")).toBe(false);
    expect(JSON.parse(window.localStorage.getItem("files.column_widths")!).type).toBe(200);
  });

  it("双击手柄恢复该列默认宽度", () => {
    window.localStorage.setItem("files.column_widths", JSON.stringify({ type: 120 }));
    render(<RootFileTable {...baseProps()} />);
    const wrapper = screen.getByTestId("file-list");
    fireEvent.doubleClick(screen.getByRole("separator", { name: "调整类型列宽" }));
    expect(wrapper.style.getPropertyValue("--file-cols-type")).toBe("84px");
    expect(JSON.parse(window.localStorage.getItem("files.column_widths")!).type).toBe(84);
  });

  it("拖拽时不触发列头排序", () => {
    const props = baseProps();
    render(<RootFileTable {...props} />);
    const handle = screen.getByRole("separator", { name: "调整类型列宽" });
    const pointer = (type: string, clientX: number) => new MouseEvent(type, { bubbles: true, cancelable: true, clientX });
    fireEvent(handle, pointer("pointerdown", 100));
    fireEvent(handle, pointer("pointermove", 120));
    fireEvent(handle, pointer("pointerup", 120));
    fireEvent.click(handle);
    expect(props.onSort).not.toHaveBeenCalled();
  });

  it("未开始拖拽时移动与抬起均为空操作", () => {
    render(<RootFileTable {...baseProps()} />);
    const wrapper = screen.getByTestId("file-list");
    const handle = screen.getByRole("separator", { name: "调整类型列宽" });
    fireEvent(handle, new MouseEvent("pointermove", { bubbles: true, clientX: 500 }));
    fireEvent(handle, new MouseEvent("pointerup", { bubbles: true }));
    expect(wrapper.style.getPropertyValue("--file-cols-type")).toBe("84px");
    expect(document.body.classList.contains("col-resizing")).toBe(false);
  });
});
