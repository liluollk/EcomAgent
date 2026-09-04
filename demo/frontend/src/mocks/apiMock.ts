/**
 * 展示版 mock 数据 + 路由表 — 模拟后端全部 REST 接口的返回与状态。
 *
 * 数据形状与后端真实接口 1:1（types/index.ts 中的类型），
 * 这样以后把 VITE_OBS_MOCK=0 切回真实后端时，组件零改动。
 */

import type {
  ChannelSource,
  ModelProvider,
  PermissionModeType,
  SessionMeta,
  Skill,
} from '../types';

// ---------------------------------------------------------------------------
// 内存态仓库（CRUD 前后一致）
// ---------------------------------------------------------------------------

export interface MockMessage {
  role: 'user' | 'assistant' | 'tool';
  content: string;
  timestamp: string;
  tool_calls?: Array<{ id: string; type: string; function: { name: string; arguments: string } }>;
  tool_call_id?: string;
}

export interface MockSession {
  session_id: string;
  workspace_id: string;
  status: string;
  user: { user_id: string; role: string };
  message_count: number;
  permission_mode: PermissionModeType;
  model_state: { provider: string; model: string };
  title?: string;
  createdAt?: number;
  messages: MockMessage[];
}

const now = () => new Date().toISOString();

function msg(role: MockMessage['role'], content: string, extra: Partial<MockMessage> = {}): MockMessage {
  return { role, content, timestamp: now(), ...extra };
}

function toolCall(id: string, name: string, argumentsStr: string) {
  return { id, type: 'function' as const, function: { name, arguments: argumentsStr } };
}

const seedSessions: MockSession[] = [
  {
    session_id: 'demo-001',
    workspace_id: 'default',
    status: 'ACTIVE',
    user: { user_id: 'user_demo', role: 'manager' },
    message_count: 4,
    permission_mode: 'ASK',
    model_state: { provider: 'openai', model: 'gpt-4o-mini' },
    title: '夏季防晒上新',
    createdAt: Date.now() - 3600_000 * 5,
    messages: [
      msg('user', '查一下淘宝 SKU-001 的库存'),
      msg('assistant', '', {
        tool_calls: [toolCall('c1', 'query_inventory', '{"channel": "taobao", "sku": "SKU-001"}')],
      }),
      msg('tool', '{"stock": 320, "name": "防晒霜", "channel": "taobao"}', { tool_call_id: 'c1' }),
      msg('assistant', '淘宝 SKU-001 防晒霜当前库存 320 件，库存充足。'),
    ],
  },
  {
    session_id: 'demo-002',
    workspace_id: 'default',
    status: 'ACTIVE',
    user: { user_id: 'user_demo', role: 'manager' },
    message_count: 4,
    permission_mode: 'ASK',
    model_state: { provider: 'openai', model: 'gpt-4o-mini' },
    title: '双11 促销筹备',
    createdAt: Date.now() - 3600_000 * 2,
    messages: [
      msg('user', '给抖音 SKU-003 建个促销活动，8 折'),
      msg('assistant', '', {
        tool_calls: [
          toolCall('c2', 'create_promotion', '{"channel": "douyin", "sku": "SKU-003", "discount": 0.8, "start_time": "2026-11-01", "end_time": "2026-11-11"}'),
        ],
      }),
      msg('tool', '{"promotion_id": "P-20261101", "channel": "douyin"}', { tool_call_id: 'c2' }),
      msg('assistant', '已在抖音为 SKU-003 创建 8 折促销活动，时间 11.1 ~ 11.11。'),
    ],
  },
  {
    session_id: 'demo-003',
    workspace_id: 'default',
    status: 'ACTIVE',
    user: { user_id: 'user_demo', role: 'customer_service' },
    message_count: 3,
    permission_mode: 'ASK',
    model_state: { provider: 'anthropic', model: 'claude-3-5-sonnet-latest' },
    title: '售后客诉跟进',
    createdAt: Date.now() - 3600_000,
    messages: [
      msg('user', '淘宝订单 TB-10086 有个客诉，建个工单'),
      msg('assistant', '', {
        tool_calls: [toolCall('c3', 'service_ticket', '{"channel": "taobao", "order_id": "TB-10086", "issue": "商品破损，申请退货退款", "priority": "high"}')],
      }),
      msg('assistant', '已创建售后工单，优先级 high，跟进中。'),
    ],
  },
];

