import { fireEvent, render, screen } from "@testing-library/react";
import AuthenticatedLayout from "@/app/(authenticated)/layout";

const retryAuthMock = jest.fn();

const authState = {
  user: null as {
    id: number;
    username: string;
    is_admin: boolean;
    quota: number;
    is_initial_password: boolean;
  } | null,
  loading: false,
  error: null as string | null,
  retryAuth: retryAuthMock,
  sidebarExpanded: false,
};

jest.mock("@/lib/AuthContext", () => ({
  __esModule: true,
  useAuth: () => authState,
}));

jest.mock("@/components/Sidebar", () => ({
  __esModule: true,
  default: ({ user }: { user: { username: string } | null }) => (
    <div data-testid="sidebar">{user?.username ?? "anonymous"}</div>
  ),
}));

jest.mock("@/components/PasswordWarningBanner", () => ({
  __esModule: true,
  default: ({ user }: { user: { username: string } | null }) => (
    <div data-testid="password-banner">{user?.username ?? "anonymous"}</div>
  ),
}));

describe("AuthenticatedLayout", () => {
  beforeEach(() => {
    retryAuthMock.mockClear();
    authState.user = null;
    authState.loading = false;
    authState.error = null;
    authState.sidebarExpanded = false;
  });

  test("shows the loading skeleton while auth is resolving", () => {
    authState.loading = true;

    render(<AuthenticatedLayout><div>content</div></AuthenticatedLayout>);

    expect(document.querySelector(".skeleton-header")).toBeInTheDocument();
    expect(screen.queryByText("content")).not.toBeInTheDocument();
  });

  test("renders the retry view when auth fails without a user", () => {
    authState.error = "服务器不可用";

    render(<AuthenticatedLayout><div>content</div></AuthenticatedLayout>);

    expect(screen.getByText("连接失败")).toBeInTheDocument();
    expect(screen.getByText("服务器不可用")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "重试" }));
    expect(retryAuthMock).toHaveBeenCalled();
  });

  test("renders authenticated shell content and expanded layout class", () => {
    authState.user = {
      id: 1,
      username: "admin",
      is_admin: true,
      quota: 1024,
      is_initial_password: false,
    };
    authState.sidebarExpanded = true;

    render(<AuthenticatedLayout><div>content</div></AuthenticatedLayout>);

    expect(screen.getByTestId("sidebar")).toHaveTextContent("admin");
    expect(screen.getByTestId("password-banner")).toHaveTextContent("admin");
    expect(screen.getByText("content")).toBeInTheDocument();
    expect(document.querySelector(".main-content")).toHaveClass("sidebar-expanded");
  });
});
