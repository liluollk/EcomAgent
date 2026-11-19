import { fireEvent, render, screen } from "@testing-library/react";
import { App } from "./App";

describe("展示工作台隔离", () => {
  afterEach(() => window.history.replaceState(null, "", "/"));

  it("演示入口无需登录，确认与拒绝均不访问后端", () => {
    window.history.replaceState(null, "", "/?demo=1");
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    render(<App />);
    expect(screen.getByText("示例数据 · 不连接真实店铺")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "查看待确认任务" }));
    fireEvent.click(screen.getByRole("button", { name: "拒绝" }));
    expect(screen.getByRole("status")).toHaveTextContent(
      "已拒绝本次改价，示例价格保持 ¥99.00",
    );
    fireEvent.click(screen.getByRole("button", { name: "重新演示" }));
    fireEvent.click(screen.getByRole("button", { name: "确认执行" }));
    expect(screen.getByRole("status")).toHaveTextContent(
      "示例改价已完成，当前价格 ¥88.00",
    );
    expect(fetchMock).not.toHaveBeenCalled();
    expect(
      screen.getByRole("link", { name: "进入真实工作台" }),
    ).toHaveAttribute("href", "/");
  });
});