const providers: ModelProvider[] = [
  { name: 'openai', label: 'OpenAI', provider: 'openai', default_model: 'gpt-4o-mini', api_key: 'sk-****', api_base: '', model_list: ['gpt-4o', 'gpt-4o-mini', 'gpt-4-turbo'], enabled: true },
  { name: 'anthropic', label: 'Anthropic', provider: 'anthropic', default_model: 'claude-3-5-sonnet-latest', api_key: 'sk-****', api_base: '', model_list: ['claude-3-5-sonnet-latest', 'claude-3-5-haiku-latest'], enabled: true },
  { name: 'mock', label: 'Mock（离线演示）', provider: 'mock', default_model: 'gpt-4o-mini', api_key: '', api_base: '', model_list: [], enabled: true },
];
let activeProvider = 'openai';

const sources: ChannelSource[] = [
  { name: 'taobao', label: '淘宝', base_url: '', platform: 'mock', auth_type: 'mock', enabled: true },
  { name: 'jd', label: '京东', base_url: '', platform: 'mock', auth_type: 'mock', enabled: true },
  { name: 'douyin', label: '抖音', base_url: '', platform: 'mock', auth_type: 'mock', enabled: true },
  { name: 'tbshop', label: '品牌直营店', base_url: 'https://eco.taobao.com/router/rest', platform: 'taobao', auth_type: 'api_key', api_key: 'sk-l****', options: { app_key: '123456', app_secret: 'sec-****' }, enabled: true },
];

const skills: Skill[] = [
  { name: 'inventory_query', description: '查询商品库存与补货建议', keywords: ['库存', '存货', '补货', '盘点'], prerequisites: [], tools: ['query_inventory'], prompt: '执行库存查询 SOP：1) 确认渠道与 SKU；2) 调用 query_inventory；3) 汇报库存数量。', enabled: true, builtin: true },
  { name: 'price_management', description: '商品价格调整管理', keywords: ['价格', '调价', '改价', '定价'], prerequisites: [], tools: ['update_price'], prompt: '调价 SOP：1) 确认新价格不低于成本价；2) 调用 update_price；3) 汇报结果。', enabled: true, builtin: true },
  { name: 'promotion_management', description: '促销活动创建与检查', keywords: ['促销', '优惠', '折扣', '满减'], prerequisites: [], tools: ['create_promotion', 'query_promotions'], prompt: '促销 SOP：1) 确认参数；2) 调用 create_promotion；3) 汇报。', enabled: true, builtin: true },
];

// ---------------------------------------------------------------------------
// 仓库操作
// ---------------------------------------------------------------------------

export const mockStore = {
  sessions: seedSessions,
  providers,
  sources,
  skills,

  get activeProvider() {
    return activeProvider;
  },
  setActiveProvider(name: string) {
    activeProvider = name;
  },

  findSession(sessionId: string) {
    return this.sessions.find((s) => s.session_id === sessionId);
  },
  appendMessage(sessionId: string, message: MockMessage) {
    const s = this.findSession(sessionId);
    if (s) {
      s.messages.push(message);
      s.message_count = s.messages.length;
    }
  },
};

// ---------------------------------------------------------------------------
// 路由表（模拟后端全部 REST 接口）
// ---------------------------------------------------------------------------

const delay = (ms = 260) => new Promise((r) => setTimeout(r, ms));

function ok(data: unknown) {
  return { ok: true, json: async () => data };
}
function err(message: string, status = 400) {
  return { ok: false, json: async () => ({ error: message }), status };
}

