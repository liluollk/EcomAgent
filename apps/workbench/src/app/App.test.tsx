import { fireEvent, render, screen, within } from "@testing-library/react";
import { workspaceOverview } from "../test/fixtures";
import { App } from "./App";

interface StubRequest {
  url: string;
  init?: RequestInit;
}

function stubAppFetch(handler: (request: StubRequest) => unknown) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url =
      typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
    const body = handler({ url, init });
    return { ok: true, status: 200, json: async () => body } as unknown as Response;
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

function stubWorkspaces() {
  return stubAppFetch(({ url }) => {
    if (url.endsWith("/api/workspaces")) {
      return {
        workspaces: [
          { id: "ws-brand-1", name: "品牌一" },
          { id: "ws-brand-2", name: "品牌二" },
        ],
      };
    }
    if (url.endsWith("/ws-brand-2/overview")) {
      return {
        ...workspaceOverview,
        workspace: { ...workspaceOverview.workspace, id: "ws-brand-2", name: "品牌二" },
        stores: [{ id: "store-b", name: "品牌二抖音店", platform: "DOUYIN", status: "ACTIVE" }],
      };
    }
    return workspaceOverview;
  });
}

describe("App", () => {
  it("shows only workspace, sessions and settings in the sidebar", async () => {
    stubWorkspaces();
    sessionStorage.setItem("workbench.token", "test-token");
    render(<App />);

    const sidebar = screen.getByRole("navigation", { name: "功能导航" });
    const items = within(sidebar).getAllByRole("button");
    expect(items.map((item) => item.textContent)).toEqual(["Workspace", "Sessions", "Settings"]);

    // 默认展示工作区选择;选择"品牌一"后加载工作区配置(含店铺列表)。
    fireEvent.click(await screen.findByRole("button", { name: "品牌一" }));
    expect(await screen.findByText("淘宝旗舰店")).toBeInTheDocument();
  });

  it("switches workspace and loads only that workspace's overview", async () => {
    stubWorkspaces();
    sessionStorage.setItem("workbench.token", "test-token");
    render(<App />);

    fireEvent.click(await screen.findByRole("button", { name: "品牌一" }));
    expect(await screen.findByText("淘宝旗舰店")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "切换工作区" }));
    fireEvent.click(await screen.findByRole("button", { name: "品牌二" }));

    expect(await screen.findByText("品牌二抖音店")).toBeInTheDocument();
    expect(screen.queryByText("淘宝旗舰店")).not.toBeInTheDocument();
  });

  it("opens the sessions page from the sidebar", async () => {
    stubWorkspaces();
    sessionStorage.setItem("workbench.token", "test-token");
    render(<App />);

    fireEvent.click(await screen.findByText("Sessions"));

    expect(screen.getByRole("heading", { name: "Sessions" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "打开会话" })).toBeInTheDocument();
  });
});
