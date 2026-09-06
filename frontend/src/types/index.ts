/** 后端 WebSocket 事件类型定义，对应 Python 的 AgentEvent 六类事件 */

/** 文本增量：LLM 流式输出的 token 片段 */
export interface TextDeltaEvent {
  type: 'text_delta';
  text: string;
}

/** 工具调用开始：Agent 决定调用某个工具 */
export interface ToolStartEvent {
  type: 'tool_start';
  tool_name: string;
  tool_use_id: string;
  input: Record<string, unknown>;
  /** 本次执行链路 ID（Execution Policy 注入，与配对 tool_result 同值） */
  trace_id?: string;
  /** 工具来源：commerce / mcp / skill */
  source?: string;
}

/** 工具调用结果：工具执行完毕的返回值 */
export interface ToolResultEvent {
  type: 'tool_result';
  tool_use_id: string;
  tool_name: string;
  result: string;
  is_error: boolean;
  /** 实际执行尝试次数（重试后 > 1） */
  attempt?: number;
  /** 执行总耗时（含重试与退避等待），毫秒 */
  duration_ms?: number;
  /** 命中幂等回放：同键重复请求未重复产生副作用 */
  idempotent_replay?: boolean;
  trace_id?: string;
  source?: string;
}

/** 权限请求：需要用户确认的工具调用 */
export interface PermissionRequestEvent {
  type: 'permission_request';
  request_id: string;
  tool_name: string;
  tool_input: Record<string, unknown>;
  reason: string;
}

/** 状态事件：Agent 当前执行阶段 */
export interface StatusEvent {
  type: 'status';
  message: string;
}

/** 完成事件：当前 turn 正常结束 */
export interface CompleteEvent {
  type: 'complete';
  stop_reason: string;
}

/** 中断事件：当前 turn 被用户或系统中断 */
export interface AbortEvent {
  type: 'abort';
  reason?: string;
}

/** 错误事件 */
export interface ErrorEvent {
  type: 'error';
  message: string;
}

/** turn 完成标记 */
export interface TurnCompleteEvent {
  type: 'turn_complete';
}

/** 权限模式变更 */
export interface ModeChangeEvent {
  type: 'mode_change';
  mode: PermissionModeType;
}

/** 所有事件类型的联合 */
export type AgentEvent =
  | TextDeltaEvent
  | ToolStartEvent
  | ToolResultEvent
  | PermissionRequestEvent
  | StatusEvent
  | CompleteEvent
  | AbortEvent
  | ErrorEvent
  | TurnCompleteEvent
  | ModeChangeEvent;

/** 权限模式：对应后端 PermissionMode */
export type PermissionModeType = 'READONLY' | 'ASK' | 'EXECUTE';

/** 工具调用信息：用于 UI 卡片展示 */
export interface ToolCallInfo {
  toolUseId: string;
  toolName: string;
  input: Record<string, unknown>;
  result: string | null;
  isError: boolean;
  status: 'pending' | 'running' | 'done' | 'error';
  /** 执行元数据（Execution Policy 回填，历史会话无此字段） */
  attempt?: number;
  durationMs?: number;
  idempotentReplay?: boolean;
  traceId?: string;
  source?: string;
}

/** 权限请求在消息流中的展示信息 */
export interface PermissionInfo {
  requestId: string;
  toolName: string;
  toolInput: Record<string, unknown>;
  reason: string;
}

/** 聊天消息：在 UI 中展示的完整消息 */
export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant' | 'system';
  content: string;
  toolCalls: ToolCallInfo[];
  isStreaming: boolean;
  timestamp: number;
  permission?: PermissionInfo;
}

/** 会话信息（后端返回） */
export interface SessionInfo {
  session_id: string;
  workspace_id: string;
  status: string;
  user?: { user_id?: string; role?: string };
  message_count?: number;
  permission_mode?: PermissionModeType;
  model_state?: { provider?: string; model?: string };
}

/** 会话信息（含本地展示字段） */
export interface SessionMeta extends SessionInfo {
  title?: string;
  createdAt?: number;
}

/** 模型供应商（后端 /providers 返回，api_key 已掩码） */
export interface ModelProvider {
  name: string;
  label: string;
  provider: 'openai' | 'anthropic' | 'mock';
  default_model: string;
  api_key?: string;
  api_base?: string;
  model_list: string[];
  enabled: boolean;
  thinking_level?: 'low' | 'medium' | 'high';
  created_at?: number;
}

/** GET /providers 响应体 */
export interface ProvidersResponse {
  providers: ModelProvider[];
  active: string;
}

/** 渠道平台类型（后端渠道配置的 platform 字段） */
export type ChannelPlatform = 'mock' | 'taobao' | 'jd' | 'douyin' | 'open';

/** 渠道来源（后端 /sources 返回，api_key 等敏感字段已掩码） */
export interface ChannelSource {
  name: string;
  label: string;
  base_url: string;
  platform: ChannelPlatform;
  auth_type: string;
  api_key?: string;
  options?: Record<string, unknown>;
  enabled: boolean;
  created_at?: number;
}

/** 平台展示名映射 */
export const PLATFORM_LABELS: Record<ChannelPlatform, string> = {
  mock: '本地 Mock',
  taobao: '淘宝 TOP',
  jd: '京东 JOS',
  douyin: '抖音开放平台',
  open: '自定义开放平台',
};

export const CHANNEL_PLATFORMS: ChannelPlatform[] = ['mock', 'taobao', 'jd', 'douyin', 'open'];

/** 技能（后端 /skills 返回，SKILL.md 知识包） */
export interface Skill {
  name: string;
  description: string;
  keywords: string[];
  prerequisites: string[];
  body: string;
  enabled: boolean;
  builtin: boolean;
  created_at?: number;
}
