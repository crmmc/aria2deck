"use client";

import { useCallback, useEffect, useRef, type ReactNode, type CSSProperties } from "react";

const FOCUSABLE_SELECTOR = [
  "button:not([disabled]):not([aria-hidden='true'])",
  "[href]",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "[tabindex]:not([tabindex='-1'])",
].join(",");

interface ModalOverlayProps {
  onClose: () => void;
  ariaLabel: string;
  children: ReactNode;
  className?: string;
  contentClassName?: string;
  contentStyle?: CSSProperties;
}

export function ModalOverlay({
  onClose,
  ariaLabel,
  children,
  className = "modal-overlay",
  contentClassName,
  contentStyle,
}: ModalOverlayProps) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const onCloseRef = useRef(onClose);

  useEffect(() => {
    onCloseRef.current = onClose;
  }, [onClose]);

  const stableOnClose = useCallback(() => onCloseRef.current(), []);

  useEffect(() => {
    const dialog = dialogRef.current;
    if (!dialog) return;

    if (!dialog.open) {
      dialog.showModal();
    }

    const focusTarget = contentRef.current?.querySelector<HTMLElement>(FOCUSABLE_SELECTOR);
    focusTarget?.focus();

    const handleCancel = (event: Event) => {
      event.preventDefault();
      onCloseRef.current();
    };

    dialog.addEventListener("cancel", handleCancel);
    return () => {
      dialog.removeEventListener("cancel", handleCancel);
      if (dialog.open) {
        dialog.close();
      }
    };
  }, []);

  return (
    <dialog
      ref={dialogRef}
      className={className}
      aria-label={ariaLabel}
    >
      <button
        type="button"
        className="modal-backdrop-button"
        aria-hidden="true"
        tabIndex={-1}
        onClick={stableOnClose}
      />
      <div
        ref={contentRef}
        className={contentClassName}
        style={contentStyle}
      >
        {children}
      </div>
    </dialog>
  );
}
