import { fireEvent, render, screen, within } from "@testing-library/react";
import { workspaceDetail } from "../test/fixtures";
import { App } from "./App";

function stubWorkspaceFetch() {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () =>
      ({ ok: true, status: 200, json: async () => workspaceDetail }) as unknown as Response,
    ),
  );
}

describe("App", () => {
  it("shows only workspace, sessions and settings in the sidebar", async () => {
    stubWorkspaceFetch();
    render(<App />);

    const sidebar = screen.getByRole("navigation", { name: "功能导航" });
    const items = within(sidebar).getAllByRole("button");
    expect(items.map((item) => item.textContent)).toEqual(["Workspace", "Sessions", "Settings"]);

    expect(await screen.findByText("淘宝旗舰店")).toBeInTheDocument();
  });

  it("opens the sessions page from the sidebar", async () => {
    stubWorkspaceFetch();
    render(<App />);

    fireEvent.click(await screen.findByText("Sessions"));

    expect(screen.getByRole("heading", { name: "Sessions" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "打开会话" })).toBeInTheDocument();
  });
});
