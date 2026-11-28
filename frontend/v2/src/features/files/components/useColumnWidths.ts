import { useCallback, useMemo, useState, type CSSProperties } from "react";

import { useLocalState } from "@/shared/hooks/useLocalState";

export type ResizableColumn = "type" | "size" | "date" | "ops";

export interface ColumnWidthDef {
  def: number;
  min: number;
  max: number;
}

export const COLUMN_DEFS: Record<ResizableColumn, ColumnWidthDef> = {
  type: { def: 84, min: 52, max: 200 },
  size: { def: 92, min: 60, max: 200 },
  date: { def: 146, min: 104, max: 240 },
  ops: { def: 122, min: 100, max: 220 },
};

export type ColumnWidths = Record<ResizableColumn, number>;

const DEFAULT_WIDTHS: ColumnWidths = { type: 84, size: 92, date: 146, ops: 122 };
const COLUMN_KEYS = Object.keys(COLUMN_DEFS) as ResizableColumn[];

function clampWidth(column: ResizableColumn, width: number): number {
  const { min, max } = COLUMN_DEFS[column];
  return Math.min(max, Math.max(min, Math.round(width)));
}

/** 本地存储数据可能损坏或来自旧版本：逐列校验，非法值回退默认。 */
export function sanitizeWidths(raw: unknown): ColumnWidths {
  const widths = { ...DEFAULT_WIDTHS };
  if (raw && typeof raw === "object") {
    for (const key of COLUMN_KEYS) {
      const value = (raw as Record<string, unknown>)[key];
      if (typeof value === "number" && Number.isFinite(value)) {
        widths[key] = clampWidth(key, value);
      }
    }
  }
  return widths;
}

/**
 * 文件表格列宽：持久化值 + 拖拽中的瞬态预览。
 * beginDrag/updateDrag/endDrag 由表头手柄的 pointer 事件驱动，endDrag 才写 localStorage。
 */
export function useColumnWidths() {
  const { value, setValue } = useLocalState<unknown>("files.column_widths", DEFAULT_WIDTHS);
  const stored = useMemo(() => sanitizeWidths(value), [value]);
  const [dragWidths, setDragWidths] = useState<ColumnWidths | null>(null);
  const widths = dragWidths ?? stored;

  const beginDrag = useCallback(() => {
    setDragWidths(stored);
  }, [stored]);

  const updateDrag = useCallback((column: ResizableColumn, width: number) => {
    setDragWidths((prev) => (prev ? { ...prev, [column]: clampWidth(column, width) } : prev));
  }, []);

  const endDrag = useCallback(() => {
    setDragWidths((prev) => {
      if (prev) setValue(prev);
      return null;
    });
  }, [setValue]);

  const resetColumn = useCallback(
    (column: ResizableColumn) => {
      setValue({ ...stored, [column]: COLUMN_DEFS[column].def });
    },
    [stored, setValue],
  );

  const styleVars = useMemo(
    () =>
      ({
        "--file-cols-type": `${widths.type}px`,
        "--file-cols-size": `${widths.size}px`,
        "--file-cols-date": `${widths.date}px`,
        "--file-cols-ops": `${widths.ops}px`,
      }) as CSSProperties,
    [widths],
  );

  return { widths, beginDrag, updateDrag, endDrag, resetColumn, styleVars };
}
