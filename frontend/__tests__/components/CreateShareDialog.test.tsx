import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import CreateShareDialog from "@/components/CreateShareDialog";
import { api } from "@/lib/api";
import type { ShareLink } from "@/types";

const showToast = jest.fn();

jest.mock("@/components/Toast", () => ({
  useToast: () => ({ showToast }),
}));

jest.mock("@/lib/api", () => ({
  api: {
    createShare: jest.fn(),
  },
}));

const mockApi = api as jest.Mocked<typeof api>;

function makeShare(overrides: Partial<ShareLink> = {}): ShareLink {
  return {
    id: 1,
    share_code: "abc123",
    file_name: "demo.txt",
    file_size: 1024,
    has_password: true,
    expires_at: null,
    max_downloads: 10,
    download_count: 0,
    status: "active" as const,
    created_at: new Date().toISOString(),
    last_accessed_at: null,
    ...overrides,
  };
}

describe("CreateShareDialog", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText: jest.fn().mockResolvedValue(undefined) },
      configurable: true,
    });
  });

  it("creates share successfully and renders generated link", async () => {
    const onCreated = jest.fn();
    const onClose = jest.fn();
    mockApi.createShare.mockResolvedValue(makeShare() as never);

    render(
      <CreateShareDialog
        userFileId={42}
        fileName="demo.txt"
        onClose={onClose}
        onCreated={onCreated}
      />
    );

    const passwordInput = await screen.findByPlaceholderText("留空则无需密码");
    fireEvent.change(passwordInput, { target: { value: "p@ss" } });

    const downloadsInput = screen.getByPlaceholderText("留空则不限");
    fireEvent.change(downloadsInput, { target: { value: "5" } });

    const expireSelect = screen.getByRole("combobox");
    fireEvent.change(expireSelect, { target: { value: "3600" } });

    fireEvent.click(screen.getByRole("button", { name: "创建分享" }));

    await waitFor(() => {
      expect(mockApi.createShare).toHaveBeenCalledWith({
        user_file_id: 42,
        password: "p@ss",
        expires_in: 3600,
        max_downloads: 5,
      });
    });

    expect(onCreated).toHaveBeenCalled();
    expect(showToast).toHaveBeenCalledWith("分享创建成功", "success");

    const linkInput = await screen.findByDisplayValue("http://localhost/s/abc123");
    expect(linkInput).toBeInTheDocument();
    expect(screen.getByText("密码: p@ss")).toBeInTheDocument();
  });

  it("copies generated link and handles copy failure", async () => {
    mockApi.createShare.mockResolvedValue(makeShare({ has_password: false }) as never);
    const writeText = jest.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText },
      configurable: true,
    });

    render(<CreateShareDialog userFileId={42} fileName="demo.txt" onClose={jest.fn()} />);

    fireEvent.click(await screen.findByRole("button", { name: "创建分享" }));
    await screen.findByDisplayValue("http://localhost/s/abc123");

    fireEvent.click(screen.getByRole("button", { name: "复制" }));
    await waitFor(() => {
      expect(writeText).toHaveBeenCalledWith("http://localhost/s/abc123");
      expect(showToast).toHaveBeenCalledWith("链接已复制", "success");
    });

    writeText.mockRejectedValueOnce(new Error("copy failed"));
    fireEvent.click(screen.getByRole("button", { name: "复制" }));

    await waitFor(() => {
      expect(showToast).toHaveBeenCalledWith("复制失败", "error");
    });
  });

  it("shows error toast when share creation fails", async () => {
    mockApi.createShare.mockRejectedValue(new Error("boom") as never);
    render(<CreateShareDialog userFileId={42} fileName="demo.txt" onClose={jest.fn()} />);

    fireEvent.click(await screen.findByRole("button", { name: "创建分享" }));

    await waitFor(() => {
      expect(showToast).toHaveBeenCalledWith("创建失败: boom", "error");
    });
  });

  it("closes on close button and overlay click", async () => {
    const onClose = jest.fn();
    render(<CreateShareDialog userFileId={42} fileName="demo.txt" onClose={onClose} />);

    fireEvent.click(await screen.findByRole("button", { name: "✕" }));
    expect(onClose).toHaveBeenCalledTimes(1);

    const overlay = document.querySelector(".modal-overlay");
    expect(overlay).not.toBeNull();
    fireEvent.click(overlay as Element);
    expect(onClose).toHaveBeenCalledTimes(2);
  });
});
