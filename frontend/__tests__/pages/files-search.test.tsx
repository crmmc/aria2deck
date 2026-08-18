import { render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import FilesPage from "@/app/(authenticated)/files/page";
import { ToastProvider } from "@/components/Toast";
import { api } from "@/lib/api";
import type { FileInfo, BrowseFileInfo, FileListResponse, FileSearchItem, FileSearchResponse } from "@/types";
import type { ListProps } from "react-window";
import type { AutoSizerProps } from "react-virtualized-auto-sizer";

// Mock next/navigation
jest.mock("next/navigation", () => ({
  useRouter: () => ({ push: jest.fn() }),
}));

// Mock react-virtualized-auto-sizer — render children with fixed dimensions
jest.mock("react-virtualized-auto-sizer", () => ({
  __esModule: true,
  AutoSizer: ({ renderProp }: AutoSizerProps) => {
    if (!renderProp) return null;
    return renderProp({ height: 600, width: 1200 });
  },
}));

// Mock react-window — render all rows without virtualization
jest.mock("react-window", () => ({
  List: ({ rowCount, rowComponent: Row, rowProps, style }: ListProps<Record<string, unknown>>) => (
    <div style={style} data-testid="virtual-list">
      {Array.from({ length: rowCount }, (_, i) => (
        <Row key={i} index={i} style={{}} {...rowProps} />
      ))}
    </div>
  ),
}));

// Mock api module
jest.mock("@/lib/api", () => ({
  __esModule: true,
  api: {
    listFiles: jest.fn(),
    browseFile: jest.fn(),
    searchFiles: jest.fn(),
    downloadFileUrl: jest.fn(),
    deleteFile: jest.fn(),
    calculatePackSize: jest.fn(),
    getAvailableSpace: jest.fn(),
    createPackTask: jest.fn(),
    renameFile: jest.fn(),
  },
  authEvents: {
    listeners: new Set(),
    onUnauthorized: jest.fn().mockReturnValue(jest.fn()),
    emit: jest.fn(),
  },
  ApiError: class ApiError extends Error {},
}));

const mockApi = api as jest.Mocked<typeof api>;

const folderFile: FileInfo = {
  id: 1,
  content_hash: "hash_folder",
  name: "MyFolder",
  size: 0,
  is_directory: true,
  created_at: "2024-01-01T00:00:00",
};

const regularFile: FileInfo = {
  id: 2,
  content_hash: "hash_readme",
  name: "readme.txt",
  size: 1024,
  is_directory: false,
  created_at: "2024-01-02T00:00:00",
};

const targetFile: FileInfo = {
  id: 99,
  content_hash: "hash_target",
  name: "target-report.pdf",
  size: 2048,
  is_directory: false,
  created_at: "2024-03-01T00:00:00",
};

const browseItems: BrowseFileInfo[] = [
  { name: "file1.txt", size: 100, is_directory: false },
];

const topSearchItem: FileSearchItem = {
  user_file_id: regularFile.id,
  content_hash: regularFile.content_hash,
  name: "readme.txt",
  size: 1024,
  path: "readme.txt",
  is_directory: false,
  entry_path: null,
  rank: 0,
  root_index: 1,
};

const innerSearchItem: FileSearchItem = {
  user_file_id: 3,
  content_hash: "hash_folder",
  name: "inner.txt",
  size: 50,
  path: "/MyFolder/sub/inner.txt",
  is_directory: false,
  entry_path: "sub/inner.txt",
  rank: 1,
  root_index: 0,
};

function setupListFiles(files: FileInfo[] = [folderFile, regularFile], total = files.length) {
  mockApi.listFiles.mockResolvedValue({
    files,
    total,
    space: { used: 1024, frozen: 0, available: 9216 },
  } satisfies FileListResponse);
}

async function renderAndWait() {
  setupListFiles();
  render(
    <ToastProvider>
      <FilesPage />
    </ToastProvider>
  );
  await waitFor(() => {
    expect(screen.getByText("MyFolder")).toBeInTheDocument();
  });
}

function getSearchInput() {
  return screen.getByRole("textbox", { name: "搜索文件" });
}

/** Type a keyword and press Enter, then wait for the result dialog */
async function runQuery(keyword: string) {
  const input = getSearchInput();
  fireEvent.change(input, { target: { value: keyword } });
  fireEvent.keyDown(input, { key: "Enter" });
  return screen.findByRole("dialog", { name: "搜索结果" });
}

beforeEach(() => {
  jest.clearAllMocks();
  jest.spyOn(console, "error").mockImplementation(() => {});
});

afterEach(() => {
  (console.error as jest.Mock).mockRestore?.();
});

describe("Files page search", () => {
  test("T13: typing in the search input does not call searchFiles", async () => {
    await renderAndWait();

    fireEvent.change(getSearchInput(), { target: { value: "readme" } });

    expect(mockApi.searchFiles).not.toHaveBeenCalled();
  });

  test("T14: Enter queries searchFiles once and keeps the main table untouched", async () => {
    mockApi.searchFiles.mockResolvedValue({
      items: [topSearchItem],
      total: 1,
      truncated: false,
    } satisfies FileSearchResponse);

    await renderAndWait();
    await runQuery("readme");

    await waitFor(() => {
      expect(mockApi.searchFiles).toHaveBeenCalledTimes(1);
    });
    expect(mockApi.searchFiles).toHaveBeenCalledWith({ q: "readme" });
    expect(mockApi.listFiles).toHaveBeenCalledTimes(1);
    // Main table still shows the original listFiles result
    expect(document.querySelector(".file-table-wrapper")?.textContent).toContain("MyFolder");
    expect(document.querySelector(".file-table-wrapper")?.textContent).toContain("readme.txt");
  });

  test("T15: empty keyword does not fetch and shows a hint", async () => {
    await renderAndWait();

    fireEvent.change(getSearchInput(), { target: { value: "   " } });
    fireEvent.click(screen.getByRole("button", { name: "查询" }));

    expect(mockApi.searchFiles).not.toHaveBeenCalled();
    expect(await screen.findByText(/请输入关键词/)).toBeInTheDocument();
  });

  test("T16: empty results show the no-match hint while the main table keeps files", async () => {
    mockApi.searchFiles.mockResolvedValue({
      items: [],
      total: 0,
      truncated: false,
    } satisfies FileSearchResponse);

    await renderAndWait();
    await runQuery("nothing");

    expect(await screen.findByText("未找到匹配的文件")).toBeInTheDocument();
    expect(screen.getByText("MyFolder")).toBeInTheDocument();
    expect(screen.getByText("readme.txt")).toBeInTheDocument();
  });

  test("T17: result rows show name, size, path and locate only — no file actions", async () => {
    mockApi.searchFiles.mockResolvedValue({
      items: [innerSearchItem],
      total: 1,
      truncated: false,
    } satisfies FileSearchResponse);

    await renderAndWait();
    const dialog = await runQuery("inner");

    expect(within(dialog).getByText("inner.txt")).toBeInTheDocument();
    expect(await within(dialog).findByText("50.0 B")).toBeInTheDocument();
    expect(within(dialog).getByText("/MyFolder/sub/inner.txt")).toBeInTheDocument();
    expect(within(dialog).getByRole("button", { name: "定位" })).toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: "下载" })).not.toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: "重命名" })).not.toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: "分享" })).not.toBeInTheDocument();
    expect(within(dialog).queryByRole("button", { name: "删除" })).not.toBeInTheDocument();
  });

  test("T18: locating a top-level item loads its page, closes the dialog and highlights the row", async () => {
    mockApi.searchFiles.mockResolvedValue({
      items: [
        {
          ...topSearchItem,
          user_file_id: targetFile.id,
          content_hash: targetFile.content_hash,
          name: targetFile.name,
          size: targetFile.size,
          path: targetFile.name,
          root_index: 10,
        },
      ],
      total: 1,
      truncated: false,
    } satisfies FileSearchResponse);

    await renderAndWait();
    const dialog = await runQuery("target");

    // Page 1 (already loaded) does not contain the target; page 2 does
    setupListFiles([targetFile], 11);

    fireEvent.click(within(dialog).getByRole("button", { name: "定位" }));

    await waitFor(() => {
      expect(mockApi.listFiles).toHaveBeenLastCalledWith(2, 10);
    });
    await waitFor(() => {
      expect(screen.queryByRole("dialog", { name: "搜索结果" })).not.toBeInTheDocument();
    });
    await waitFor(() => {
      const highlighted = document.querySelector(".file-locate-highlight");
      expect(highlighted).not.toBeNull();
      expect(highlighted?.textContent).toContain("target-report.pdf");
    });
  });

  test("T19: locating an inner item browses to its parent folder path", async () => {
    mockApi.searchFiles.mockResolvedValue({
      items: [innerSearchItem],
      total: 1,
      truncated: false,
    } satisfies FileSearchResponse);
    mockApi.browseFile.mockResolvedValue([
      { name: "inner.txt", size: 50, is_directory: false },
    ]);

    await renderAndWait();
    const dialog = await runQuery("inner");

    fireEvent.click(within(dialog).getByRole("button", { name: "定位" }));

    await waitFor(() => {
      expect(mockApi.browseFile).toHaveBeenCalledWith("hash_folder", "sub");
    });
    await waitFor(() => {
      expect(screen.queryByRole("dialog", { name: "搜索结果" })).not.toBeInTheDocument();
    });
    // 面包屑根名取 path 首段（MyFolder），而非文件名
    await waitFor(() => {
      expect(screen.getByRole("button", { name: "MyFolder" })).toBeInTheDocument();
    });
    const highlighted = document.querySelector(".file-locate-highlight");
    expect(highlighted?.textContent).toContain("inner.txt");
  });

  test("T20: browse failure toasts an error and keeps the result dialog open", async () => {
    mockApi.searchFiles.mockResolvedValue({
      items: [innerSearchItem],
      total: 1,
      truncated: false,
    } satisfies FileSearchResponse);
    mockApi.browseFile.mockRejectedValue(new Error("无法读取文件夹"));

    await renderAndWait();
    const dialog = await runQuery("inner");

    fireEvent.click(within(dialog).getByRole("button", { name: "定位" }));

    expect(await screen.findByText(/定位失败/)).toBeInTheDocument();
    const stillOpen = screen.getByRole("dialog", { name: "搜索结果" });
    expect(within(stillOpen).getByRole("button", { name: "定位" })).toBeInTheDocument();
  });

  test("T21: at root, both non-global and global queries omit scopeContentHash", async () => {
    mockApi.searchFiles.mockResolvedValue({
      items: [],
      total: 0,
      truncated: false,
    } satisfies FileSearchResponse);

    await renderAndWait();
    await runQuery("readme");

    fireEvent.click(screen.getByLabelText("全局"));
    fireEvent.keyDown(getSearchInput(), { key: "Enter" });

    await waitFor(() => {
      expect(mockApi.searchFiles).toHaveBeenCalledTimes(2);
    });
    expect(mockApi.searchFiles).toHaveBeenNthCalledWith(1, { q: "readme" });
    expect(mockApi.searchFiles).toHaveBeenNthCalledWith(2, { q: "readme" });
  });

  test("inside a folder the query button sends the folder hash as scope", async () => {
    mockApi.searchFiles.mockResolvedValue({
      items: [],
      total: 0,
      truncated: false,
    } satisfies FileSearchResponse);
    mockApi.browseFile.mockResolvedValue(browseItems);

    await renderAndWait();
    fireEvent.click(screen.getByRole("button", { name: "MyFolder" }));
    await waitFor(() => {
      expect(screen.getByText("file1.txt")).toBeInTheDocument();
    });

    fireEvent.change(getSearchInput(), { target: { value: "file1" } });
    fireEvent.click(screen.getByRole("button", { name: "查询" }));

    await waitFor(() => {
      expect(mockApi.searchFiles).toHaveBeenCalledWith({ q: "file1", scopeContentHash: "hash_folder" });
    });
  });

  test("search input stays usable inside a folder", async () => {
    mockApi.browseFile.mockResolvedValue(browseItems);

    await renderAndWait();
    fireEvent.click(screen.getByRole("button", { name: "MyFolder" }));
    await waitFor(() => {
      expect(screen.getByText("file1.txt")).toBeInTheDocument();
    });

    const searchGroup = document.querySelector(".search-input-group");
    expect(searchGroup?.className).not.toContain("pointer-events-none");
    expect(getSearchInput()).toBeEnabled();
  });

  test("Escape closes the result dialog without touching the main table", async () => {
    mockApi.searchFiles.mockResolvedValue({
      items: [topSearchItem],
      total: 1,
      truncated: false,
    } satisfies FileSearchResponse);

    await renderAndWait();
    await runQuery("readme");
    expect(screen.getByRole("dialog", { name: "搜索结果" })).toBeInTheDocument();

    fireEvent.keyDown(window, { key: "Escape" });

    await waitFor(() => {
      expect(screen.queryByRole("dialog", { name: "搜索结果" })).not.toBeInTheDocument();
    });
    expect(screen.getByText("MyFolder")).toBeInTheDocument();
    expect(screen.getByText("readme.txt")).toBeInTheDocument();
  });

  test("truncated results show a hint to narrow the keyword", async () => {
    mockApi.searchFiles.mockResolvedValue({
      items: [topSearchItem],
      total: 500,
      truncated: true,
    } satisfies FileSearchResponse);

    await renderAndWait();
    const dialog = await runQuery("readme");

    expect(await within(dialog).findByText(/缩小关键词/)).toBeInTheDocument();
  });

  test("searchFiles errors surface their backend message", async () => {
    mockApi.searchFiles.mockRejectedValue(new Error("请求过于频繁，请稍后再试"));

    await renderAndWait();
    const dialog = await runQuery("readme");

    expect(await within(dialog).findByText("请求过于频繁，请稍后再试")).toBeInTheDocument();
    expect(screen.getByText("MyFolder")).toBeInTheDocument();
  });
});
