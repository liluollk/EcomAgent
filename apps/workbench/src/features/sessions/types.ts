/**
 * Session 视图类型(计划 04 Task 10)。
 *
 * 命名对齐后端实际协议:
 * - WS /api/runs/{run_id}/events 推送 {id, sequence_no, event_kind, payload, created_at};
 * - GET /api/sessions/{id} 返回会话快照(messages/runs/active_run_id);
 * - GET /api/runs/{id} 返回 Run 快照(status/state_version/events_cursor/messages/tool_calls)。
 * 待处理 Input/Confirmation Request 详情目前后端快照未返回,类型按可选字段定义,
 * 由前端在快照提供 `pending_inputs`/`pending_confirmations` 时渲染。
 */

export type RunStatus =
  | "QUEUED"
  | "RUNNING"
  | "WAITING_INPUT"
  | "WAITING_CONFIRMATION"
  | "WAITING_RECONCILIATION"
  | "COMPLETED"
  | "FAILED"
  | "CANCELLED";

export const TERMINAL_RUN_STATUSES: readonly string[] = ["COMPLETED", "FAILED", "CANCELLED"];

export interface MessageView {
  id: string;
  run_id: string | null;
  role: string;
  status: string;
  content: string;
  sequence_no: number;
}

export interface ToolCallView {
  id: string;
  ordinal: number;
  status: string;
  raw_arguments: Record<string, unknown>;
  failure_code: string | null;
}

/** Input Request 的候选项(value 为提交值,label 为展示名)。 */
export interface InputOptionView {
  value: string;
  label: string;
}

export interface InputRequestView {
  id: string;
  run_id: string;
  field_paths: string[];
  question: string;
  /** 候选项(如店铺);兼容纯文本 label(旧快照)与 {value,label}。 */
  options?: Array<InputOptionView | string>;
  status: string;
  created_at?: string;
}

export interface ConfirmationRequestView {
  id: string;
  run_id: string;
  /** 待执行写操作的摘要。 */
  summary?: string;
  normalized_arguments?: Record<string, unknown>;
  resource_versions?: Record<string, unknown>;
  status: string;
  created_at?: string;
}

export interface SessionRunSnapshot {
  id: string;
  status: string;
  /** Run 快照的 events_cursor(最后已持久事件序号)。 */
  events_cursor: number;
  failure_code?: string | null;
}

export interface SessionSnapshot {
  id: string;
  title?: string | null;
  status?: string;
  active_run_id: string | null;
  messages: MessageView[];
  runs: SessionRunSnapshot[];
  tool_calls?: ToolCallView[];
  pending_inputs?: InputRequestView[];
  pending_confirmations?: ConfirmationRequestView[];
}

/** WS 推送的 wire 事件;runId 由连接目标 Run 提供(WS 按 Run 建立)。 */
export interface AgentEvent {
  id: string;
  runId: string;
  sequenceNo: number;
  kind: string;
  payload: Record<string, unknown>;
  createdAt?: string;
}
