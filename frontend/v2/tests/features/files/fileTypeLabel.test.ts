import { fileTypeLabel, sortBrowseItems, sortFiles, type BrowseItemView, type FileView } from "@/features/files/model";

describe("fileTypeLabel", () => {
  it.each([
    ["archive.zip", false, "zip"],
    ["movie.MKV", false, "mkv"],
    ["archive.tar.gz", false, "tar.gz"],
    ["backup.TAR.GZ", false, "tar.gz"],
    ["data.tar.bz2", false, "tar.bz2"],
    ["data.tar.xz", false, "tar.xz"],
    ["data.tar.zst", false, "tar.zst"],
    ["archive.v1.2.tar.gz", false, "tar.gz"],
    ["report.final.docx", false, "docx"],
    ["a.b.c", false, "c"],
    [".gitignore", false, "-"],
    ["LICENSE", false, "-"],
    ["readme", false, "-"],
    ["trailing.", false, "-"],
    ["Ubuntu_24.04.iso", false, "iso"],
    ["素材", true, "文件夹"],
    ["folder.zip", true, "文件夹"],
  ])("fileTypeLabel(%s, %s) = %s", (name, isDirectory, expected) => {
    expect(fileTypeLabel(name, isDirectory)).toBe(expected);
  });
});

describe("sortBrowseItems 类型排序", () => {
  it("目录项按类型排序且文件夹在前，同类型回退名称", () => {
    const items: BrowseItemView[] = [
      { name: "b.zip", size: 1, isDirectory: false },
      { name: "a.zip", size: 2, isDirectory: false },
      { name: "c.mp4", size: 3, isDirectory: false },
      { name: "子目录", size: 0, isDirectory: true },
    ];
    expect(sortBrowseItems(items, "type", "asc").map((item) => item.name)).toEqual([
      "子目录",
      "c.mp4",
      "a.zip",
      "b.zip",
    ]);
    expect(sortBrowseItems(items, "type", "desc").map((item) => item.name)).toEqual([
      "子目录",
      "b.zip",
      "a.zip",
      "c.mp4",
    ]);
  });
});

describe("sortFiles 类型排序", () => {
  const files: FileView[] = [
    { id: 1, name: "b.zip", size: 10, isDirectory: false, createdAtMs: 1 },
    { id: 2, name: "a.mp4", size: 20, isDirectory: false, createdAtMs: 2 },
    { id: 3, name: "c.tar.gz", size: 30, isDirectory: false, createdAtMs: 3 },
    { id: 4, name: "素材", size: 0, isDirectory: true, createdAtMs: 4 },
  ];

  it("升序按显示文本排序且文件夹在前", () => {
    expect(sortFiles(files, "type", "asc").map((file) => file.name)).toEqual([
      "素材",
      "a.mp4",
      "c.tar.gz",
      "b.zip",
    ]);
  });

  it("降序反转文件顺序但文件夹仍在前", () => {
    expect(sortFiles(files, "type", "desc").map((file) => file.name)).toEqual([
      "素材",
      "b.zip",
      "c.tar.gz",
      "a.mp4",
    ]);
  });

  it("同类型时按名称排序", () => {
    const sameType: FileView[] = [
      { id: 1, name: "b.zip", size: 0, isDirectory: false, createdAtMs: 0 },
      { id: 2, name: "a.zip", size: 0, isDirectory: false, createdAtMs: 0 },
    ];
    expect(sortFiles(sameType, "type", "asc").map((file) => file.name)).toEqual(["a.zip", "b.zip"]);
  });
});
