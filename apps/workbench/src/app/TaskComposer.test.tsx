import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { workspaceOverview } from "../test/fixtures";
import { TaskComposer } from "./TaskComposer";

function stubFetch(handler: (url: string, init?: RequestInit) => unknown) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = typeof input === "string" ? input : input instanceof URL ? input.toString() : input.url;
    return { ok: true, status: 201, json: async () => handler(url, init) } as unknown as Response;
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("TaskComposer", () => {
  it("creates a session with the current Workspace Agent without querying a global catalog", async () => {
    const fetchMock = stubFetch((url) =>
      url.endsWith("/sessions") ? { id: "session-1" } : { run_id: "run-1" },
    );
    const onSessionOpened = vi.fn();
    render(
      <TaskComposer
        workspaceId={workspaceOverview.workspace.id}
        agent={workspaceOverview.agent}
        onSessionOpened={onSessionOpened}
      />,
    );

    fireEvent.change(screen.getByLabelText("新任务输入"), { target: { value: "查询商品" } });
    fireEvent.click(screen.getByRole("button", { name: "发送任务" }));

    await waitFor(() => expect(onSessionOpened).toHaveBeenCalledWith("session-1"));
    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      "/api/sessions",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ workspace_id: "ws-brand-1", agent_id: "agent-product-ops" }),
      }),
    );
    expect(fetchMock).not.toHaveBeenCalledWith("/api/agents", expect.anything());
  });

  it("blocks task submission when the Workspace has no enabled Agent", () => {
    const fetchMock = stubFetch(() => ({}));
    render(<TaskComposer workspaceId="ws-brand-1" agent={null} onSessionOpened={vi.fn()} />);

    expect(screen.getByText("当前 Workspace 未启用商品运营 Agent")).toBeInTheDocument();
    expect(screen.getByLabelText("新任务输入")).toBeDisabled();
    expect(screen.getByRole("button", { name: "发送任务" })).toBeDisabled();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
