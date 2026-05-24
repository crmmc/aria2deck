type ShareDownloadActionsProps = {
  downloading: boolean;
  onDownload: () => void;
};

export function ShareDownloadActions({ downloading, onDownload }: ShareDownloadActionsProps) {
  return (
    <button
      type="button"
      onClick={onDownload}
      className="button w-full"
      disabled={downloading}
      style={{ opacity: downloading ? 0.7 : 1 }}
    >
      {downloading ? "准备下载..." : "下载文件"}
    </button>
  );
}
