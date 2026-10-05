import { useCallback, useState } from "react";

import { getJSON, postJSON } from "../../api/client";
import type { InputInteraction, ToolCallProjection } from "./eventReducer";
import { TERMINAL_RUN_STATUSES, type SessionSnapshot } from "./types";
import { ConfirmationCard, type ConfirmationDecision } from "./ConfirmationCard";
import { InputRequestCard } from "./InputRequestCard";
import { ToolCallCard } from "./ToolCallCard";
import { useAgentEvents } from "./useAgentEvents";

/**
 * Session 对话页(计划 04 Task 10 Step 3):
 * 中间主区域展示消息、ToolCall、Input Request、Confirmation Request、
 * 错误和最终结果;不建设独立右侧 Run 视图。编辑器在存在活动等待 Run 时
 * 禁止新普通任务。所有动作走 api/client,不直接回写 Runtime 状态。
 */

interface SessionApiResponse {
  id: string;
  title: string | null;
  status: string;
  active_run_id: string | null;
  messages: SessionSnapshot["messages"];
  runs: Array<{ id: string; status: string; created_at: string }>;
  pending_inputs?: SessionSnapshot["pending_inputs"];
  pending_confirmations?: SessionSnapshot["pending_confirmations"];
}

interface RunApiResponse {
  id: string;
  status: string;
  state_version: number;
  events_cursor: number;
  tool_calls: NonNullable<SessionSnapshot["tool_calls"]>;
  failure_code?: string | null;
  pending_inputs?: SessionSnapshot["pending_inputs"];
  pending_confirmations?: SessionSnapshot["pending_confirmations"];
}

/**
 * 合并 Session 快照与活动 Run 快照:
 * Session 快照提供全量消息与 Run 摘要;Run 快照提供 events_cursor
 * (WS cursor)与 tool_calls。pending 请求详情当前后端未返回,
 * 字段按可选透传,存在时渲染。
 */
export async function loadSessionSnapshot(sessionId: string): Promise<SessionSnapshot> {
  const session = await getJSON<SessionApiResponse>(`/sessions/${sessionId}`);
  // 活动 Run 提供 WS cursor;无活动 Run 时取最近一个 Run 以展示终态详情
  // (如失败原因)。
  const detailRunId = session.active_run_id ?? session.runs.at(-1)?.id ?? null;
  const run = detailRunId
    ? await getJSON<RunApiResponse>(`/runs/${detailRunId}`).catch(() => null)
    : null;

  return {
    id: session.id,
    title: session.title,
    status: session.status,
    active_run_id: session.active_run_id,
    messages: session.messages,
    runs: session.runs.map((item) =>
      run !== null && item.id === run.id
        ? {
            id: item.id,
            status: run.status,
            events_cursor: run.events_cursor,
            failure_code: run.failure_code ?? null,
          }
        : { id: item.id, status: item.status, events_cursor: 0 },
    ),
    tool_calls: run?.tool_calls,
    pending_inputs: session.pending_inputs ?? run?.pending_inputs,
    pending_confirmations: session.pending_confirmations ?? run?.pending_confirmations,
  };
}

const runStatusLabels: Record<string, string> = {
  QUEUED: "排队中",
  RUNNING: "运行中",
  WAITING_INPUT: "等待输入",
  WAITING_CONFIRMATION: "等待确认",
  WAITING_RECONCILIATION: "等待对账",
  COMPLETED: "已完成",
  FAILED: "失败",
  CANCELLED: "已取消",
};

const messageRoleLabels: Record<string, string> = {
  USER: "用户",
  ASSISTANT: "助手",
  TOOL: "工具结果",
  SESSION_SUMMARY: "会话摘要",
};

function runStatusLabel(status: string): string {
  return runStatusLabels[status] ?? status;
}

function messageRoleLabel(role: string): string {
  return messageRoleLabels[role] ?? role;
}

interface ToolReceiptData {
  sku?: unknown;
  after_price?: unknown;
}

/**
 * 从写操作 ToolResult 消息中解析权威回读价(平台写后回执的 after_price,
 * 即 Mock/平台侧真实落库的当前价);解析失败返回 null,不影响渲染。
 */
function parseWritePrice(content: string): { sku: string; price: string } | null {
  try {
    const parsed = JSON.parse(content) as { data?: ToolReceiptData };
    const data = parsed.data;
    if (
      data !== undefined &&
      typeof data.sku === "string" &&
      typeof data.after_price === "string"
    ) {
      return { sku: data.sku, price: data.after_price };
    }
    return null;
  } catch {
    return null;
  }
}

export interface SessionPageProps {
  sessionId: string;
}

