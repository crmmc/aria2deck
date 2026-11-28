import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

import { Icon } from "@/shared/components/Icon";

export interface FilterSelectOption {
  value: string;
  label: string;
  disabled?: boolean;
}

interface FilterSelectProps {
  options: readonly FilterSelectOption[];
  value: string;
  onChange: (value: string) => void;
  ariaLabel: string;
  placeholder?: string;
  className?: string;
}

/** 同一时刻只允许一个下拉列表展开（模块级协调，避免引入全局状态）。 */
let closeActiveList: (() => void) | null = null;

const LIST_GAP_PX = 4;
const VIEWPORT_MARGIN_PX = 8;
const LIST_MAX_HEIGHT_PX = 244;

/**
 * 文件筛选专用下拉：触发器 + fixed 定位的玻璃拟态选项列表。
 * 列表无进出场动画（与原生 select 一致）；禁选项不可点，Esc 只关列表不冒泡。
 */
export function FilterSelect({
  options,
  value,
  onChange,
  ariaLabel,
  placeholder = "选择…",
  className = "",
}: FilterSelectProps) {
  const triggerRef = useRef<HTMLButtonElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const [open, setOpen] = useState(false);
  const [activeIndex, setActiveIndex] = useState(0);

  const close = useCallback(() => setOpen(false), []);

  const openList = useCallback(() => {
    const selectedIndex = options.findIndex((option) => option.value === value && !option.disabled);
    const firstEnabled = options.findIndex((option) => !option.disabled);
    setActiveIndex(selectedIndex >= 0 ? selectedIndex : Math.max(0, firstEnabled));
    setOpen(true);
  }, [options, value]);

  // 单实例协调 + 外部点击关闭 + 卸载清理
  useEffect(() => {
    if (!open) return undefined;
    closeActiveList?.();
    closeActiveList = close;
    const handlePointerDown = (event: PointerEvent) => {
      const target = event.target;
      if (!(target instanceof Element)) return;
      if (listRef.current?.contains(target)) return;
      if (triggerRef.current?.contains(target)) return;
      close();
    };
    document.addEventListener("pointerdown", handlePointerDown, true);
    return () => {
      document.removeEventListener("pointerdown", handlePointerDown, true);
      if (closeActiveList === close) closeActiveList = null;
    };
  }, [open, close]);

  // 定位：锚定触发器下缘，随窗口 resize/scroll 重算
  useLayoutEffect(() => {
    if (!open) return undefined;
    const place = () => {
      const list = listRef.current;
      const trigger = triggerRef.current;
      if (!list || !trigger) return;
      const rect = trigger.getBoundingClientRect();
      const top = Math.round(rect.bottom + LIST_GAP_PX);
      list.style.minWidth = `${rect.width}px`;
      list.style.maxHeight = `${Math.max(120, Math.min(LIST_MAX_HEIGHT_PX, window.innerHeight - top - VIEWPORT_MARGIN_PX))}px`;
      list.style.top = `${top}px`;
      list.style.left = `${Math.max(VIEWPORT_MARGIN_PX, Math.min(rect.left, window.innerWidth - list.offsetWidth - VIEWPORT_MARGIN_PX))}px`;
    };
    place();
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    return () => {
      window.removeEventListener("resize", place);
      window.removeEventListener("scroll", place, true);
    };
  }, [open]);

  useEffect(() => {
    if (open) listRef.current?.focus();
  }, [open]);

  // 键盘活动项保持可见
  useEffect(() => {
    if (!open) return;
    const element = listRef.current?.children[activeIndex];
    if (element instanceof HTMLElement && typeof element.scrollIntoView === "function") {
      element.scrollIntoView({ block: "nearest" });
    }
  }, [activeIndex, open]);

  const pick = useCallback((option: FilterSelectOption) => {
    if (option.disabled) return;
    onChange(option.value);
    close();
    triggerRef.current?.focus();
  }, [onChange, close]);

  const handleListKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Escape") {
      event.stopPropagation();
      close();
      triggerRef.current?.focus();
      return;
    }
    const enabled = options.flatMap((option, index) => (option.disabled ? [] : [index]));
    if (enabled.length === 0) return;
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      const position = enabled.indexOf(activeIndex);
      const next = event.key === "ArrowDown"
        ? (position + 1) % enabled.length
        : (position - 1 + enabled.length) % enabled.length;
      setActiveIndex(enabled[next]);
      return;
    }
    if (event.key === "Enter") {
      event.preventDefault();
      const option = options[activeIndex];
      if (option) pick(option);
    }
  };

  const selectedLabel = options.find((option) => option.value === value)?.label ?? value;

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        className={`cselect ${className}${value === "" ? " is-placeholder" : ""}${open ? " is-open" : ""}`}
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-label={ariaLabel}
        onClick={() => (open ? close() : openList())}
        onKeyDown={(event) => {
          if (event.key === "ArrowDown" && !open) {
            event.preventDefault();
            openList();
          }
        }}
      >
        <span className="cselect-label">{value === "" ? placeholder : selectedLabel}</span>
        <span className="cselect-chev"><Icon name="chevron-down" className="icon-sm" /></span>
      </button>
      {open && createPortal(
        <div
          ref={listRef}
          className="cselect-list"
          role="listbox"
          aria-label={ariaLabel}
          tabIndex={0}
          onKeyDown={handleListKeyDown}
        >
          {options.map((option, index) => (
            <div
              key={option.value}
              role="option"
              aria-selected={option.value === value}
              aria-disabled={option.disabled ? true : undefined}
              className={`cselect-option${option.value === value ? " is-selected" : ""}${option.disabled ? " is-disabled" : ""}${index === activeIndex ? " is-active" : ""}`}
              data-value={option.value}
              title={option.disabled ? "与其他条件冲突" : undefined}
              onClick={() => pick(option)}
              onMouseEnter={() => {
                if (!option.disabled) setActiveIndex(index);
              }}
            >
              <span>{option.label}</span>
              {option.value === value && (
                <span className="option-check"><Icon name="check" className="icon-sm" /></span>
              )}
            </div>
          ))}
        </div>,
        document.body,
      )}
    </>
  );
}
