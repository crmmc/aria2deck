import { fireEvent, render, screen } from "@testing-library/react";
import PasswordWarningBanner from "@/components/PasswordWarningBanner";

const pushMock = jest.fn();

jest.mock("next/navigation", () => ({
  useRouter: () => ({ push: pushMock }),
}));

const initialPasswordUser = {
  id: 1,
  username: "admin",
  is_admin: true,
  quota: 1024,
  is_initial_password: true,
};

describe("PasswordWarningBanner", () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  test("renders for initial-password users and navigates to profile", () => {
    render(<PasswordWarningBanner user={initialPasswordUser} />);

    expect(screen.getByText("安全提醒")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "前往修改" }));
    expect(pushMock).toHaveBeenCalledWith("/profile");
  });

  test("can be dismissed and stays hidden for users without the initial password flag", () => {
    const { rerender } = render(<PasswordWarningBanner user={initialPasswordUser} />);

    fireEvent.click(screen.getByTitle("关闭"));
    expect(screen.queryByText("安全提醒")).not.toBeInTheDocument();

    rerender(<PasswordWarningBanner user={{ ...initialPasswordUser, is_initial_password: false }} />);
    expect(screen.queryByText("安全提醒")).not.toBeInTheDocument();
  });
});
