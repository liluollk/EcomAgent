import { useEffect, useState } from 'react';
import type { ChannelPlatform, ChannelSource, ModelProvider, PermissionModeType, ProvidersResponse } from '../types';
import { SkillManager } from './SkillManager';
import { toast } from '../lib/toast';

const MODE_INFO: { id: PermissionModeType; name: string; desc: string; dot: string }[] = [
  { id: 'READONLY', name: '只读模式', desc: '只读操作自动执行，写操作自动拦截', dot: 'bg-ok' },
  { id: 'ASK', name: '询问模式', desc: '写操作执行前需要用户确认（默认）', dot: 'bg-warn' },
  { id: 'EXECUTE', name: '执行模式', desc: '所有操作自动执行，适合管理员（高风险）', dot: 'bg-danger' },
];

type Tab = 'model' | 'channel' | 'permission' | 'notify' | 'skill';

const TAB_LABELS: { id: Tab; label: string }[] = [
  { id: 'model', label: '模型配置' },
  { id: 'channel', label: '渠道连接' },
  { id: 'skill', label: '技能管理' },
  { id: 'permission', label: '权限模式' },
  { id: 'notify', label: '通知设置' },
];

interface SettingsModalProps {
  open: boolean;
  onClose: () => void;
  mode: PermissionModeType;
  onModeChange: (mode: PermissionModeType) => void;
  activeProvider: string;
  onProvidersChange: () => void;
}

