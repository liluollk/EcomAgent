/** WebSocket 事件类型：Python 核心 AgentEvent 8 类，另包含 3 类传输层事件。 */

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
}

/** 工具调用结果：工具执行完毕的返回值 */
export interface ToolResultEvent {
  type: 'tool_result';
  tool_use_id: string;
  tool_name: string;
  result: string;
  is_error: boolean;
}

/** 权限请求：需要用户确认的工具调用 */
export interface PermissionRequestEvent {
  type: 'permission_request';
  request_id: string;
  tool_name: string;
  tool_input: Record<string, unknown>;
  reason: string;
  /** 以下为引擎注入的调价操作上下文；非调价工具的审批不带这些字段 */
  operation_id?: string;
  platform?: string;
  product_ref?: { platform?: string; shop_id?: string; product_id?: string; sku_id?: string };
  target_price?: number;
  rule_summary?: string;
}

/** 结构化错误：对应 Python 核心 AgentEvent。 */
export interface TypedErrorEvent {
  type: 'typed_error';
  error?: {
    code: string;
    title: string;
    message: string;
    can_retry?: boolean;
  } | null;
}

/** 状态事件：Agent 当前执行阶段 */
export interface StatusEvent {
  type: 'status';
  message: string;
}

/** 完成事件：当前 turn 正常结束 */
export interface CompleteEvent {
  type: 'complete';
  stop_reason?: string;
}

/** 中断事件：当前 turn 被用户或系统中断 */
export interface AbortEvent {
  type: 'abort';
  reason?: string;
}

/** WebSocket 传输层错误（不属于核心 AgentEvent） */
export interface ErrorEvent {
  type: 'error';
  message: string;
}

/** WebSocket 传输层 turn 完成标记 */
export interface TurnCompleteEvent {
  type: 'turn_complete';
}

/** WebSocket 传输层权限模式广播 */
export interface ModeChangeEvent {
  type: 'mode_change';
  mode: PermissionModeType;
}

/** WebSocket 事件联合：核心 AgentEvent + 传输层包装事件 */
export type AgentEvent =
  | TextDeltaEvent
  | ToolStartEvent
  | ToolResultEvent
  | PermissionRequestEvent
  | TypedErrorEvent
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
}

/** 权限请求在消息流中的展示信息 */
export interface PermissionInfo {
  requestId: string;
  toolName: string;
  toolInput: Record<string, unknown>;
  reason: string;
  /** 调价操作的审批上下文：审批要看到「改哪个商品的价、改成多少、触发了哪条规则」 */
  operationId?: string;
  platform?: string;
  productRef?: { product_id?: string; sku_id?: string };
  targetPrice?: number;
  ruleSummary?: string;
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
  /** 是否属于默认调价闭环；false 表示该技能的 SOP 依赖扩展工具通道 */
  default?: boolean;
  created_at?: number;
}