export function SessionPage({ sessionId }: SessionPageProps) {
  const loadSnapshot = useCallback(() => loadSessionSnapshot(sessionId), [sessionId]);
  const { projection, connectionError, reload } = useAgentEvents({
    sessionId,
    loadSnapshot,
  });

  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const activeRun =
    projection?.activeRunId && projection.runs[projection.activeRunId]
      ? projection.runs[projection.activeRunId]
      : undefined;
  // 存在活动等待 Run:禁止提交新普通任务(后端会以 ACTIVE_RUN_EXISTS 拒绝)。
  const runLocked =
    activeRun !== undefined && !TERMINAL_RUN_STATUSES.includes(activeRun.status);

  const reportActionError = (cause: unknown) => {
    setActionError(cause instanceof Error ? cause.message : String(cause));
  };

  const sendMessage = async () => {
    const content = draft.trim();
    if (!content || sending) {
      return;
    }
    setSending(true);
    setActionError(null);
    try {
      await postJSON(`/sessions/${sessionId}/messages`, { content });
      setDraft("");
      await reload();
    } catch (cause) {
      reportActionError(cause);
    } finally {
      setSending(false);
    }
  };

  const answerInput = useCallback(
    async (requestId: string, values: Record<string, unknown>) => {
      setActionError(null);
      try {
        await postJSON(`/requests/${requestId}/answer`, { values });
        await reload();
      } catch (cause) {
        reportActionError(cause);
      }
    },
    [reload],
  );

  const decideConfirmation = useCallback(
    async (requestId: string, decision: ConfirmationDecision) => {
      setActionError(null);
      try {
        await postJSON(`/requests/${requestId}/confirmation/${decision}`, {});
        await reload();
      } catch (cause) {
        reportActionError(cause);
      }
    },
    [reload],
  );

  if (!projection) {
    return <p>正在加载会话…</p>;
  }

  const runs = Object.values(projection.runs);
  const toolCalls: ToolCallProjection[] = Object.values(projection.toolCalls).sort(
    (left, right) => left.ordinal - right.ordinal,
  );
  const inputs: InputInteraction[] = projection.interactions.inputs;
  // 权威价回读:最新一次写操作回执中的 after_price(平台侧真实当前价)。
  const writePrices = projection.messages
    .filter((message) => message.role === "TOOL")
    .map((message) => parseWritePrice(message.content))
    .filter((price): price is { sku: string; price: string } => price !== null);
  const latestPrice = writePrices.at(-1) ?? null;

  return (
    <section aria-label={`Session ${sessionId}`}>
      <h1>Session 对话</h1>
      {connectionError && <p role="status">{connectionError}</p>}

      <section aria-label="Run 状态">
        <h2>Run 状态</h2>
        <ul>
          {runs.map((run) => (
            <li key={run.id} data-run-id={run.id}>
              <span>Run {run.id}</span>
              <span>{runStatusLabel(run.status)}</span>
              {run.status === "FAILED" && run.failureCode && (
                <span>失败原因:{run.failureCode}</span>
              )}
            </li>
          ))}
        </ul>
      </section>

      {latestPrice && (
        <section aria-label="权威价格">
          <h2>权威价格</h2>
          <p>{`${latestPrice.sku} 当前价格：${latestPrice.price} 元`}</p>
        </section>
      )}

      <section aria-label="对话消息">
        <h2>对话</h2>
        <ul>
          {projection.messages.map((message) => (
            <li key={message.id} data-role={message.role}>
              <span>{messageRoleLabel(message.role)}:</span>
              <span>{message.content}</span>
            </li>
          ))}
        </ul>
      </section>

      <section aria-label="工具调用">
        <h2>工具调用</h2>
        {toolCalls.length > 0 ? (
          <ul>
            {toolCalls.map((toolCall) => (
              <ToolCallCard key={`${toolCall.runId}:${toolCall.ordinal}`} toolCall={toolCall} />
            ))}
          </ul>
        ) : (
          <p>暂无工具调用。</p>
        )}
      </section>

      {inputs.map((request) => (
        <InputRequestCard key={request.id} request={request} onAnswer={answerInput} />
      ))}
      {projection.interactions.confirmations.map((request) => (
        <ConfirmationCard key={request.id} request={request} onDecide={decideConfirmation} />
      ))}

      <section aria-label="任务编辑器">
        <h2>提交新任务</h2>
        <textarea
          aria-label="任务输入"
          value={draft}
          disabled={runLocked || sending}
          onChange={(changeEvent) => setDraft(changeEvent.target.value)}
        />
        {runLocked && <p>当前存在进行中的任务,请等待其完成后再提交新任务。</p>}
        <button
          type="button"
          onClick={sendMessage}
          disabled={runLocked || sending || draft.trim().length === 0}
        >
          发送
        </button>
        {actionError && <p role="alert">{actionError}</p>}
      </section>
    </section>
  );
}
