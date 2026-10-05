/**
 * AgentEventReducer(计划 04 Task 10 Step 3 权威语义)。
 *
 * Reducer 是纯函数,只维护 UI 投影,不回写任何后端状态:
 * - 按 event id 去重(at-least-once 重放);
 * - 按 Run 序号推进,忽略旧序号;
 * - 检测缺口:序号跳变 → needsSnapshot=true 并停止消费,直到快照重建;
 * - 终态(COMPLETED/FAILED/CANCELLED)不可被迟到事件逆转;
 * - 事件按后端实际 event_kind 命名:RUN_STARTED/RUN_COMPLETED/RUN_FAILED/
 *   RUN_CANCELLED/RUN_RESUMED/RUN_WAITING_INPUT/RUN_WAITING_CONFIRMATION/
 *   RUN_WAITING_RECONCILIATION/TOOL_CALL_PROPOSED/TOOL_CALL_DENIED。
 *   (计划示例中的 RUN_MESSAGE_APPEND/INPUT_REQUESTED/CONFIRMATION_REQUESTED
 *   在后端不存在:消息与 Request 详情来自快照;RUN_RUNNING 作为计划原文
 *   别名归入 RUNNING。)
 */

import {
  TERMINAL_RUN_STATUSES,
  type AgentEvent,
  type MessageView,
  type SessionSnapshot,
  type ToolCallView,
} from "./types";

export interface AgentRunProjection {
  id: string;
  status: string;
  lastSequenceNo: number;
  failureCode: string | null;
}

export interface ToolCallProjection {
  runId: string;
  ordinal: number;
  /** Backend event 的 call_id;TOOL_CALL_DENIED payload 无 ordinal,按此匹配。 */
  callId: string | null;
  toolName: string | null;
  status: string;
  rawArguments: Record<string, unknown> | null;
  reasonCode?: string;
  failureCode?: string | null;
}

/** 快照 pending Input 请求的 UI 投影(字段归一为 camelCase)。 */
export interface InputInteraction {
  id: string;
  runId: string;
  fieldPaths: string[];
  question: string;
  /** 候选项统一为 {value,label};纯文本选项(旧快照)value=label。 */
  options: Array<{ value: string; label: string }>;
  status: string;
}

/** 快照 pending Confirmation 请求的 UI 投影(字段归一为 camelCase)。 */
export interface ConfirmationInteraction {
  id: string;
  runId: string;
  summary: string | null;
  normalizedArguments: Record<string, unknown> | null;
  resourceVersions: Record<string, unknown>;
  status: string;
}

export interface AgentEventProjection {
  sessionId: string;
  runs: Record<string, AgentRunProjection>;
  activeRunId: string | null;
  /** 为 true 时由 hook 重新拉取快照重建投影,并以新 cursor 重连 WS。 */
  needsSnapshot: boolean;
  /** WS 重连使用的 cursor(活动 Run 的最后已处理序号)。 */
  cursor: number | null;
  messages: MessageView[];
  /** key: `${runId}:${ordinal}`。 */
  toolCalls: Record<string, ToolCallProjection>;
  interactions: {
    inputs: InputInteraction[];
    confirmations: ConfirmationInteraction[];
  };
  seenEventIds: ReadonlySet<string>;
}

/** 迁移类事件 → 目标 Run 状态;未列出的 event_kind 不改变状态。 */
const STATUS_BY_EVENT_KIND: Record<string, string> = {
  RUN_STARTED: "RUNNING",
  RUN_RUNNING: "RUNNING", // 计划原文测试使用的别名
  RUN_RESUMED: "QUEUED",
  RUN_WAITING_INPUT: "WAITING_INPUT",
  RUN_WAITING_CONFIRMATION: "WAITING_CONFIRMATION",
  RUN_WAITING_RECONCILIATION: "WAITING_RECONCILIATION",
  RUN_COMPLETED: "COMPLETED",
  RUN_FAILED: "FAILED",
  RUN_CANCELLED: "CANCELLED",
};

