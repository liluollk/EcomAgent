/**
 * mock 事件流生成器 — 模拟后端 WebSocket 推送的 AgentEvent 序列。
 *
 * sendMessage 按序派发：status → text_delta（流式）→ tool_start →
 * （ASK 模式写操作 → permission_request 挂起）→ tool_result → text_delta 结论
 * → complete。支持 abort 中断、respondPermission 续推、sendModeChange 回发。
 *
 * 演示的是**多平台商品调价闭环**：查快照 → 提交目标价 → 审批 → 执行 → 结果回查。
 * 默认 Agent 只暴露 query_product_snapshot 与 update_price 两个业务工具，
 * 因此这里的意图解析也只产出这两个（外加 save_skill 元技能）。
 *
 * 事件形状与后端 events/agent_event.py 序列化一致（见 types/index.ts AgentEvent）。
 */

import type { AgentEvent, PermissionModeType } from '../types';
import { mockStore, type MockMessage } from './apiMock';

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

type OnEvent = (event: AgentEvent) => void;

interface PendingPermission {
  requestId: string;
  toolName: string;
  resolve: (approved: boolean) => void;
}

/** 平台识别词表：淘宝 / 天猫 → taobao，抖店 / 抖音 → douyin */
const PLATFORM_WORDS: Array<[RegExp, string]> = [
  [/淘宝|天猫/, 'taobao'],
  [/抖店|抖音/, 'douyin'],
];

/** 与 mock 平台侧一致的出厂数据：成本价不由调用方提供，活动锁价的 SKU 无法改价 */
const COST_PRICE = 59;
const LOCKED_SKU = 'SKU-001';

/** 从用户消息解析平台 / 商品 / SKU / 目标价（与后端剧本后端的新契约一致） */
function inferTool(text: string) {
  const platform = PLATFORM_WORDS.find(([re]) => re.test(text))?.[1] ?? 'taobao';
  const product = text.match(/ITEM-\d+/)?.[0] ?? 'ITEM-1001';
  const sku = text.match(/SKU-\d+/)?.[0] ?? 'SKU-002';
  const price = text.match(/(?:调到|改为|调整到|调整为|调整至|改成|改到)\s*(\d+(?:\.\d+)?)/)?.[1];
  if (price !== undefined) {
    return {
      toolName: 'update_price',
      input: { platform, product_id: product, sku_id: sku, target_price: Number(price) },
    };
  }
  return {
    toolName: 'query_product_snapshot',
    input: { platform, product_id: product, sku_id: sku },
  };
}

const WRITE_TOOLS = new Set(['update_price', 'save_skill']);

function snapshotText(platform: string, product: string, sku: string): string {
  const activity =
    sku === LOCKED_SKU
      ? '双11预热 9折（活动锁价，提交改价可能被拦截）'
      : '无';
  const stock = sku === LOCKED_SKU ? 1523 : 100;
  return `平台 ${platform} 商品 ${product}/${sku}：当前价 89.00 元，库存 ${stock} 件，状态 在售；进行中活动：${activity}`;
}

/**
 * 调价前置判定 —— 对应后端的 PreToolUse 早闸。
 *
 * 被早闸拦下的调价**不会**进入审批、也不会向平台写入，所以这里直接给拦截结果，
 * 不再弹权限卡。成本保护与活动锁价都在这层，与后端规则引擎的语义一致。
 */
function precheck(
  platform: string,
  sku: string,
  target: number,
): { blocked: boolean; text: string; ruleSummary: string } {
  if (sku === LOCKED_SKU) {
    return {
      blocked: true,
      text: `拦截：调价未执行。原因：[BUSINESS_ERROR]@${platform} 活动锁价，不可改价(平台码 41)`,
      ruleSummary: '活动锁价：该 SKU 正在参加平台活动，不可改价',
    };
  }
  if (target < COST_PRICE) {
    return {
      blocked: true,
      text: `拦截：调价未执行。原因：成本保护拦截：新价格 ${target.toFixed(1)} 低于成本价 ${COST_PRICE.toFixed(1)}，不允许调整`,
      ruleSummary: `成本保护拦截：目标价 ${target.toFixed(2)} 元 < 成本价 ${COST_PRICE.toFixed(2)} 元`,
    };
  }
  return {
    blocked: false,
    text: '',
    ruleSummary: `成本保护通过：目标价 ${target.toFixed(2)} 元 ≥ 成本价 ${COST_PRICE.toFixed(2)} 元`,
  };
}