export function SettingsModal({ open, onClose, mode, onModeChange, activeProvider, onProvidersChange }: SettingsModalProps) {
  const [tab, setTab] = useState<Tab>('model');

  useEffect(() => {
    if (!open) return;
    const handler = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', handler);
    return () => document.removeEventListener('keydown', handler);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/25 backdrop-blur-[2px]" onClick={onClose}>
      <div
        className="flex max-h-[82vh] w-[560px] flex-col overflow-hidden rounded-2xl border border-line bg-elevated shadow-pop animate-fade-up"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex h-[52px] flex-shrink-0 items-center justify-between border-b border-line px-5">
          <h2 className="text-[14.5px] font-semibold text-ink">设置</h2>
          <button
            onClick={onClose}
            className="flex h-7 w-7 items-center justify-center rounded-md text-ink-3 transition-colors hover:bg-black/5 hover:text-ink"
          >
            <svg className="h-4 w-4" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
              <line x1="18" y1="6" x2="6" y2="18" />
              <line x1="6" y1="6" x2="18" y2="18" />
            </svg>
          </button>
        </div>

        <div className="flex flex-shrink-0 gap-1 border-b border-line px-4 pt-2.5">
          {TAB_LABELS.map((t) => (
            <button
              key={t.id}
              onClick={() => setTab(t.id)}
              className={`rounded-t-md border-b-2 px-3 pb-2 pt-1 text-[13px] transition-colors ${
                tab === t.id ? 'border-accent font-medium text-accent' : 'border-transparent text-ink-2 hover:text-ink'
              }`}
            >
              {t.label}
            </button>
          ))}
        </div>

        <div className="flex-1 overflow-y-auto px-5 py-4">
          {tab === 'model' && <ModelSettings activeProvider={activeProvider} onProvidersChange={onProvidersChange} />}
          {tab === 'channel' && <ChannelSettings />}
          {tab === 'skill' && <SkillSettings />}
          {tab === 'permission' && <PermissionSettings mode={mode} onModeChange={onModeChange} />}
          {tab === 'notify' && <NotifySettings />}
        </div>
      </div>
    </div>
  );
}

function ModelSettings({ activeProvider, onProvidersChange }: { activeProvider: string; onProvidersChange: () => void }) {
  const [providers, setProviders] = useState<ModelProvider[]>([]);
  const [active, setActive] = useState(activeProvider);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [testing, setTesting] = useState<string | null>(null);

  // 正在编辑的供应商（空 = 未在编辑）
  const [editing, setEditing] = useState<ModelProvider | null>(null);
  const [editApiKey, setEditApiKey] = useState('');
  const [editApiBase, setEditApiBase] = useState('');
  const [editModel, setEditModel] = useState('');
  const [editThinking, setEditThinking] = useState('');

  // 新增供应商表单
  const [addName, setAddName] = useState('');
  const [addLabel, setAddLabel] = useState('');
  const [addProvider, setAddProvider] = useState<'openai' | 'anthropic' | 'mock'>('openai');
  const [addApiKey, setAddApiKey] = useState('');
  const [addModel, setAddModel] = useState('');
  const [addThinking, setAddThinking] = useState('');

  async function refresh() {
    try {
      const resp = await fetch('/providers');
      const data = (await resp.json()) as ProvidersResponse;
      setProviders(Array.isArray(data.providers) ? data.providers : []);
      setActive(data.active);
      setError(null);
    } catch {
      setError('加载模型供应商失败');
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (editing) {
      setEditApiKey(editing.api_key ?? '');
      setEditApiBase(editing.api_base ?? '');
      setEditModel(editing.default_model ?? '');
      setEditThinking(editing.thinking_level ?? '');
    }
  }, [editing]);

  async function activate(p: ModelProvider) {
    const resp = await fetch(`/providers/${p.name}/activate`, { method: 'POST' });
    const data = await resp.json();
    if (resp.ok) {
      setActive(p.name);
      setNotice(`已切换为 ${p.label || p.name}`);
      onProvidersChange();
    } else {
      setNotice(data.message || data.error || '切换失败');
    }
  }

  /** 测连通：校验供应商配置能否构建后端实例（离线，不实际调用 LLM 网络） */
  async function testProvider(p: ModelProvider) {
    setTesting(p.name);
    try {
      const resp = await fetch(`/providers/${p.name}/test`, { method: 'POST' });
      const data = await resp.json();
      setNotice(`${p.label || p.name}: ${data.message ?? (data.ok ? '配置可用' : '测试失败')}`);
    } catch {
      setNotice(`${p.label || p.name}: 测试请求失败`);
    } finally {
      setTesting(null);
    }
  }

  async function saveEdit() {
    if (!editing) return;
    const body: Record<string, unknown> = {};
    if (editModel.trim()) body.default_model = editModel.trim();
    if (editApiBase.trim()) body.api_base = editApiBase.trim();
    if (editApiKey.trim()) body.api_key = editApiKey.trim();
    if (editThinking) body.thinking_level = editThinking;
    const resp = await fetch(`/providers/${editing.name}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await resp.json();
    if (resp.ok) {
      setNotice(`已保存 ${data.label || data.name} 配置`);
      setEditing(null);
      refresh();
      onProvidersChange();
    } else {
      setNotice(data.error || '保存失败');
    }
  }

  async function addProviderFn() {
    if (!addName.trim()) {
      setNotice('请填写供应商标识（name）');
      return;
    }
    const body: Record<string, unknown> = { name: addName.trim(), provider: addProvider };
    if (addLabel.trim()) body.label = addLabel.trim();
    if (addModel.trim()) body.default_model = addModel.trim();
    if (addApiKey.trim()) body.api_key = addApiKey.trim();
    if (addThinking) body.thinking_level = addThinking;
    const resp = await fetch('/providers', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await resp.json();
    if (resp.ok) {
      setNotice(`已新增供应商 ${data.name}`);
      setAddName('');
      setAddLabel('');
      setAddApiKey('');
      setAddModel('');
      setAddThinking('');
      refresh();
      onProvidersChange();
    } else {
      setNotice(data.error || '新增供应商失败');
    }
  }

  const providerDot: Record<string, string> = { openai: 'bg-accent', anthropic: 'bg-amber-500', mock: 'bg-ink-3/50' };

  return (
    <div className="space-y-4">
      <div className="text-[12.5px] font-medium text-ink">模型供应商（切换即时生效）</div>
      {loading ? (
        <p className="text-[12px] text-ink-3">加载中…</p>
      ) : error ? (
        <p className="text-[12px] text-danger">{error}</p>
      ) : (
        <div className="space-y-2">
          {providers.map((p) => (
            <div key={p.name} className="rounded-xl border border-line px-4 py-3">
              <div className="flex items-center justify-between">
                <div className="flex min-w-0 items-center gap-2.5">
                  <span className={`h-2 w-2 flex-shrink-0 rounded-full ${providerDot[p.provider] ?? 'bg-ink-3/50'}`} />
                  <span className="truncate text-[13.5px] text-ink">{p.label}</span>
                  <span className="font-mono text-[11px] text-ink-3">{p.name}</span>
                  {p.provider === 'mock' && <span className="text-[11px] text-ink-3">离线演示</span>}
                </div>
                <div className="flex flex-shrink-0 items-center gap-1">
                  {active === p.name && (
                    <span className="rounded-full bg-accent-soft px-2 py-0.5 text-[11px] font-medium text-accent">当前</span>
                  )}
                  {active !== p.name && (
                    <button
                      onClick={() => activate(p)}
                      className="rounded-md px-2 py-1 text-[11.5px] text-accent transition-colors hover:bg-accent-soft"
                    >
                      设为当前
                    </button>
                  )}
                  <button
                    onClick={() => testProvider(p)}
                    className="rounded-md px-2 py-1 text-[11.5px] text-ink-2 transition-colors hover:bg-black/5"
                  >
                    {testing === p.name ? '测试中…' : '测连通'}
                  </button>
                  <button
                    onClick={() => setEditing(editing?.name === p.name ? null : p)}
                    className="rounded-md px-2 py-1 text-[11.5px] text-ink-2 transition-colors hover:bg-black/5"
                  >
                    {editing?.name === p.name ? '收起' : '编辑'}
                  </button>
                </div>
              </div>
              <div className="mt-1 flex items-center gap-2 pl-[18px] font-mono text-[11px] text-ink-3">
                <span>{p.provider}</span>
                <span>·</span>
                <span>{p.default_model || '-'}</span>
              </div>

              {editing?.name === p.name && (
                <div className="mt-3 grid grid-cols-2 gap-2 border-t border-line pt-3">
                  <InputField value={editModel} onChange={setEditModel} placeholder="默认模型（如 gpt-4o）" />
                  <InputField value={editApiBase} onChange={setEditApiBase} placeholder="API 地址（可选）" />
                  <InputField value={editApiKey} onChange={setEditApiKey} placeholder="API Key（掩码存储）" wide />
                  <select
                    value={editThinking}
                    onChange={(e) => setEditThinking(e.target.value)}
                    className="col-span-2 h-8 rounded-lg border border-line bg-white px-2 text-[12.5px] text-ink outline-none"
                  >
                    <option value="">思考强度：不启用</option>
                    <option value="low">思考强度：低（Anthropic→thinking / OpenAI→reasoning_effort）</option>
                    <option value="medium">思考强度：中</option>
                    <option value="high">思考强度：高</option>
                  </select>
                  <div className="col-span-2 flex justify-end gap-2">
                    <button
                      onClick={saveEdit}
                      className="h-8 rounded-lg bg-accent px-3 text-[12.5px] font-medium text-white transition-colors hover:bg-accent/90"
                    >
                      保存
                    </button>
                  </div>
                </div>
              )}
            </div>
          ))}
        </div>
      )}
      {notice && <p className="text-[12px] text-ink-2">{notice}</p>}

      <div className="rounded-xl border border-line bg-black/[0.02] p-4">
        <div className="mb-2 text-[12.5px] font-medium text-ink">新增供应商</div>
        <div className="grid grid-cols-2 gap-2">
          <InputField value={addName} onChange={setAddName} placeholder="标识（name，如 zhipu）" />
          <InputField value={addLabel} onChange={setAddLabel} placeholder="展示名（如 智谱）" />
          <select
            value={addProvider}
            onChange={(e) => setAddProvider(e.target.value as 'openai' | 'anthropic' | 'mock')}
            className="h-8 rounded-lg border border-line bg-white px-2 text-[12.5px] text-ink outline-none"
          >
            <option value="openai">OpenAI 兼容</option>
            <option value="anthropic">Anthropic</option>
            <option value="mock">Mock 离线</option>
          </select>
          <InputField value={addModel} onChange={setAddModel} placeholder="默认模型" />
          <InputField value={addApiKey} onChange={setAddApiKey} placeholder="API Key（可选，掩码存储）" wide />
          <select
            value={addThinking}
            onChange={(e) => setAddThinking(e.target.value)}
            className="col-span-2 h-8 rounded-lg border border-line bg-white px-2 text-[12.5px] text-ink outline-none"
          >
            <option value="">思考强度：不启用</option>
            <option value="low">思考强度：低</option>
            <option value="medium">思考强度：中</option>
            <option value="high">思考强度：高</option>
          </select>
          <button
            onClick={addProviderFn}
            className="h-8 rounded-lg bg-accent px-3 text-[12.5px] font-medium text-white transition-colors hover:bg-accent/90"
          >
            添加
          </button>
        </div>
      </div>
      <p className="text-[11.5px] leading-relaxed text-ink-3">
        填 API Key / API 地址后「设为当前」，新对话即用该供应商模型执行。密钥掩码持久化，重启后保留。
      </p>
    </div>
  );
}


function ChannelSettings() {
  const [sources, setSources] = useState<ChannelSource[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [testing, setTesting] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const [name, setName] = useState('');
  const [label, setLabel] = useState('');
  const [baseUrl, setBaseUrl] = useState('');
  const [platform, setPlatform] = useState<ChannelPlatform>('mock');
  const [authType, setAuthType] = useState<'mock' | 'api_key'>('mock');
  const [apiKey, setApiKey] = useState('');
  const [optAppKey, setOptAppKey] = useState('');
  const [optAppSecret, setOptAppSecret] = useState('');
  const [optAccessToken, setOptAccessToken] = useState('');

  // 正在编辑的渠道（展开编辑表单）
  const [editing, setEditing] = useState<ChannelSource | null>(null);
  const [editBaseUrl, setEditBaseUrl] = useState('');
  const [editPlatform, setEditPlatform] = useState<ChannelPlatform>('mock');
  const [editApiKey, setEditApiKey] = useState('');
  const [editOptAppKey, setEditOptAppKey] = useState('');
  const [editOptAppSecret, setEditOptAppSecret] = useState('');
  const [editOptAccessToken, setEditOptAccessToken] = useState('');

  async function refresh() {
    try {
      const resp = await fetch('/sources');
      const data = await resp.json();
      setSources(Array.isArray(data) ? data : []);
      setError(null);
    } catch {
      setError('加载渠道列表失败');
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    refresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /** 从渠道配置提取 options（掩码值可能带 ****，保存时未改动就丢掉） */
  function optionsOf(s: ChannelSource | null): { app_key?: string; app_secret?: string; access_token?: string } {
    const o = (s?.options ?? {}) as Record<string, unknown>;
    const pick = (k: string) => (typeof o[k] === 'string' ? (o[k] as string) : '');
    return { app_key: pick('app_key'), app_secret: pick('app_secret'), access_token: pick('access_token') };
  }

  function openEdit(s: ChannelSource) {
    const o = optionsOf(s);
    setEditing(s);
    setEditBaseUrl(s.base_url ?? '');
    setEditPlatform(s.platform ?? 'mock');
    setEditApiKey(s.api_key ?? '');
    setEditOptAppKey(o.app_key ?? '');
    setEditOptAppSecret(o.app_secret ?? '');
    setEditOptAccessToken(o.access_token ?? '');
  }

  async function addSource() {
    if (!name.trim()) {
      setNotice('请填写渠道标识（name）');
      return;
    }
    const body: Record<string, unknown> = { name: name.trim() };
    if (label.trim()) body.label = label.trim();
    if (baseUrl.trim()) body.base_url = baseUrl.trim();
    body.platform = platform;
    body.auth_type = authType;
    if (authType === 'api_key' && apiKey.trim()) body.api_key = apiKey.trim();
    if (platform !== 'mock') {
      const opts: Record<string, unknown> = {};
      if (optAppKey.trim()) opts.app_key = optAppKey.trim();
      if (optAppSecret.trim()) opts.app_secret = optAppSecret.trim();
      if (optAccessToken.trim()) opts.access_token = optAccessToken.trim();
      if (Object.keys(opts).length > 0) body.options = opts;
    }
    const resp = await fetch('/sources', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await resp.json();
    if (resp.ok) {
      setNotice(`已新增渠道 ${data.name}`);
      setName('');
      setLabel('');
      setBaseUrl('');
      setApiKey('');
      setOptAppKey('');
      setOptAppSecret('');
      setOptAccessToken('');
      refresh();
    } else {
      setNotice(data.error || '新增渠道失败');
    }
  }

  async function saveEdit() {
    if (!editing) return;
    const body: Record<string, unknown> = {};
    if (editBaseUrl.trim()) body.base_url = editBaseUrl.trim();
    body.platform = editPlatform;
    if (editApiKey.trim()) body.api_key = editApiKey.trim();
    const opts: Record<string, unknown> = {};
    if (editOptAppKey.trim() && !editOptAppKey.endsWith('****')) opts.app_key = editOptAppKey.trim();
    if (editOptAppSecret.trim() && !editOptAppSecret.endsWith('****')) opts.app_secret = editOptAppSecret.trim();
    if (editOptAccessToken.trim() && !editOptAccessToken.endsWith('****')) opts.access_token = editOptAccessToken.trim();
    if (editPlatform !== 'mock' && Object.keys(opts).length > 0) body.options = opts;
    const resp = await fetch(`/sources/${editing.name}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    const data = await resp.json();
    if (resp.ok) {
      setNotice(`已保存渠道 ${editing.name}`);
      setEditing(null);
      refresh();
    } else {
      setNotice(data.error || '保存失败');
    }
  }

  async function toggleEnabled(s: ChannelSource) {
    const resp = await fetch(`/sources/${s.name}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: !s.enabled }),
    });
    if (resp.ok) refresh();
  }

  async function removeSource(s: ChannelSource) {
    const resp = await fetch(`/sources/${s.name}`, { method: 'DELETE' });
    const data = await resp.json();
    setNotice(data.error || `已删除渠道 ${s.name}`);
    refresh();
  }

  async function testSource(s: ChannelSource) {
    setTesting(s.name);
    try {
      const resp = await fetch(`/sources/${s.name}/test`, { method: 'POST' });
      const data = await resp.json();
      setNotice(`${s.name}: ${data.message}`);
    } catch {
      setNotice(`${s.name}: 测试请求失败`);
    } finally {
      setTesting(null);
    }
  }

  return (
    <div className="space-y-4">
      <div className="text-[12.5px] font-medium text-ink">已接入渠道</div>
      {loading ? (
        <p className="text-[12px] text-ink-3">加载中…</p>
      ) : error ? (
        <p className="text-[12px] text-danger">{error}</p>
      ) : (
        <div className="space-y-2">
          {sources.map((s) => (
            <div key={s.name} className="rounded-xl border border-line px-4 py-3">
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2.5">
                  <span className={`h-2 w-2 rounded-full ${s.enabled ? 'bg-ok' : 'bg-ink-3/50'}`} />
                  <span className="text-[13.5px] text-ink">{s.label}</span>
                  <span className="font-mono text-[11px] text-ink-3">{s.name}</span>
                  <span
                    className={`rounded-full px-2 py-0.5 text-[11px] font-medium ${
                      s.enabled ? 'bg-[#ECFDF3] text-ok' : 'bg-black/5 text-ink-3'
                    }`}
                  >
                    {s.enabled ? '已接入' : '已停用'}
                  </span>
                  <span
                    className={`rounded-full px-2 py-0.5 font-mono text-[11px] ${
                      (s.platform ?? 'mock') === 'mock' ? 'bg-black/5 text-ink-3' : 'bg-[#FFF4E5] text-[#B45309]'
                    }`}
                  >
                    {(s.platform ?? 'mock') === 'mock' ? 'mock' : `真实:${s.platform}`}
                  </span>
                  {s.auth_type === 'api_key' && <span className="text-[11px] text-ink-3">{s.auth_type}</span>}
                </div>
                <div className="flex items-center gap-1">
                  <button
                    onClick={() => testSource(s)}
                    className="rounded-md px-2 py-1 text-[11.5px] text-ink-2 transition-colors hover:bg-black/5"
                  >
                    {testing === s.name ? '测试中…' : '测连通'}
                  </button>
                  <button
                    onClick={() => toggleEnabled(s)}
                    className="rounded-md px-2 py-1 text-[11.5px] text-ink-2 transition-colors hover:bg-black/5"
                  >
                    {s.enabled ? '停用' : '启用'}
                  </button>
                  <button
                    onClick={() => (editing?.name === s.name ? setEditing(null) : openEdit(s))}
                    className="rounded-md px-2 py-1 text-[11.5px] text-ink-2 transition-colors hover:bg-black/5"
                  >
                    {editing?.name === s.name ? '收起' : '编辑'}
                  </button>
                  <button
                    onClick={() => removeSource(s)}
                    className="rounded-md px-2 py-1 text-[11.5px] text-danger transition-colors hover:bg-red-50"
                  >
                    删除
                  </button>
                </div>
              </div>

              {editing?.name === s.name && (
                <div className="mt-3 grid grid-cols-2 gap-2 border-t border-line pt-3">
                  <select
                    value={editPlatform}
                    onChange={(e) => setEditPlatform(e.target.value as ChannelPlatform)}
                    className="h-8 rounded-lg border border-line bg-white px-2 text-[12.5px] text-ink outline-none"
                  >
                    <option value="mock">本地 Mock</option>
                    <option value="taobao">淘宝 TOP</option>
                    <option value="jd">京东 JOS</option>
                    <option value="douyin">抖音开放平台</option>
                    <option value="open">自定义开放平台</option>
                  </select>
                  <InputField value={editBaseUrl} onChange={setEditBaseUrl} placeholder="平台网关地址（留空=离线）" />
                  {editPlatform !== 'mock' && (
                    <>
                      <InputField value={editOptAppKey} onChange={setEditOptAppKey} placeholder="app_key" />
                      <InputField value={editOptAppSecret} onChange={setEditOptAppSecret} placeholder="app_secret（掩码）" />
                      <InputField value={editOptAccessToken} onChange={setEditOptAccessToken} placeholder="access_token（掩码）" wide />
                    </>
                  )}
                  <div className="col-span-2 flex justify-end gap-2">
                    <button
                      onClick={() => setEditing(null)}
                      className="h-8 rounded-lg px-3 text-[12.5px] text-ink-2 transition-colors hover:bg-black/5"
                    >
                      取消
                    </button>
                    <button
                      onClick={saveEdit}
                      className="h-8 rounded-lg bg-accent px-3 text-[12.5px] font-medium text-white transition-colors hover:bg-accent/90"
                    >
                      保存
                    </button>
                  </div>
                </div>
              )}
            </div>
          ))}
          {sources.length === 0 && <p className="text-[12px] text-ink-3">暂无渠道</p>}
        </div>
      )}
      {notice && <p className="text-[12px] text-ink-2">{notice}</p>}

      <div className="rounded-xl border border-line bg-black/[0.02] p-4">
        <div className="mb-2 text-[12.5px] font-medium text-ink">新增渠道</div>
        <div className="grid grid-cols-2 gap-2">
          <InputField value={name} onChange={setName} placeholder="渠道标识（name）" />
          <InputField value={label} onChange={setLabel} placeholder="展示名（如 拼多多）" />
          <InputField value={baseUrl} onChange={setBaseUrl} placeholder="平台地址（留空=本地 mock）" />
          <select
            value={platform}
            onChange={(e) => setPlatform(e.target.value as ChannelPlatform)}
            className="h-8 rounded-lg border border-line bg-white px-2 text-[12.5px] text-ink outline-none"
          >
            <option value="mock">本地 Mock（免费离线）</option>
            <option value="taobao">淘宝开放平台 TOP</option>
            <option value="jd">京东宙斯 JOS</option>
            <option value="douyin">抖音电商开放平台</option>
            <option value="open">自定义开放平台</option>
          </select>
          <div className="flex items-center gap-2">
            <select
              value={authType}
              onChange={(e) => setAuthType(e.target.value as 'mock' | 'api_key')}
              className="h-8 flex-1 rounded-lg border border-line bg-white px-2 text-[12.5px] text-ink outline-none"
            >
              <option value="mock">mock 鉴权</option>
              <option value="api_key">API Key</option>
            </select>
            <button
              onClick={addSource}
              className="h-8 rounded-lg bg-accent px-3 text-[12.5px] font-medium text-white transition-colors hover:bg-accent/90"
            >
              添加
            </button>
          </div>
          {authType === 'api_key' && (
            <InputField value={apiKey} onChange={setApiKey} placeholder="API Key（掩码存储）" wide />
          )}
          {platform !== 'mock' && (
            <>
              <InputField value={optAppKey} onChange={setOptAppKey} placeholder="app_key" />
              <InputField value={optAppSecret} onChange={setOptAppSecret} placeholder="app_secret（掩码）" />
              <InputField value={optAccessToken} onChange={setOptAccessToken} placeholder="access_token（掩码）" wide />
            </>
          )}
        </div>
      </div>
      <p className="text-[11.5px] leading-relaxed text-ink-3">
        渠道统一经 MCP + REST 协议接入：新增渠道写配置后即时生效，无需重启。选「本地 Mock」离线免费；
        真实平台（淘宝 TOP / 京东 JOS / 抖音）需平台资质，适配器接缝已留好，逐平台接入中。
      </p>
    </div>
  );
}

function SkillSettings() {
  return <SkillManager />;
}

function InputField({
  value,
  onChange,
  placeholder,
  wide,
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder: string;
  wide?: boolean;
}) {
  return (
    <input
      value={value}
      onChange={(e) => onChange(e.target.value)}
      placeholder={placeholder}
      className={`${wide ? 'col-span-2' : ''} h-8 rounded-lg border border-line bg-white px-2.5 text-[12.5px] text-ink outline-none placeholder:text-ink-3 focus:border-accent`}
    />
  );
}

function PermissionSettings({ mode, onModeChange }: { mode: PermissionModeType; onModeChange: (m: PermissionModeType) => void }) {
  return (
    <div className="space-y-2">
      {MODE_INFO.map((m) => (
        <button
          key={m.id}
          onClick={() => onModeChange(m.id)}
          className={`w-full rounded-xl border px-4 py-3 text-left transition-colors ${
            mode === m.id ? 'border-accent/50 bg-accent-soft' : 'border-line hover:border-line-strong'
          }`}
        >
          <div className="flex items-center gap-2.5">
            <span className={`h-2 w-2 rounded-full ${m.dot}`} />
            <span className="text-[13.5px] font-medium text-ink">{m.name}</span>
            {mode === m.id && <span className="ml-auto text-[11.5px] font-medium text-accent">当前</span>}
          </div>
          <p className="mt-1 pl-[18px] text-[12px] text-ink-3">{m.desc}</p>
        </button>
      ))}
    </div>
  );
}

function NotifySettings() {
  const [threshold, setThreshold] = useState(50);
  const [priceChange, setPriceChange] = useState(true);
  const [promoExpiry, setPromoExpiry] = useState(true);
  const [dailyReport, setDailyReport] = useState(false);

  /** 通知服务未开通：开关可交互（本地预览），点击时提示实际状态 */
  const notifyToggle = (setter: (v: boolean) => void) => (v: boolean) => {
    setter(v);
    toast('通知推送服务未开通：企业微信 / 短信通道接入中，设置暂不生效', 'warn');
  };

  return (
    <div className="space-y-5">
      <div className="flex items-start gap-2 rounded-xl border border-[#F0D48A] bg-[#FFFBEB] px-4 py-3">
        <svg className="mt-0.5 h-4 w-4 flex-shrink-0 text-[#92610A]" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
          <path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
          <line x1="12" y1="9" x2="12" y2="13" />
          <line x1="12" y1="17" x2="12.01" y2="17" />
        </svg>
        <div>
          <div className="text-[12.5px] font-medium text-[#92610A]">通知推送服务未开通</div>
          <div className="mt-0.5 text-[11.5px] leading-relaxed text-[#92610A]/80">
            企业微信 / 短信通道接入中，以下设置可预览交互但暂不生效；经营异常当前可在「工作台 → 总览」查看。
          </div>
        </div>
      </div>
      <div>
        <div className="mb-2 flex items-center justify-between">
          <span className="text-[12.5px] font-medium text-ink">库存预警阈值</span>
          <span className="font-mono text-[12.5px] text-ink-2">{threshold} 件</span>
        </div>
        <input
          type="range"
          min="10"
          max="200"
          step="10"
          value={threshold}
          onChange={(e) => setThreshold(parseInt(e.target.value, 10))}
          className="w-full accent-accent"
        />
      </div>
      <ToggleRow label="价格变动通知" desc="商品价格调整时推送通知" checked={priceChange} onChange={notifyToggle(setPriceChange)} />
      <ToggleRow label="促销到期提醒" desc="促销活动结束前 24 小时提醒" checked={promoExpiry} onChange={notifyToggle(setPromoExpiry)} />
      <ToggleRow label="每日运营报告" desc="每日自动生成运营数据汇总" checked={dailyReport} onChange={notifyToggle(setDailyReport)} />
    </div>
  );
}

function ToggleRow({ label, desc, checked, onChange }: { label: string; desc: string; checked: boolean; onChange: (v: boolean) => void }) {
  return (
    <div className="flex items-center justify-between">
      <div>
        <div className="text-[12.5px] font-medium text-ink">{label}</div>
        <div className="mt-0.5 text-[11.5px] text-ink-3">{desc}</div>
      </div>
      <button
        onClick={() => onChange(!checked)}
        className={`relative h-5 w-9 flex-shrink-0 rounded-full transition-colors ${checked ? 'bg-accent' : 'bg-ink-3/40'}`}
        aria-pressed={checked}
      >
        <span
          className={`absolute top-0.5 h-4 w-4 rounded-full bg-white shadow-card transition-transform ${
            checked ? 'translate-x-[18px]' : 'translate-x-0.5'
          }`}
        />
      </button>
    </div>
  );
}
