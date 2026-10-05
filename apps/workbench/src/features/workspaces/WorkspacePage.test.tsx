import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { workspaceOverview } from "../../test/fixtures";
import { WorkspaceContainer } from "./WorkspaceContainer";
import { WorkspacePage } from "./WorkspacePage";

interface StubResponse {
  status?: number;
  body: unknown;
}

function stubWorkspaceFetch(handler: (url: string, init?: RequestInit) => StubResponse) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url =
      typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
    const { status = 200, body } = handler(url, init);
    return {
      ok: status < 400,
      status,
      json: async () => body,
    } as unknown as Response;
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("WorkspacePage", () => {
  it("uses the current membership role and canonical execution modes", () => {
    render(<WorkspacePage overview={workspaceOverview} />);

    expect(screen.getByText("淘宝旗舰店")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "编辑执行模式" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "执行模式" })).toHaveTextContent("CONFIRM_WRITES");
  });

  it("does not expose configuration controls to operators", () => {
    render(
      <WorkspacePage
        overview={{
          ...workspaceOverview,
          current_membership: { ...workspaceOverview.current_membership, role: "OPERATOR" },
        }}
      />,
    );
    expect(screen.queryByRole("button", { name: "编辑执行模式" })).not.toBeInTheDocument();
  });

  it("renders the overview's stores, capabilities, policies, and current-member scope", () => {
    render(<WorkspacePage overview={workspaceOverview} />);

    expect(screen.getByRole("region", { name: "店铺" })).toHaveTextContent("淘宝旗舰店");
    expect(screen.getByRole("region", { name: "店铺" })).toHaveTextContent("抖音小店");
    expect(screen.getByRole("region", { name: "Agent" })).toHaveTextContent("商品运营 Agent");
    expect(screen.getByRole("region", { name: "Model" })).toHaveTextContent("deepseek-chat");
    expect(screen.getByRole("region", { name: "Skill" })).toHaveTextContent("改价与上架");
    expect(screen.getByRole("region", { name: "Source" })).toHaveTextContent("运营知识库");
    expect(screen.getByRole("region", { name: "业务规则" })).toHaveTextContent("最低价规则");
    expect(screen.getByRole("region", { name: "当前成员权限" })).toHaveTextContent(
      "store:store-tb",
    );
  });

  it("renders an explicit empty Agent state", () => {
    render(<WorkspacePage overview={{ ...workspaceOverview, agent: null }} />);
    expect(screen.getByText("当前 Workspace 未启用 Agent" )).toBeInTheDocument();
  });

  it("lets owners save one of the three execution modes", async () => {
    const onSaveExecutionMode = vi.fn().mockResolvedValue(undefined);
    render(<WorkspacePage overview={workspaceOverview} onSaveExecutionMode={onSaveExecutionMode} />);

    fireEvent.click(screen.getByRole("button", { name: "编辑执行模式" }));
    expect(screen.getByRole("radio", { name: "READ_ONLY" })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "CONFIRM_WRITES" })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "AUTO_EXECUTE" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("radio", { name: "AUTO_EXECUTE" }));
    fireEvent.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() =>
      expect(onSaveExecutionMode).toHaveBeenCalledWith("ws-brand-1", "AUTO_EXECUTE"),
    );
  });
});

describe("WorkspaceContainer", () => {
  it("loads the aggregate overview endpoint and renders its data", async () => {
    const fetchMock = stubWorkspaceFetch(() => ({ body: workspaceOverview }));

    render(<WorkspaceContainer workspaceId="ws-brand-1" />);

    expect(await screen.findByText("淘宝旗舰店")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/workspaces/ws-brand-1/overview",
      expect.objectContaining({ credentials: "include" }),
    );
  });

  it("fails visibly when the server omits required overview fields", async () => {
    stubWorkspaceFetch(() => ({ body: { workspace: workspaceOverview.workspace } }));

    render(<WorkspaceContainer workspaceId="ws-brand-1" />);

    expect(await screen.findByRole("alert")).toHaveTextContent("工作台数据格式不完整");
  });

  it("saves the mode then reloads the authoritative overview", async () => {
    let mode: "CONFIRM_WRITES" | "AUTO_EXECUTE" = "CONFIRM_WRITES";
    const fetchMock = stubWorkspaceFetch((url, init) => {
      if (url.endsWith("/execution-mode")) {
        mode = JSON.parse(String(init?.body)).mode as typeof mode;
        return { body: { mode } };
      }
      return { body: { ...workspaceOverview, workspace: { ...workspaceOverview.workspace, execution_mode: mode } } };
    });

    render(<WorkspaceContainer workspaceId="ws-brand-1" />);
    fireEvent.click(await screen.findByRole("button", { name: "编辑执行模式" }));
    fireEvent.click(screen.getByRole("radio", { name: "AUTO_EXECUTE" }));
    fireEvent.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    expect(screen.getByRole("region", { name: "执行模式" })).toHaveTextContent("AUTO_EXECUTE");
  });
});
