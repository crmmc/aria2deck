const openDialogs: HTMLDialogElement[] = [];
const listeners = new Set<() => void>();

function notifyLayerChange() {
  listeners.forEach((listener) => listener());
}

export function setModalDialogOpen(dialog: HTMLDialogElement, open: boolean) {
  const index = openDialogs.indexOf(dialog);
  if (open) {
    if (index !== -1 && index === openDialogs.length - 1) return;
    if (index !== -1) openDialogs.splice(index, 1);
    openDialogs.push(dialog);
    notifyLayerChange();
    return;
  }
  if (index === -1) return;
  openDialogs.splice(index, 1);
  notifyLayerChange();
}

export function getTopModalDialog(): HTMLDialogElement | null {
  while (openDialogs.length > 0) {
    const dialog = openDialogs[openDialogs.length - 1];
    if (dialog.isConnected && dialog.open) return dialog;
    openDialogs.pop();
  }
  return null;
}

export function subscribeToModalLayers(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}
