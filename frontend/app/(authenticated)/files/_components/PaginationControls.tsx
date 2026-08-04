type PaginationControlsProps = {
  currentPage: number;
  pageSize: number;
  totalFiles: number;
  onPageChange: (page: number) => void;
  onPageSizeChange: (pageSize: number) => void;
};

export function PaginationControls({
  currentPage,
  pageSize,
  totalFiles,
  onPageChange,
  onPageSizeChange,
}: PaginationControlsProps) {
  const totalPages = Math.max(1, Math.ceil(totalFiles / pageSize));
  const pages: number[] = [];
  let start = Math.max(1, currentPage - 2);
  const end = Math.min(totalPages, start + 4);
  start = Math.max(1, end - 4);

  for (let page = start; page <= end; page += 1) {
    pages.push(page);
  }

  return (
    <div className="flex items-center justify-end gap-2 py-3 px-2">
      <select
        className="select-sm"
        value={pageSize}
        aria-label="每页条数"
        onChange={(event) => {
          onPageSizeChange(Number(event.target.value));
          onPageChange(1);
        }}
      >
        {[10, 20, 30, 50, 100].map((size) => (
          <option key={size} value={size}>{size} 条/页</option>
        ))}
      </select>
      <span className="text-sm muted" style={{ marginLeft: 4 }}>
        共 {totalFiles} 项
      </span>
      <div className="flex items-center gap-0" style={{ marginLeft: 8 }}>
        <button
          type="button"
          className="button secondary btn-sm"
          style={{ borderRadius: "4px 0 0 4px" }}
          disabled={currentPage <= 1}
          aria-label="上一页"
          onClick={() => onPageChange(currentPage - 1)}
        >
          ‹
        </button>
        {pages.map((page) => (
          <button
            type="button"
            key={page}
            className={`button btn-sm ${page === currentPage ? "primary" : "secondary"}`}
            style={{ borderRadius: 0, minWidth: 32 }}
            onClick={() => {
              if (page !== currentPage) onPageChange(page);
            }}
          >
            {page}
          </button>
        ))}
        <button
          type="button"
          className="button secondary btn-sm"
          style={{ borderRadius: "0 4px 4px 0" }}
          disabled={currentPage >= totalPages}
          aria-label="下一页"
          onClick={() => onPageChange(currentPage + 1)}
        >
          ›
        </button>
      </div>
    </div>
  );
}
