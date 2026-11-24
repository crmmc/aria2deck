import { fireEvent, render, screen } from "@testing-library/react";
import { ModalOverlay } from "@/components/ModalOverlay";

describe("ModalOverlay", () => {
  let originalShowModal: HTMLDialogElement["showModal"] | undefined;
  let originalClose: HTMLDialogElement["close"] | undefined;
  let showModalMock: jest.Mock<void, []>;
  let closeMock: jest.Mock<void, []>;

  beforeEach(() => {
    originalShowModal = HTMLDialogElement.prototype.showModal;
    originalClose = HTMLDialogElement.prototype.close;
    showModalMock = jest.fn(function showModal(this: HTMLDialogElement) {
        this.setAttribute("open", "");
      });
    closeMock = jest.fn(function close(this: HTMLDialogElement) {
        this.removeAttribute("open");
      });
    Object.defineProperty(HTMLDialogElement.prototype, "showModal", {
      configurable: true,
      value: showModalMock,
    });
    Object.defineProperty(HTMLDialogElement.prototype, "close", {
      configurable: true,
      value: closeMock,
    });
  });

  afterEach(() => {
    if (originalShowModal) {
      Object.defineProperty(HTMLDialogElement.prototype, "showModal", {
        configurable: true,
        value: originalShowModal,
      });
    } else {
      Reflect.deleteProperty(HTMLDialogElement.prototype, "showModal");
    }
    if (originalClose) {
      Object.defineProperty(HTMLDialogElement.prototype, "close", {
        configurable: true,
        value: originalClose,
      });
    } else {
      Reflect.deleteProperty(HTMLDialogElement.prototype, "close");
    }
  });

  it("renders a native dialog and opens it modally", () => {
    const { unmount } = render(
      <ModalOverlay onClose={jest.fn()} ariaLabel="测试弹窗">
        <button type="button">Inside</button>
      </ModalOverlay>
    );

    const dialog = screen.getByRole("dialog", { name: "测试弹窗" });
    expect(dialog.tagName).toBe("DIALOG");
    expect(dialog).toHaveClass("modal-overlay");
    expect(showModalMock).toHaveBeenCalledTimes(1);
    expect(screen.getByRole("button", { name: "Inside" })).toHaveFocus();

    unmount();
    expect(closeMock).toHaveBeenCalledTimes(1);
  });

  it("closes from backdrop click and native cancel but not content click", () => {
    const onClose = jest.fn();
    render(
      <ModalOverlay
        onClose={onClose}
        ariaLabel="测试弹窗"
        contentClassName="modal-content"
      >
        <button type="button">Inside</button>
      </ModalOverlay>
    );

    const dialog = screen.getByRole("dialog", { name: "测试弹窗" });
    const content = document.querySelector(".modal-content");

    expect(screen.queryByRole("button", { name: "关闭弹窗" })).not.toBeInTheDocument();

    fireEvent.click(content!);
    expect(onClose).not.toHaveBeenCalled();

    fireEvent.click(document.querySelector(".modal-backdrop-button")!);
    expect(onClose).toHaveBeenCalledTimes(1);

    fireEvent(dialog, new Event("cancel", { bubbles: false, cancelable: true }));
    expect(onClose).toHaveBeenCalledTimes(2);
  });
});
