import type { RefObject } from "react";

type AddTaskFormProps = {
  uri: string;
  error: string | null;
  isSubmitting: boolean;
  torrentInputRef: RefObject<HTMLInputElement>;
  onUriChange: (value: string) => void;
  onSubmit: (event: React.FormEvent<HTMLFormElement>) => void;
  onTorrentUpload: (event: React.ChangeEvent<HTMLInputElement>) => void;
  onBatchAdd: () => void;
};

export function AddTaskForm({
  uri,
  error,
  isSubmitting,
  torrentInputRef,
  onUriChange,
  onSubmit,
  onTorrentUpload,
  onBatchAdd,
}: AddTaskFormProps) {
  return (
    <div className="card add-task-card">
      <form onSubmit={onSubmit} className="add-task-form">
        <input
          className="input add-task-input"
          placeholder="粘贴磁力链接、HTTP 或 FTP URL..."
          value={uri}
          onChange={(event) => onUriChange(event.target.value)}
          required
          aria-label="下载链接"
        />
        <input
          type="file"
          ref={torrentInputRef}
          accept=".torrent"
          onChange={onTorrentUpload}
          className="hidden"
          aria-label="上传种子文件"
        />
        <button
          className={`button flex-shrink-0 shadow-none${isSubmitting ? " opacity-60" : ""}`}
          type="submit"
          disabled={isSubmitting}
        >
          {isSubmitting ? "添加中..." : "+ 添加任务"}
        </button>
        <button
          className="button secondary flex-shrink-0 shadow-none"
          type="button"
          onClick={onBatchAdd}
        >
          批量添加
        </button>
        <button
          className="button secondary flex-shrink-0 shadow-none"
          type="button"
          onClick={() => torrentInputRef.current?.click()}
          title="上传种子文件"
        >
          上传种子
        </button>
      </form>
      {error ? <div className="form-error">{error}</div> : null}
    </div>
  );
}