/** 需要拉取 Request 详情的等待事件:事件 payload 只有 tool_call_id。 */
const SNAPSHOT_REQUIRED_EVENT_KINDS: ReadonlySet<string> = new Set([
  "RUN_WAITING_INPUT",
  "RUN_WAITING_CONFIRMATION",
]);

function toolCallKey(runId: string, ordinal: number): string {
  return `${runId}:${ordinal}`;
}

function mergeSnapshotToolCalls(
  toolCalls: Record<string, ToolCallProjection>,
  runId: string,
  snapshotToolCalls: ToolCallView[] | undefined,
): void {
  for (const toolCall of snapshotToolCalls ?? []) {
    const key = toolCallKey(runId, toolCall.ordinal);
    const existing = toolCalls[key];
    toolCalls[key] = {
      runId,
      ordinal: toolCall.ordinal,
      callId: existing?.callId ?? null,
      toolName: existing?.toolName ?? null,
      status: toolCall.status,
      rawArguments: toolCall.raw_arguments,
      failureCode: toolCall.failure_code,
    };
  }
}

/** 候选项归一:纯文本 label → {value=label};对象 → 原样。 */
function normalizeOption(item: unknown): { value: string; label: string } | null {
  if (typeof item === "string") {
    return { value: item, label: item };
  }
  if (typeof item === "object" && item !== null && "value" in item) {
    const record = item as { value: unknown; label?: unknown };
    return {
      value: String(record.value),
      label: record.label === undefined ? String(record.value) : String(record.label),
    };
  }
  return null;
}

export function projectionFromSnapshot(
  snapshot: SessionSnapshot,
  previous?: AgentEventProjection | null,
): AgentEventProjection {
  const runs: Record<string, AgentRunProjection> = {};
  let cursor: number | null = null;

  for (const run of snapshot.runs) {
    runs[run.id] = {
      id: run.id,
      status: run.status,
      lastSequenceNo: run.events_cursor,
      failureCode: run.failure_code ?? null,
    };
    if (run.id === snapshot.active_run_id) {
      cursor = run.events_cursor;
    }
  }

  // 快照的 ToolCallView 不含工具名:重建时先带入旧投影中由
  // TOOL_CALL_PROPOSED 等事件获得的知识(工具名/call_id),再由
  // mergeSnapshotToolCalls 用快照事实(状态/参数/失败码)覆盖。
  const toolCalls: Record<string, ToolCallProjection> = {};
  if (previous) {
    for (const [key, prior] of Object.entries(previous.toolCalls)) {
      if (prior.toolName !== null || prior.callId !== null) {
        toolCalls[key] = prior;
      }
    }
  }
  const activeRun = snapshot.active_run_id ? runs[snapshot.active_run_id] : undefined;
  if (activeRun) {
    mergeSnapshotToolCalls(toolCalls, activeRun.id, snapshot.tool_calls);
  }

  const messages = [...snapshot.messages].sort(
    (left, right) => left.sequence_no - right.sequence_no,
  );

  return {
    sessionId: snapshot.id,
    runs,
    activeRunId: snapshot.active_run_id,
    needsSnapshot: false,
    cursor,
    messages,
    toolCalls,
    interactions: {
      inputs: (snapshot.pending_inputs ?? []).map((request) => ({
        id: request.id,
        runId: request.run_id,
        fieldPaths: request.field_paths,
        question: request.question,
        options: (request.options ?? [])
          .map(normalizeOption)
          .filter((option): option is { value: string; label: string } => option !== null),
        status: request.status,
      })),
      confirmations: (snapshot.pending_confirmations ?? []).map((request) => ({
        id: request.id,
        runId: request.run_id,
        summary: request.summary ?? null,
        normalizedArguments: request.normalized_arguments ?? null,
        resourceVersions: request.resource_versions ?? {},
        status: request.status,
      })),
    },
    seenEventIds: new Set<string>(),
  };
}