export async function mockRequest(
  method: string,
  url: string,
  body?: unknown,
): Promise<{ ok: boolean; status?: number; json(): Promise<unknown> }> {
  await delay();
  const path = url.split('?')[0];
  const payload = (body ?? {}) as Record<string, unknown>;

  // ---------- 会话 ----------
  if (method === 'GET' && path === '/sessions') {
    return ok(mockStore.sessions.map(({ messages, ...meta }) => ({
      ...meta,
      message_count: messages.length,
    })));
  }
  if (method === 'POST' && path === '/sessions') {
    const sid = `demo-${String(Date.now()).slice(-6)}`;
    const session: MockSession = {
      session_id: sid,
      workspace_id: (payload.workspace_id as string) || 'default',
      status: 'ACTIVE',
      user: { user_id: 'user_demo', role: (payload.role as string) || 'operator' },
      message_count: 0,
      permission_mode: (payload.permission_mode as PermissionModeType) || 'ASK',
      model_state: { provider: activeProvider, model: providers.find((p) => p.name === activeProvider)?.default_model || '' },
      title: '',
      createdAt: Date.now(),
      messages: [],
    };
    mockStore.sessions.unshift(session);
    return ok({
      session_id: session.session_id,
      workspace_id: session.workspace_id,
      user: session.user,
      model_state: session.model_state,
      permission_mode: session.permission_mode,
      status: session.status,
    });
  }
  const sessionMatch = path.match(/^\/sessions\/([^/]+)$/);
  if (method === 'DELETE' && sessionMatch) {
    const sid = sessionMatch[1];
    mockStore.sessions = mockStore.sessions.filter((s) => s.session_id !== sid);
    return ok({ deleted: sid });
  }
  const messagesMatch = path.match(/^\/sessions\/([^/]+)\/messages$/);
  if (method === 'GET' && messagesMatch) {
    const s = mockStore.findSession(messagesMatch[1]);
    return ok(s ? s.messages : []);
  }
  const modeGet = path.match(/^\/sessions\/([^/]+)\/mode$/);
  if (method === 'GET' && modeGet) {
    const s = mockStore.findSession(modeGet[1]);
    return ok(s ? { session_id: s.session_id, mode: s.permission_mode } : { mode: 'ASK' });
  }
  if (method === 'PUT' && modeGet) {
    const s = mockStore.findSession(modeGet[1]);
    const mode = payload.mode as PermissionModeType;
    if (s) s.permission_mode = mode;
    return ok({ session_id: modeGet[1], mode });
  }

  // ---------- 供应商 ----------
  if (method === 'GET' && path === '/providers') {
    return ok({ providers: mockStore.providers, active: mockStore.activeProvider });
  }
  if (method === 'POST' && path === '/providers') {
    const name = (payload.name as string) || '';
    if (mockStore.providers.some((p) => p.name === name)) return err(`供应商已存在: ${name}`);
    const provider: ModelProvider = {
      name,
      label: (payload.label as string) || name,
      provider: (payload.provider as ModelProvider['provider']) || 'openai',
      default_model: (payload.default_model as string) || '',
      api_key: payload.api_key ? `${String(payload.api_key).slice(0, 4)}****` : '',
      api_base: (payload.api_base as string) || '',
      model_list: [],
      enabled: true,
      created_at: Date.now(),
    };
    mockStore.providers.push(provider);
    return ok(provider);
  }
  const providerMatch = path.match(/^\/providers\/([^/]+)(\/\w+)?$/);
  if (providerMatch) {
    const name = providerMatch[1];
    const action = providerMatch[2] ?? '';
    const p = mockStore.providers.find((x) => x.name === name);
    if (!p) return err(`供应商不存在: ${name}`, 404);
    if (method === 'PATCH' && !action) {
      if (payload.default_model) p.default_model = payload.default_model as string;
      if (payload.api_base) p.api_base = payload.api_base as string;
      if (payload.api_key) p.api_key = `${String(payload.api_key).slice(0, 4)}****`;
      if (typeof payload.enabled === 'boolean') p.enabled = payload.enabled;
      return ok(p);
    }
    if (method === 'POST' && action === '/activate') {
      mockStore.setActiveProvider(name);
      return ok({ ok: true, active: name });
    }
    if (method === 'POST' && action === '/test') {
      return ok({ ok: true, message: `${p.label} 配置有效（mock）` });
    }
    if (method === 'DELETE' && !action) {
      mockStore.providers = mockStore.providers.filter((x) => x.name !== name);
      return ok({ deleted: name });
    }
  }

  // ---------- 渠道 ----------
  if (method === 'GET' && path === '/sources') {
    return ok(mockStore.sources);
  }
  if (method === 'POST' && path === '/sources') {
    const name = (payload.name as string) || '';
    if (mockStore.sources.some((s) => s.name === name)) return err(`渠道已存在: ${name}`);
    const source: ChannelSource = {
      name,
      label: (payload.label as string) || name,
      base_url: (payload.base_url as string) || '',
      platform: (payload.platform as ChannelSource['platform']) || 'mock',
      auth_type: (payload.auth_type as string) || 'mock',
      api_key: payload.api_key ? `${String(payload.api_key).slice(0, 4)}****` : undefined,
      options: payload.options as Record<string, unknown> | undefined,
      enabled: true,
      created_at: Date.now(),
    };
    mockStore.sources.push(source);
    return ok(source);
  }
  const sourceMatch = path.match(/^\/sources\/([^/]+)(\/\w+)?$/);
  if (sourceMatch) {
    const name = sourceMatch[1];
    const action = sourceMatch[2] ?? '';
    const s = mockStore.sources.find((x) => x.name === name);
    if (!s) return err(`渠道不存在: ${name}`, 404);
    if (method === 'PATCH' && !action) {
      if (payload.base_url) s.base_url = payload.base_url as string;
      if (payload.platform) s.platform = payload.platform as ChannelSource['platform'];
      if (typeof payload.enabled === 'boolean') s.enabled = payload.enabled;
      if (payload.api_key) s.api_key = `${String(payload.api_key).slice(0, 4)}****`;
      return ok(s);
    }
    if (method === 'DELETE' && !action) {
      mockStore.sources = mockStore.sources.filter((x) => x.name !== name);
      return ok({ deleted: name });
    }
    if (method === 'POST' && action === '/test') {
      return ok({ ok: true, message: `连通成功（mock：${s.platform}）`, data: { orders: 12 } });
    }
  }

  // ---------- 技能 ----------
  if (method === 'GET' && path === '/skills') {
    return ok(mockStore.skills);
  }
  if (method === 'POST' && path === '/skills') {
    const name = (payload.name as string) || '';
    if (mockStore.skills.some((s) => s.name === name)) return err(`技能已存在: ${name}`);
    const skill: Skill = {
      name,
      description: (payload.description as string) || name,
      keywords: Array.isArray(payload.keywords) ? payload.keywords as string[] : [],
      prerequisites: [],
      tools: Array.isArray(payload.tools) ? payload.tools as string[] : [],
      prompt: (payload.prompt as string) || '',
      enabled: true,
      builtin: false,
      created_at: Date.now(),
    };
    mockStore.skills.push(skill);
    return ok(skill);
  }
  const skillMatch = path.match(/^\/skills\/([^/]+)$/);
  if (skillMatch) {
    const name = skillMatch[1];
    const s = mockStore.skills.find((x) => x.name === name);
    if (!s) return err(`技能不存在: ${name}`, 404);
    if (method === 'PATCH') {
      if (payload.keywords) s.keywords = payload.keywords as string[];
      if (payload.tools) s.tools = payload.tools as string[];
      if (payload.prompt) s.prompt = payload.prompt as string;
      if (typeof payload.enabled === 'boolean') s.enabled = payload.enabled;
      return ok(s);
    }
    if (method === 'DELETE') {
      if (s.builtin) return err(`内置技能 ${name} 不可删除，可停用`, 400);
      mockStore.skills = mockStore.skills.filter((x) => x.name !== name);
      return ok({ deleted: name });
    }
  }

  // 未匹配的路由：模拟 404
  return err(`模拟后端未实现该接口: ${method} ${path}`, 404);
}