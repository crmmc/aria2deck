import { useCallback, useEffect, useLayoutEffect, useRef } from "react";
import { createPortal } from "react-dom";

import { Icon } from "@/shared/components/Icon";
import {
  defaultRule,
  distinctTypeLabels,
  FIELD_RULES,
  isRuleDisabled,
  isValueDisabled,
  SIZE_PRESETS,
  type FilterCondition,
  type FilterField,
  type FilterRule,
  type FileView,
} from "../model";
import { FilterSelect } from "./FilterSelect";

/** 收起动画时长（与 06-components.css 的 pack-pop-out 一致），animationend 未触发时兜底。 */
const CLOSE_FALLBACK_MS = 200;
const ANCHOR_GAP_PX = 8;
const VIEWPORT_MARGIN_PX = 12;
const MAX_HEIGHT_PX = 660;

interface FilterPopoverProps {
  conditions: readonly FilterCondition[];
  /** 当前页全部条目（提供类型指标与隐藏计数）。 */
  files: readonly FileView[];
  visibleCount: number;
  trigger: HTMLElement | null;
  closing: boolean;
  onChange: (conditions: FilterCondition[]) => void;
  onRequestClose: () => void;
  onClosed: () => void;
}

/** 本页条件筛选浮层：字段/规则/指标三段条件行，条件间固定 AND，实时生效。 */
export function FilterPopover({
  conditions,
  files,
  visibleCount,
  trigger,
  closing,
  onChange,
  onRequestClose,
  onClosed,
}: FilterPopoverProps) {
  const rootRef = useRef<HTMLDivElement>(null);
  const finishedRef = useRef(false);

  const finish = useCallback(() => {
    if (finishedRef.current) return;
    finishedRef.current = true;
    // 焦点在浮层内被卸载时会丢到 body，还给入口按钮；浮层外（点击穿透）不抢夺焦点。
    const root = rootRef.current;
    if (root && document.activeElement instanceof HTMLElement && root.contains(document.activeElement)) {
      if (trigger instanceof HTMLElement && trigger.isConnected) trigger.focus();
    }
    onClosed();
  }, [onClosed, trigger]);

  useEffect(() => {
    if (!closing) return undefined;
    if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) {
      finish();
      return undefined;
    }
    const root = rootRef.current;
    const timer = window.setTimeout(finish, CLOSE_FALLBACK_MS);
    const handleAnimationEnd = (event: AnimationEvent) => {
      if (event.target === root) finish();
    };
    root?.addEventListener("animationend", handleAnimationEnd);
    return () => {
      window.clearTimeout(timer);
      root?.removeEventListener("animationend", handleAnimationEnd);
    };
  }, [closing, finish]);

  useEffect(() => {
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      if (document.querySelector("dialog[open]")) return;
      onRequestClose();
    };
    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target;
      if (!(target instanceof Element)) return;
      if (rootRef.current?.contains(target)) return;
      if (trigger?.contains(target)) return;
      // 下拉选项列表 portal 在浮层 DOM 之外，但属于浮层内部交互。
      if (target.closest(".cselect-list")) return;
      // 模态（确认框、其他对话框）内部或背板上的点击不影响浮层。
      if (target.closest("dialog")) return;
      // 不拦截事件：点击穿透到页面元素正常生效，浮层只负责收起。
      onRequestClose();
    };
    document.addEventListener("keydown", handleKeyDown);
    document.addEventListener("pointerdown", handlePointerDown, true);
    return () => {
      document.removeEventListener("keydown", handleKeyDown);
      document.removeEventListener("pointerdown", handlePointerDown, true);
    };
  }, [onRequestClose, trigger]);

  useLayoutEffect(() => {
    const place = () => {
      const root = rootRef.current;
      if (!root || !trigger) return;
      const rect = trigger.getBoundingClientRect();
      const top = Math.round(rect.bottom + ANCHOR_GAP_PX);
      root.style.top = `${top}px`;
      root.style.maxHeight = `${Math.max(200, Math.min(MAX_HEIGHT_PX, window.innerHeight - top - VIEWPORT_MARGIN_PX))}px`;
      const width = root.offsetWidth;
      const left = Math.max(
        VIEWPORT_MARGIN_PX,
        Math.min(rect.right - width, window.innerWidth - width - VIEWPORT_MARGIN_PX),
      );
      root.style.left = `${left}px`;
    };
    place();
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    return () => {
      window.removeEventListener("resize", place);
      window.removeEventListener("scroll", place, true);
    };
  }, [trigger]);

  const updateRow = (index: number, patch: Partial<FilterCondition>) => {
    onChange(conditions.map((condition, i) => (i === index ? { ...condition, ...patch } : condition)));
  };

  const handleFieldChange = (index: number, field: FilterField) => {
    updateRow(index, { field, rule: defaultRule(conditions, index, field), value: "" });
  };

  const handleRuleChange = (index: number, rule: FilterRule) => {
    const condition = conditions[index];
    const keepValue = condition.value !== ""
      && !isValueDisabled(conditions, index, { field: condition.field, rule }, condition.value);
    updateRow(index, { rule, value: keepValue ? condition.value : "" });
  };

  const handleValueChange = (index: number, value: string) => {
    updateRow(index, { value });
  };

  const removeRow = (index: number) => {
    onChange(conditions.filter((_, i) => i !== index));
  };

  const addRow = () => {
    const nextId = Math.max(0, ...conditions.map((condition) => condition.id)) + 1;
    onChange([
      ...conditions,
      { id: nextId, field: "type", rule: defaultRule(conditions, conditions.length, "type"), value: "" },
    ]);
  };

  const activeCount = conditions.filter((condition) => condition.value !== "").length;
  const hasIncomplete = conditions.some((condition) => condition.value === "");
  const hiddenCount = files.length - visibleCount;

  return createPortal(
    <div
      ref={rootRef}
      className={closing ? "pack-pop filter-pop is-closing" : "pack-pop filter-pop is-open"}
      role="dialog"
      aria-label="筛选"
    >
      <div className="filter-head">
        <span className="font-medium text-sm">按条件筛选</span>
        <span className="scope">仅筛选当前页 · 条件间为 AND</span>
      </div>
      <div className="pack-pop-body">
        {conditions.length === 0 ? (
          <p className="filter-empty-hint">尚未添加条件，列表显示全部。条件之间为 AND 关系，全部条件同时满足才会显示。</p>
        ) : (
          <div className="filter-cond-list">
            {conditions.map((condition, index) => (
              <div key={condition.id}>
                {index > 0 && <div className="filter-and" aria-hidden="true">AND</div>}
                <div className="filter-cond-row">
                  <FilterSelect
                    className="cond-field"
                    ariaLabel={`条件 ${index + 1} 字段`}
                    options={[
                      { value: "type", label: "类型" },
                      { value: "size", label: "大小" },
                    ]}
                    value={condition.field}
                    onChange={(field) => handleFieldChange(index, field as FilterField)}
                  />
                  <FilterSelect
                    className="cond-rule"
                    ariaLabel={`条件 ${index + 1} 规则`}
                    options={FIELD_RULES[condition.field].map((rule) => ({
                      value: rule.value,
                      label: rule.label,
                      disabled: isRuleDisabled(conditions, index, condition.field, rule.value),
                    }))}
                    value={condition.rule}
                    onChange={(rule) => handleRuleChange(index, rule as FilterRule)}
                  />
                  <FilterSelect
                    className="cond-value"
                    ariaLabel={`条件 ${index + 1} 指标`}
                    options={(condition.field === "type"
                      ? distinctTypeLabels(files).map((label) => ({ value: label, label }))
                      : SIZE_PRESETS.map((preset) => ({ value: preset.value, label: preset.label }))
                    ).map((option) => ({
                      ...option,
                      disabled: isValueDisabled(conditions, index, condition, option.value),
                    }))}
                    value={condition.value}
                    onChange={(value) => handleValueChange(index, value)}
                  />
                  <button
                    type="button"
                    className="icon-btn cond-remove"
                    aria-label={`删除条件 ${index + 1}`}
                    onClick={() => removeRow(index)}
                  >
                    <Icon name="x" className="icon-sm" />
                  </button>
                </div>
              </div>
            ))}
          </div>
        )}
        <button
          type="button"
          className="filter-add-row"
          onClick={addRow}
          disabled={hasIncomplete}
        >
          ＋ 添加条件
        </button>
      </div>
      <div className="pack-pop-footer">
        <span className="filter-hidden-note">
          {activeCount > 0 ? `已隐藏 ${hiddenCount} 项 · 隐藏项不参与批量操作` : "即时生效，仅筛选当前页"}
        </span>
        <button type="button" className="button secondary btn-sm" onClick={() => onChange([])}>
          清除筛选
        </button>
      </div>
    </div>,
    document.body,
  );
}
