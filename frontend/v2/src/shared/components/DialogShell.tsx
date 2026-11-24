import type { ReactNode, CSSProperties } from "react";
import { createPortal } from "react-dom";

import { ModalOverlay, type OverlayMotionVariant } from "@/shared/components/ModalOverlay";
import { useMounted } from "@/shared/hooks/useMounted";

type DialogShellProps = {
  onClose: () => void;
  ariaLabel: string;
  children: ReactNode;
  contentClassName?: string;
  contentStyle?: CSSProperties;
  className?: string;
  portal?: boolean;
  motionVariant?: OverlayMotionVariant;
  closeDisabled?: boolean;
};

export function DialogShell({
  onClose,
  ariaLabel,
  children,
  contentClassName,
  contentStyle,
  className,
  portal = true,
  motionVariant,
  closeDisabled = false,
}: DialogShellProps) {
  const mounted = useMounted();
  const dialog = (
    <ModalOverlay
      onClose={onClose}
      ariaLabel={ariaLabel}
      className={className}
      contentClassName={contentClassName}
      contentStyle={contentStyle}
      motionVariant={motionVariant}
      closeDisabled={closeDisabled}
    >
      {children}
    </ModalOverlay>
  );

  if (!portal) return dialog;
  if (!mounted) return null;
  return createPortal(dialog, document.body);
}
