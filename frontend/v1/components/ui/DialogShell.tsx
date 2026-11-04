import type { ReactNode, CSSProperties } from "react";
import { createPortal } from "react-dom";

import { ModalOverlay } from "@/components/ModalOverlay";
import { useMounted } from "@/lib/useMounted";

type DialogShellProps = {
  onClose: () => void;
  ariaLabel: string;
  children: ReactNode;
  contentClassName?: string;
  contentStyle?: CSSProperties;
  className?: string;
  portal?: boolean;
};

export function DialogShell({
  onClose,
  ariaLabel,
  children,
  contentClassName,
  contentStyle,
  className,
  portal = true,
}: DialogShellProps) {
  const mounted = useMounted();
  const dialog = (
    <ModalOverlay
      onClose={onClose}
      ariaLabel={ariaLabel}
      className={className}
      contentClassName={contentClassName}
      contentStyle={contentStyle}
    >
      {children}
    </ModalOverlay>
  );

  if (!portal) return dialog;
  if (!mounted) return null;
  return createPortal(dialog, document.body);
}
