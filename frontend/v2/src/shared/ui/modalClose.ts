export const MODAL_REQUEST_CLOSE_EVENT = "v2:modal-request-close";

export function requestModalClose(element: Element | null) {
  element?.closest("dialog")?.dispatchEvent(new Event(MODAL_REQUEST_CLOSE_EVENT, { bubbles: true }));
}
