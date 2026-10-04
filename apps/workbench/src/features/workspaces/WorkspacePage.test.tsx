import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { workspaceDetail } from "../../test/fixtures";
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
  it("hides configuration controls from operators", async () => {
    render(<WorkspacePage currentRole="OPERATOR" workspace={workspaceDetail} />);
    expect(await screen.findByText("淘宝旗舰店")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "编辑执行模式" })).not.toBeInTheDocument();
  });

  it("shows execution mode editing control to owners", () => {
    render(<WorkspacePage currentRole="OWNER" workspace={workspaceDetail} />);
    expect(screen.getByText("淘宝旗舰店")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "编辑执行模式" })).toBeInTheDocument();
  });

  it("renders stores, configuration blocks and member scopes in one workspace", () => {
    render(<WorkspacePage currentRole="OPERATOR" workspace={workspaceDetail} />);

    expect(screen.getByRole("region", { name: "店铺" })).toHaveTextContent("淘宝旗舰店");
    expect(screen.getByRole("region", { name: "店铺" })).toHaveTextContent("抖音小店");
    expect(screen.getByText("淘宝")).toBeInTheDocument();
    expect(screen.getByText("抖音")).toBeInTheDocument();

    expect(screen.getByRole("region", { name: "Agent" })).toHaveTextContent("商品运营 Agent");
    expect(screen.getByRole("region", { name: "Model" })).toHaveTextContent("deepseek-chat");
    expect(screen.getByRole("region", { name: "Skill" })).toHaveTextContent("改价与上架");
    expect(screen.getByRole("region", { name: "Source" })).toHaveTextContent("weknora-knowledge");

    const executionMode = screen.getByRole("region", { name: "执行模式" });
    expect(executionMode).toHaveTextContent("AWAIT_CONFIRMATION");
    expect(executionMode).toHaveTextContent("当前为只读视图");

    const members = screen.getByRole("region", { name: "成员与资源范围" });
    expect(members).toHaveTextContent("张三");
    expect(members).toHaveTextContent("李四");
    expect(members).toHaveTextContent("资源范围:淘宝旗舰店");
  });

  it("lets owners pick a mode and save it through the injected callback", async () => {
    const onSaveExecutionMode = vi.fn().mockResolvedValue(undefined);
    render(
      <WorkspacePage
        currentRole="OWNER"
        workspace={workspaceDetail}
        onSaveExecutionMode={onSaveExecutionMode}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "编辑执行模式" }));
    expect(screen.getByRole("radio", { name: "AUTO_EXECUTE" })).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "AWAIT_CONFIRMATION" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("radio", { name: "AUTO_EXECUTE" }));
    fireEvent.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() =>
      expect(onSaveExecutionMode).toHaveBeenCalledWith("ws-brand-1", "AUTO_EXECUTE"),
    );
    expect(await screen.findByRole("button", { name: "编辑执行模式" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "保存" })).not.toBeInTheDocument();
  });
});

describe("WorkspaceContainer", () => {
  it("loads the workspace through the api with credentials and renders all stores", async () => {
    const fetchMock = stubWorkspaceFetch(() => ({ body: workspaceDetail }));

    render(<WorkspaceContainer workspaceId="ws-brand-1" currentRole="OPERATOR" />);

    expect(await screen.findByText("淘宝旗舰店")).toBeInTheDocument();
    expect(screen.getByText("抖音小店")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/workspaces/ws-brand-1",
      expect.objectContaining({ credentials: "include" }),
    );
  });

  it("saves an execution mode change through the api", async () => {
    const fetchMock = stubWorkspaceFetch((url) =>
      url.endsWith("/execution-mode")
        ? { body: { mode: "AUTO_EXECUTE" } }
        : { body: { ...workspaceDetail, executionMode: "AUTO_EXECUTE" } },
    );

    render(<WorkspaceContainer workspaceId="ws-brand-1" currentRole="OWNER" />);

    fireEvent.click(await screen.findByRole("button", { name: "编辑执行模式" }));
    fireEvent.click(screen.getByRole("radio", { name: "AUTO_EXECUTE" }));
    fireEvent.click(screen.getByRole("button", { name: "保存" }));

    await waitFor(() => {
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/workspaces/ws-brand-1/execution-mode",
        expect.objectContaining({
          method: "PATCH",
          credentials: "include",
          body: JSON.stringify({ mode: "AUTO_EXECUTE" }),
        }),
      );
    });
  });
});