/** 调价真正生效的判据是平台回查，不是写接口返回 —— 演示里也照这个口径产出结果文本 */
function priceChangeText(platform: string, product: string, sku: string, target: number): string {
  return `平台 ${platform} 商品 ${product}/${sku} 价格已更新为 ${target.toFixed(1)} 元（平台回查一致）`;
}

function buildSummary(
  toolName: string,
  input: Record<string, unknown>,
  result: string,
  blocked: boolean,
): string {
  const platform = String(input.platform ?? '');
  const product = String(input.product_id ?? '');
  const sku = String(input.sku_id ?? '');
  if (toolName === 'query_product_snapshot') {
    return `已读到 ${platform} ${product}/${sku} 的快照：${result.replace(/^平台 [^：]*：/, '')}。`;
  }
  if (toolName === 'update_price') {
    if (blocked) {
      return `这次改价没有执行——${result.replace(/^拦截：调价未执行。原因：/, '')}。没有向平台发出任何写入。`;
    }
    if (result.includes('未确认')) {
      return `改价请求已经发出，但回查没能确认结果：${result} 写入有可能已经生效，建议重新查一次快照。`;
    }
    return `调价完成：${platform} ${product}/${sku} 已改为 ${String(input.target_price)} 元，并经平台回查确认生效。`;
  }
  return result;
}

export class MockEventStream {
  private onEvent: OnEvent;
  private sessionId: string;
  private mode: PermissionModeType;
  private running = false;
  private aborted = false;
  private pending: PendingPermission | null = null;
  private timer: ReturnType<typeof setTimeout> | null = null;

  constructor(sessionId: string, onEvent: OnEvent, mode: PermissionModeType) {
    this.sessionId = sessionId;
    this.onEvent = onEvent;
    this.mode = mode;
  }

  /** 切换权限模式（模拟后端 mode_change 广播） */
  setMode(mode: PermissionModeType) {
    this.mode = mode;
  }

  private emit(event: AgentEvent) {
    this.onEvent(event);
  }

  private pushMessage(message: MockMessage) {
    mockStore.appendMessage(this.sessionId, message);
  }

