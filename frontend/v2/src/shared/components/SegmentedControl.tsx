import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  type KeyboardEvent,
} from "react";

import { syncSegmentIndicator } from "../ui/segmentIndicator";

export type SegmentedOption<T extends string> = {
  value: T;
  label: string;
  count: number | null;
};

type SegmentedControlProps<T extends string> = {
  ariaLabel: string;
  options: readonly SegmentedOption<T>[];
  value: T;
  onChange: (value: T) => void;
  mode?: "tab" | "radio";
};

export function SegmentedControl<T extends string>({
  ariaLabel,
  options,
  value,
  onChange,
  mode = "tab",
}: SegmentedControlProps<T>) {
  const rootRef = useRef<HTMLDivElement>(null);
  const buttonRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const frameRef = useRef<number | null>(null);
  const hasPositionedRef = useRef(false);

  const sync = useCallback((immediate = false): boolean => {
    const root = rootRef.current;
    if (!root) return false;
    if (frameRef.current !== null) {
      window.cancelAnimationFrame(frameRef.current);
      frameRef.current = null;
    }

    const positioned = syncSegmentIndicator(root, immediate);
    if (
      positioned &&
      immediate &&
      !window.matchMedia("(prefers-reduced-motion: reduce)").matches
    ) {
      frameRef.current = window.requestAnimationFrame(() => {
        const indicator = root.querySelector<HTMLElement>(
          ":scope > .segment-indicator",
        );
        if (indicator) indicator.style.transition = "";
        frameRef.current = null;
      });
    }
    if (positioned) hasPositionedRef.current = true;
    return positioned;
  }, []);

  useLayoutEffect(() => {
    sync(!hasPositionedRef.current);
  }, [options, sync, value]);

  useEffect(() => {
    const root = rootRef.current;
    if (!root) return;

    const handleResize = () => sync(true);
    window.addEventListener("resize", handleResize);
    const observer =
      typeof ResizeObserver === "undefined"
        ? null
        : new ResizeObserver(handleResize);
    observer?.observe(root);
    buttonRefs.current.slice(0, options.length).forEach((button) => {
      if (button) observer?.observe(button);
    });

    return () => {
      window.removeEventListener("resize", handleResize);
      observer?.disconnect();
      if (frameRef.current !== null) {
        window.cancelAnimationFrame(frameRef.current);
        frameRef.current = null;
      }
    };
  }, [options, sync]);

  const selectedIndex = Math.max(
    0,
    options.findIndex((option) => option.value === value),
  );

  const handleKeyDown = (
    event: KeyboardEvent<HTMLButtonElement>,
    index: number,
  ) => {
    let nextIndex: number;
    switch (event.key) {
      case "ArrowRight":
        nextIndex = (index + 1) % options.length;
        break;
      case "ArrowLeft":
        nextIndex = (index - 1 + options.length) % options.length;
        break;
      case "Home":
        nextIndex = 0;
        break;
      case "End":
        nextIndex = options.length - 1;
        break;
      default:
        return;
    }

    event.preventDefault();
    onChange(options[nextIndex].value);
    buttonRefs.current[nextIndex]?.focus();
  };

  const isRadio = mode === "radio";

  return (
    <div
      className="segmented"
      role={isRadio ? "radiogroup" : "tablist"}
      aria-label={ariaLabel}
      ref={rootRef}
    >
      <span className="segment-indicator" aria-hidden="true" />
      {options.map((option, index) => {
        const selected = option.value === value;
        return (
          <button
            key={option.value}
            ref={(node) => {
              buttonRefs.current[index] = node;
            }}
            type="button"
            role={isRadio ? "radio" : "tab"}
            className={`segment${selected ? " active" : ""}`}
            aria-selected={isRadio ? undefined : selected}
            aria-checked={isRadio ? selected : undefined}
            tabIndex={index === selectedIndex ? 0 : -1}
            data-value={option.value}
            onClick={() => onChange(option.value)}
            onKeyDown={(event) => handleKeyDown(event, index)}
          >
            {option.label}
            {!isRadio && <span>{option.count ?? "—"}</span>}
          </button>
        );
      })}
    </div>
  );
}
