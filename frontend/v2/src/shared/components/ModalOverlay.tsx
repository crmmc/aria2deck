"use client";

import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  type CSSProperties,
  type MouseEvent as ReactMouseEvent,
  type ReactNode,
} from "react";
import { gsap } from "gsap";

import {
  overlayIn,
  overlayReverseOut,
  overlayTriggerMotion,
  type OverlayInOptions,
} from "@/shared/ui/overlayMotion";
import { MODAL_REQUEST_CLOSE_EVENT } from "@/shared/ui/modalClose";
import { setModalDialogOpen } from "@/shared/ui/modalLayers";

const FOCUSABLE_SELECTOR = [
  "button:not([disabled]):not([aria-hidden='true'])",
  "a[href]",
  "input:not([disabled]):not([type='hidden'])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "[tabindex]:not([tabindex='-1'])",
].join(",");

export type OverlayMotionVariant = "dialog" | "command";

interface ModalOverlayProps {
  onClose: () => void;
  ariaLabel: string;
  children: ReactNode;
  className?: string;
  contentClassName?: string;
  contentStyle?: CSSProperties;
  trigger?: Element | null;
  motionVariant?: OverlayMotionVariant;
  onRequestClose?: () => void;
  closeDisabled?: boolean;
}

export function ModalOverlay({
  onClose,
  ariaLabel,
  children,
  className = "modal-overlay",
  contentClassName,
  contentStyle,
  trigger = null,
  motionVariant = "dialog",
  onRequestClose,
  closeDisabled = false,
}: ModalOverlayProps) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const onCloseRef = useRef(onClose);
  const closingRef = useRef(false);
  const invokerRef = useRef<Element | null>(null);
  const triggerRef = useRef(trigger);
  const motionVariantRef = useRef(motionVariant);
  const onRequestCloseRef = useRef(onRequestClose);
  const closeDisabledRef = useRef(closeDisabled);
  const commandOpacityFrameRef = useRef<number | null>(null);
  const entryMotionRef = useRef<OverlayInOptions | null>(null);
  triggerRef.current = trigger;
  motionVariantRef.current = motionVariant;
  onRequestCloseRef.current = onRequestClose;
  closeDisabledRef.current = closeDisabled;

  useEffect(() => {
    onCloseRef.current = onClose;
  }, [onClose]);

  const restoreCommandOpacity = useCallback((dialog = dialogRef.current) => {
    if (commandOpacityFrameRef.current !== null) {
      window.cancelAnimationFrame(commandOpacityFrameRef.current);
      commandOpacityFrameRef.current = null;
    }
    if (dialog) dialog.style.opacity = "";
  }, []);

  const requestClose = useCallback(() => {
    if (closingRef.current) return;
    closingRef.current = true;
    const variant = motionVariantRef.current;
    overlayReverseOut(contentRef.current, {
      ...entryMotionRef.current,
      onComplete: () => {
        if (variant === "command") restoreCommandOpacity();
        onCloseRef.current();
      },
    });
  }, [restoreCommandOpacity]);

  const requestDismiss = useCallback(() => {
    if (closeDisabledRef.current) return;
    if (onRequestCloseRef.current) {
      onRequestCloseRef.current();
      return;
    }
    requestClose();
  }, [requestClose]);

  useLayoutEffect(() => {
    const dialog = dialogRef.current;
    const content = contentRef.current;
    if (!dialog || !content) return;

    const activeElement = document.activeElement;
    invokerRef.current = triggerRef.current ??
      (activeElement instanceof HTMLElement ? activeElement : null);

    const variant = motionVariantRef.current;
    const isCommand = variant === "command";
    if (isCommand) dialog.style.opacity = "0";
    if (!dialog.open) dialog.showModal();
    setModalDialogOpen(dialog, true);

    const focusTarget = content.querySelector<HTMLElement>(FOCUSABLE_SELECTOR);
    const motion = overlayTriggerMotion(content, invokerRef.current);
    const entryMotion = {
      ...(motion ?? {
        scale: isCommand ? 0.86 : 0.96,
        x: 0,
        y: 0,
      }),
      duration: isCommand ? 0.34 : 0.26,
      ease: isCommand ? "back.out(1.2)" : "back.out(1.15)",
    };
    entryMotionRef.current = entryMotion;
    overlayIn(content, entryMotion);
    if (focusTarget) focusTarget.focus();
    else content.focus();
    if (isCommand) {
      commandOpacityFrameRef.current = window.requestAnimationFrame(() => {
        dialog.style.opacity = "1";
        commandOpacityFrameRef.current = null;
      });
    }

    const handleCancel = (event: Event) => {
      event.preventDefault();
      requestDismiss();
    };
    const handleRequestClose = (event: Event) => {
      if (event.target !== dialog) return;
      event.stopPropagation();
      requestClose();
    };
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Tab") return;
      const target = event.target;
      if (target instanceof Element && target.closest("dialog") !== dialog) return;
      const focusable = Array.from(dialog.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR))
        .filter((element) => element.closest("dialog") === dialog);
      if (focusable.length === 0) {
        event.preventDefault();
        content.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const focusOutside = !(document.activeElement instanceof Node) || !dialog.contains(document.activeElement);
      if (event.shiftKey && (document.activeElement === first || focusOutside)) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && (document.activeElement === last || focusOutside)) {
        event.preventDefault();
        first.focus();
      }
    };

    dialog.addEventListener("cancel", handleCancel);
    dialog.addEventListener("keydown", handleKeyDown);
    dialog.addEventListener(MODAL_REQUEST_CLOSE_EVENT, handleRequestClose);
    return () => {
      dialog.removeEventListener("cancel", handleCancel);
      dialog.removeEventListener("keydown", handleKeyDown);
      dialog.removeEventListener(MODAL_REQUEST_CLOSE_EVENT, handleRequestClose);
      if (isCommand) restoreCommandOpacity(dialog);
      gsap.killTweensOf(content);
      content.classList.remove("overlay-animating");
      if (dialog.open) dialog.close();
      setModalDialogOpen(dialog, false);
      if (invokerRef.current instanceof HTMLElement && invokerRef.current.isConnected) {
        invokerRef.current.focus();
      }
    };
  }, [requestClose, requestDismiss, restoreCommandOpacity]);

  const handleDialogClick = (event: ReactMouseEvent<HTMLDialogElement>) => {
    if (event.target !== event.currentTarget) return;
    const rect = event.currentTarget.getBoundingClientRect();
    const outside = event.clientX < rect.left || event.clientX > rect.right ||
      event.clientY < rect.top || event.clientY > rect.bottom;
    if (outside) requestDismiss();
  };

  return (
    <dialog
      ref={dialogRef}
      className={className}
      aria-label={ariaLabel}
      aria-modal="true"
      onClick={handleDialogClick}
    >
      {motionVariant === "command" ? null : (
        <button
          type="button"
          className="modal-backdrop-button"
          aria-hidden="true"
          tabIndex={-1}
          onClick={requestDismiss}
        />
      )}
      <div
        ref={contentRef}
        className={contentClassName}
        style={contentStyle}
        tabIndex={-1}
      >
        {children}
      </div>
    </dialog>
  );
}
