import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import StoragePage from "@/app/(authenticated)/storage/page";
import { api } from "@/lib/api";

const replaceMock = jest.fn();
const showToastMock = jest.fn();

jest.mock("next/navigation", () => ({
  useRouter: () => ({ replace: replaceMock }),
}));

jest.mock("@/lib/AuthContext", () => ({
  __esModule: true,
  useAuth: () => ({
    user: {
      id: 1,
      username: "admin",
      is_admin: true,
      quota: 1024 * 1024 * 1024,
      is_initial_password: false,
    },
  }),
}));

jest.mock("@/components/Toast", () => ({
  __esModule: true,
  useToast: () => ({
    showToast: showToastMock,
  }),
}));

jest.mock("@/lib/api", () => ({
  __esModule: true,
  api: {
    listStoredFiles: jest.fn(),
    bulkDeleteStoredFiles: jest.fn(),
    getFileUsers: jest.fn(),
  },
}));

const mockApi = api as jest.Mocked<typeof api>;

const file = {
  id: 1,
  content_hash: "hash_abc",
  original_name: "movie.mkv",
  size: 1024 * 1024,
  is_directory: false,
  ref_count: 1,
  created_at: "2024-01-01T00:00:00Z",
  real_path: "/tmp/movie.mkv",
  exists_on_disk: true,
};

describe("StoragePage", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    mockApi.listStoredFiles.mockResolvedValue({
      files: [file],
      total: 1,
    } as never);
    mockApi.getFileUsers.mockResolvedValue({
      file_id: 1,
      users: [{ user_id: 1, username: "admin", display_name: "movie.mkv" }],
    } as never);
    mockApi.bulkDeleteStoredFiles.mockResolvedValue({
      deleted_count: 1,
      failed_ids: [],
      errors: [],
    } as never);
  });

  test("renders storage files and opens user list modal", async () => {
    render(<StoragePage />);

    expect(await screen.findByText("存储管理")).toBeInTheDocument();
    expect(screen.getByText("movie.mkv")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "1" }));
    await waitFor(() => {
      expect(mockApi.getFileUsers).toHaveBeenCalledWith(1);
    });
    expect(await screen.findByText("引用用户")).toBeInTheDocument();
  });

  test("deletes selected files", async () => {
    render(<StoragePage />);

    expect(await screen.findByText("存储管理")).toBeInTheDocument();
    fireEvent.click(screen.getAllByRole("checkbox")[1]);
    fireEvent.click(screen.getByRole("button", { name: "删除选中 (1)" }));

    await waitFor(() => {
      expect(mockApi.bulkDeleteStoredFiles).toHaveBeenCalledWith([1]);
    });
  });

  test("shows error toast when deleting selected files fails", async () => {
    mockApi.bulkDeleteStoredFiles.mockRejectedValue(new Error("delete failed") as never);
    render(<StoragePage />);

    expect(await screen.findByText("存储管理")).toBeInTheDocument();
    fireEvent.click(screen.getAllByRole("checkbox")[1]);
    fireEvent.click(screen.getByRole("button", { name: "删除选中 (1)" }));

    await waitFor(() => {
      expect(showToastMock).toHaveBeenCalledWith("删除失败", "error");
    });
  });
});