  /** 发送一条用户消息，模拟完整 turn 的事件流 */
  async sendMessage(content: string) {
    if (this.running) return;
    this.running = true;
    this.aborted = false;
    const { toolName, input } = inferTool(content);
    const useId = `call_mock_${Date.now()}`;
    const platform = String(input.platform ?? '');
    const product = String(input.product_id ?? '');
    const sku = String(input.sku_id ?? '');
    const target = Number(input.target_price ?? 0);

    // 早闸：被拦截的调价不进入审批，也不产生任何平台副作用
    const check =
      toolName === 'update_price' ? precheck(platform, sku, target) : { blocked: false, text: '', ruleSummary: '' };
    const isWrite = WRITE_TOOLS.has(toolName) && !check.blocked;

    try {
      this.pushMessage({ role: 'user', content, timestamp: new Date().toISOString() });
      this.emit({ type: 'status', message: '正在分析意图…' });
      await sleep(250);
      this.emit({ type: 'status', message: `已解析技能，准备调用 ${toolName}` });
      await sleep(200);

      // 流式思考文本
      const thoughts = ['正在调用平台接口获取数据…', '校验参数与权限…'];
      for (const t of thoughts) {
        if (this.aborted) return;
        for (const ch of t) {
          if (this.aborted) return;
          this.emit({ type: 'text_delta', text: ch });
          await sleep(12);
        }
        await sleep(60);
      }

      if (this.aborted) return;
      this.emit({ type: 'tool_start', tool_name: toolName, tool_use_id: useId, input });
      await sleep(300);

      let result = '';
      let isError = false;

      if (check.blocked) {
        // 早闸拦截：直接给结果，不弹审批（后端也是这个顺序）
        result = check.text;
        isError = true;
        this.emit({ type: 'tool_result', tool_use_id: useId, tool_name: toolName, result, is_error: true });
        this.pushMessage({ role: 'tool', content: result, timestamp: new Date().toISOString(), tool_call_id: useId });
      } else if (this.mode === 'ASK' && isWrite) {
        // 权限挂起：审批上下文与后端 permission_request 字段一一对应，
        // operation_id 与协调器实际使用的操作 ID 是同一个（引擎在权限检查前注入）。
        const requestId = `req_${useId}`;
        const approved = await new Promise<boolean>((resolve) => {
          this.pending = { requestId, toolName, resolve };
          this.emit({
            type: 'permission_request',
            request_id: requestId,
            tool_name: toolName,
            tool_input: input,
            reason: `写操作 ${toolName} 需要用户确认后执行`,
            operation_id: `op-${this.sessionId}-1-${useId}`,
            platform,
            product_ref: { platform, product_id: product, sku_id: sku },
            target_price: target,
            rule_summary: check.ruleSummary,
          });
        });
        this.pending = null;
        if (this.aborted) return;
        if (!approved) {
          result = `[已拒绝] 用户拒绝了 ${toolName} 的执行请求`;
          isError = true;
          this.emit({ type: 'tool_result', tool_use_id: useId, tool_name: toolName, result, is_error: true });
          this.pushMessage({ role: 'tool', content: result, timestamp: new Date().toISOString(), tool_call_id: useId });
        } else {
          result = priceChangeText(platform, product, sku, target);
          this.emit({ type: 'tool_result', tool_use_id: useId, tool_name: toolName, result, is_error: false });
          this.pushMessage({ role: 'tool', content: result, timestamp: new Date().toISOString(), tool_call_id: useId });
        }
      } else {
        // READONLY 下写操作自动拦截；只读工具放行
        if (this.mode === 'READONLY' && isWrite) {
          result = `[拦截] 当前为只读模式，写操作 ${toolName} 已被拦截`;
          isError = true;
        } else if (toolName === 'update_price') {
          result = priceChangeText(platform, product, sku, target);
        } else {
          result = snapshotText(platform, product, sku);
        }
        this.emit({ type: 'tool_result', tool_use_id: useId, tool_name: toolName, result, is_error: isError });
        this.pushMessage({ role: 'tool', content: result, timestamp: new Date().toISOString(), tool_call_id: useId });
      }

      if (this.aborted) return;
      await sleep(200);
      const summary =
        isError && !check.blocked && this.mode === 'READONLY'
          ? '当前是只读模式，写操作没有执行。切到 ASK 或 EXECUTE 模式后我再动手。'
          : buildSummary(toolName, input, result, check.blocked || isError);
      for (const ch of summary) {
        if (this.aborted) return;
        this.emit({ type: 'text_delta', text: ch });
        await sleep(14);
      }
      this.pushMessage({ role: 'assistant', content: summary, timestamp: new Date().toISOString() });

      this.emit({ type: 'status', message: '完成' });
      this.emit({ type: 'complete', stop_reason: 'end_turn' });
    } finally {
      this.running = false;
    }
  }

  abort() {
    if (!this.running) return;
    this.aborted = true;
    this.pending?.resolve(false);
    this.pending = null;
    this.running = false;
    // 与真实后端 1:1：中断以 AbortEvent 收尾（前端中断提示统一由事件驱动）
    this.emit({ type: 'abort', reason: 'user_cancel' });
  }

  respondPermission(requestId: string, approved: boolean) {
    if (this.pending && this.pending.requestId === requestId) {
      this.pending.resolve(approved);
    }
  }

  sendModeChange(mode: PermissionModeType) {
    this.setMode(mode);
    this.emit({ type: 'mode_change', mode });
  }

  close() {
    this.aborted = true;
    if (this.timer) clearTimeout(this.timer);
  }
}
