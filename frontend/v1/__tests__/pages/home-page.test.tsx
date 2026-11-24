const redirectMock = jest.fn();

jest.mock("next/navigation", () => ({
  redirect: (...args: unknown[]) => redirectMock(...args),
}));

describe("Home", () => {
  beforeEach(() => {
    jest.clearAllMocks();
  });

  test("redirects root requests to the canonical tasks route", async () => {
    const { default: Home } = await import("@/app/page");

    Home();

    expect(redirectMock).toHaveBeenCalledWith("/tasks");
    expect(redirectMock).not.toHaveBeenCalledWith("/tasks.html");
  });
});