export function reduceAgentEvent(
  projection: AgentEventProjection,
  event: AgentEvent,
): AgentEventProjection {
  // 缺口或等待详情后停止消费,直到快照重建。
  if (projection.needsSnapshot) {
    return projection;
  }
  // 按 event id 去重(at-least-once 重放)。
  if (projection.seenEventIds.has(event.id)) {
    return projection;
  }
  const run = projection.runs[event.runId];
  if (!run) {
    // 未知 Run:需要包含该 Run 的快照。
    return { ...projection, needsSnapshot: true };
  }
  // 旧序号:忽略,不算缺口。
  if (event.sequenceNo <= run.lastSequenceNo) {
    return projection;
  }
  // 缺口:序号跳变,停止消费并等待快照重建。
  if (event.sequenceNo !== run.lastSequenceNo + 1) {
    return { ...projection, needsSnapshot: true };
  }

  const nextStatus = STATUS_BY_EVENT_KIND[event.kind];
  const isTerminal = TERMINAL_RUN_STATUSES.includes(run.status);
  // 终态不可被任何状态迁移事件逆转;序号仍推进以免后续事件误判缺口。
  const status = nextStatus === undefined || isTerminal ? run.status : nextStatus;
  const failureCode =
    !isTerminal && event.kind === "RUN_FAILED"
      ? typeof event.payload.failure_code === "string"
        ? event.payload.failure_code
        : run.failureCode
      : run.failureCode;

  const next: AgentEventProjection = {
    ...projection,
    runs: {
      ...projection.runs,
      [event.runId]: { ...run, status, lastSequenceNo: event.sequenceNo, failureCode },
    },
    seenEventIds: new Set(projection.seenEventIds).add(event.id),
  };

  if (event.runId === projection.activeRunId) {
    next.cursor = event.sequenceNo;
  }

  switch (event.kind) {
    case "TOOL_CALL_PROPOSED": {
      const ordinal = Number(event.payload.ordinal);
      const key = toolCallKey(event.runId, ordinal);
      next.toolCalls = {
        ...projection.toolCalls,
        [key]: {
          runId: event.runId,
          ordinal,
          callId: typeof event.payload.call_id === "string" ? event.payload.call_id : null,
          toolName: typeof event.payload.tool_name === "string" ? event.payload.tool_name : null,
          status: "PROPOSED",
          rawArguments: projection.toolCalls[key]?.rawArguments ?? null,
        },
      };
      break;
    }
    case "TOOL_CALL_DENIED": {
      const callId = typeof event.payload.call_id === "string" ? event.payload.call_id : null;
      const denied = Object.entries(projection.toolCalls).find(
        ([key, entry]) =>
          entry.runId === event.runId &&
          (callId !== null ? entry.callId === callId : key === toolCallKey(event.runId, Number(event.payload.ordinal))),
      );
      if (denied) {
        const [key, entry] = denied;
        next.toolCalls = {
          ...projection.toolCalls,
          [key]: {
            ...entry,
            status: "DENIED",
            reasonCode:
              typeof event.payload.reason_code === "string" ? event.payload.reason_code : undefined,
          },
        };
      }
      break;
    }
    case "RUN_RESUMED": {
      // 回答/裁决后 Run 迁回 QUEUED,对应的待处理 Request 已被消费。
      next.interactions = {
        inputs: projection.interactions.inputs.filter((item) => item.runId !== event.runId),
        confirmations: projection.interactions.confirmations.filter(
          (item) => item.runId !== event.runId,
        ),
      };
      break;
    }
    default:
      break;
  }

  if (SNAPSHOT_REQUIRED_EVENT_KINDS.has(event.kind)) {
    // 等待事件的 payload 只有 tool_call_id,Input/Confirmation 详情须从快照获取。
    next.needsSnapshot = true;
  }

  return next;
}
