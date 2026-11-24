import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import FilesPage from "@/app/(authenticated)/files/page";
import { ToastProvider } from "@/components/Toast";
import { api } from "@/lib/api";
import type { FileInfo, BrowseFileInfo, FileListResponse } from "@/types";

jest.mock("next/navigation", () => ({
  useRouter: () => ({ push: jest.fn() }),
}));

jest.mock("@/hooks/useIsMobile", () => ({
  __esModule: true,
  useIsMobile: () => true,
}));

jest.mock("@/lib/api", () => ({
  __esModule: true,
  api: {
    listFiles: jest.fn(),
    browseFile: jest.fn(),
    searchFiles: jest.fn(),
    downloadFileUrl: jest.fn(),
    deleteFiles: jest.fn(),
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

const browseItems: BrowseFileInfo[] = [
  bf("file1.txt", 100, false),
  bf("subdir", 0, true),
];

function bf(name: string, size: number, isDirectory: boolean, path = name): BrowseFileInfo {
  return { name, size, is_directory: isDirectory, is_dir: isDirectory, path, modified_at: 1_700_000_000_000 };
}

function browseResponse(items: BrowseFileInfo[], total = items.length) {
  return { items, total, page: 1, page_size: 200 };
}


beforeEach(() => {
  jest.clearAllMocks();
  jest.spyOn(console, "error").mockImplementation(() => {});
});

afterEach(() => {
  (console.error as jest.Mock).mockRestore?.();
});

describe("FilesPage mobile folder view", () => {
  test("delete inside a folder shows the unsupported toast", async () => {
    mockApi.listFiles.mockResolvedValue({
      files: [folderFile],
      total: 1,
      space: { used: 1024, frozen: 0, available: 9216 },
    } satisfies FileListResponse);
    mockApi.browseFile.mockResolvedValue(browseResponse(browseItems));

    render(
      <ToastProvider>
        <FilesPage />
      </ToastProvider>
    );
    await waitFor(() => {
      expect(screen.getByText("MyFolder")).toBeInTheDocument();
    });

    fireEvent.click(screen.getByRole("button", { name: "浏览" }));
    await waitFor(() => {
      expect(screen.getByText("file1.txt")).toBeInTheDocument();
    });

    fireEvent.click(screen.getAllByRole("button", { name: "删除" })[0]);

    expect(await screen.findByText(/文件夹内暂不支持/)).toBeInTheDocument();
    expect(mockApi.deleteFiles).not.toHaveBeenCalled();
  });
});
