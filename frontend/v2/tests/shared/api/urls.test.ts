import { fileDownloadUrl, publicShareDownloadUrl } from "@/shared/api/urls";

describe("urls", () => {
  it("builds cookie-session file download URL", () => {
    expect(fileDownloadUrl(3)).toBe("/api/v2/files/3/download");
    expect(fileDownloadUrl(3, "docs/a b.bin")).toBe(
      "/api/v2/files/3/download?path=docs%2Fa%20b.bin",
    );
  });

  it("builds public share download URL with encoding", () => {
    expect(publicShareDownloadUrl("ab/cd")).toBe("/api/s/ab%2Fcd/download");
  });
});
