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
    share_code: "perm123",
    file_name: "demo.txt",
    file_size: 1024,
    has_password: false,
    password: null,
    expires_at: null,
    max_downloads: null,
    download_count: 0,
    status: "active" as const,
    created_at: new Date().toISOString(),
    last_accessed_at: null,
    ...overrides,
  };
}

describe("CreateShareDialog edge cases", () => {
  beforeEach(() => {
    jest.clearAllMocks();
    Object.defineProperty(navigator, "clipboard", {
      value: { writeText: jest.fn().mockResolvedValue(undefined) },
      configurable: true,
    });
  });

  it("sends undefined expires_in when 永久 option is selected", async () => {
    mockApi.createShare.mockResolvedValue(makeShare() as never);

    render(<CreateShareDialog userFileId={7} fileName="demo.txt" onClose={jest.fn()} />);

    fireEvent.change(await screen.findByRole("combobox"), { target: { value: "0" } });
    fireEvent.click(screen.getByRole("button", { name: "创建分享" }));

    await waitFor(() => {
      expect(mockApi.createShare).toHaveBeenCalledWith({
        user_file_id: 7,
        password: undefined,
        expires_in: undefined,
        max_downloads: undefined,
      });
    });
  });

  it("clears max downloads back to empty and submits undefined", async () => {
    mockApi.createShare.mockResolvedValue(makeShare() as never);

    render(<CreateShareDialog userFileId={7} fileName="demo.txt" onClose={jest.fn()} />);

    const downloadsInput = await screen.findByPlaceholderText("留空则不限");
    fireEvent.change(downloadsInput, { target: { value: "5" } });
    fireEvent.change(downloadsInput, { target: { value: "" } });
    expect(downloadsInput).toHaveValue(null);

    fireEvent.click(screen.getByRole("button", { name: "创建分享" }));

    await waitFor(() => {
      expect(mockApi.createShare).toHaveBeenCalledWith(
        expect.objectContaining({ max_downloads: undefined })
      );
    });
  });

  it.each(["0", "-3", "1.5", "20000"])(
    "rejects invalid download limit %s",
    async (value) => {
      render(<CreateShareDialog userFileId={7} fileName="demo.txt" onClose={jest.fn()} />);

      const downloadsInput = await screen.findByPlaceholderText("留空则不限");
      fireEvent.change(downloadsInput, { target: { value: "5" } });
      fireEvent.change(downloadsInput, { target: { value } });
      expect(downloadsInput).toHaveValue(5);
    }
  );
});
