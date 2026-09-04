/**
 * mock 事件流生成器 — 模拟后端 WebSocket 推送的 AgentEvent 序列。
 *
 * sendMessage 按序派发：status → text_delta（流式）→ tool_start →
 * （ASK 模式写操作 → permission_request 挂起）→ tool_result → text_delta 结论
 * → complete。支持 abort 中断、respondPermission 续推、sendModeChange 回发。
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

/** 工具关键词 → (工具名, 归一化输入) — 与后端 mock 剧本一致的演示链路 */
function inferTool(text: string) {
  if (/促销|活动/.test(text)) {
    return { toolName: 'create_promotion', input: { channel: 'douyin', sku: 'SKU-003', discount: 0.8 } };
  }
  if (/价格|调价|改价/.test(text)) {
    return { toolName: 'update_price', input: { channel: 'taobao', sku: 'SKU-001', new_price: 79 } };
  }
  if (/订单/.test(text)) {
    return { toolName: 'query_order_status', input: { channel: 'taobao', order_id: 'TB-10086' } };
  }
  if (/工单|客诉/.test(text)) {
    return { toolName: 'service_ticket', input: { channel: 'taobao', order_id: 'TB-10086', issue: '商品破损，申请退货退款' } };
  }
  if (/知识|规范|政策/.test(text)) {
    return { toolName: 'query_knowledge_base', input: { topic: '上架规范' } };
  }
  return { toolName: 'query_inventory', input: { channel: 'taobao', sku: 'SKU-001' } };
}

const WRITE_TOOLS = new Set(['update_price', 'create_promotion', 'product_shelf', 'service_ticket', 'restock_product', 'batch_update_prices', 'update_logistics', 'end_promotion']);

const TOOL_RESULTS: Record<string, string> = {
  query_inventory: '{"stock": 320, "name": "防晒霜", "channel": "taobao"}',
  update_price: '{"old_price": 89, "new_price": 79, "channel": "taobao"}',
  create_promotion: '{"promotion_id": "P-20261101", "channel": "douyin"}',
  query_order_status: '{"status": "已发货", "channel": "taobao"}',
  service_ticket: '{"ticket_id": "TK-1001", "channel": "taobao"}',
  query_knowledge_base: '{"matched": true, "key": "上架规范", "text": "详情页需真实展示商品信息，禁止虚假宣传。"}',
};

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
    const isWrite = WRITE_TOOLS.has(toolName);

    try {
      this.pushMessage({ role: 'user', content, timestamp: new Date().toISOString() });
      this.emit({ type: 'status', message: '正在分析意图…' });
      await sleep(250);
      this.emit({ type: 'status', message: `已解析技能，准备调用 ${toolName}` });
      await sleep(200);

      // 流式思考文本
      const thoughts = ['正在调用渠道接口获取数据…', '校验参数与权限…'];
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

      // 权限挂起：ASK + 写操作
      if (this.mode === 'ASK' && isWrite) {
        const requestId = `req_${useId}`;
        const approved = await new Promise<boolean>((resolve) => {
          this.pending = { requestId, toolName, resolve };
          this.emit({
            type: 'permission_request',
            request_id: requestId,
            tool_name: toolName,
            tool_input: input,
            reason: `写操作 ${toolName} 需要用户确认后执行`,
          });
        });
        this.pending = null;
        if (this.aborted) return;
        if (!approved) {
          const result = `[已拒绝] 用户拒绝了 ${toolName} 的执行请求`;
          this.emit({ type: 'tool_result', tool_use_id: useId, tool_name: toolName, result, is_error: true });
          this.pushMessage({
            role: 'tool', content: result, timestamp: new Date().toISOString(), tool_call_id: useId,
          });
        } else {
          const result = TOOL_RESULTS[toolName] ?? '{}';
          this.emit({ type: 'tool_result', tool_use_id: useId, tool_name: toolName, result, is_error: false });
          this.pushMessage({
            role: 'tool', content: result, timestamp: new Date().toISOString(), tool_call_id: useId,
          });
        }
      } else {
        // READONLY 下写操作自动拦截
        if (this.mode === 'READONLY' && isWrite) {
          const result = `[拦截] 当前为只读模式，写操作 ${toolName} 已被拦截`;
          this.emit({ type: 'tool_result', tool_use_id: useId, tool_name: toolName, result, is_error: true });
          this.pushMessage({ role: 'tool', content: result, timestamp: new Date().toISOString(), tool_call_id: useId });
        } else {
          const result = TOOL_RESULTS[toolName] ?? '{}';
          this.emit({ type: 'tool_result', tool_use_id: useId, tool_name: toolName, result, is_error: false });
          this.pushMessage({ role: 'tool', content: result, timestamp: new Date().toISOString(), tool_call_id: useId });
        }
      }

      if (this.aborted) return;
      await sleep(200);
      const summary =
        toolName === 'query_inventory'
          ? `已查得渠道 ${input.channel} 商品 ${input.sku} 库存 320 件，库存充足。`
          : toolName === 'update_price'
            ? '已成功将商品价格调整为 79 元。'
            : toolName === 'create_promotion'
              ? '已为抖音 SKU-003 创建 8 折促销活动。'
              : toolName === 'query_order_status'
                ? '该订单当前状态为「已发货」。'
                : toolName === 'service_ticket'
                  ? '已创建售后工单 TK-1001，优先级 high，跟进中。'
                  : '已查询到相关规范条目。';
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
    this.aborted = true;
    this.pending?.resolve(false);
    this.pending = null;
    this.running = false;
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