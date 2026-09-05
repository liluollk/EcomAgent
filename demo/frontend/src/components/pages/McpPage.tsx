import { useCallback, useEffect, useState } from 'react';
import { api } from '../../lib/api';

interface McpTool {
  name: string;
  description: string;
}

interface McpServerStatus {
  name: string;
  connected: boolean;
  tools: McpTool[];
}

interface McpStatus {
  connected: boolean;
  transport: string;
  servers: McpServerStatus[];
  tools: McpTool[];
}

interface McpServerConfig {
  name: string;
  command: string;
  args: string[];
  env: Record<string, string>;
  enabled: boolean;
}

/** MCP 页：外部工具服务通道 — server 配置 CRUD（用户自行接入想用的 MCP）+ 连接状态与工具发现聚合 */
export function McpPage() {
  const [status, setStatus] = useState<McpStatus | null>(null);
  const [servers, setServers] = useState<McpServerConfig[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [testing, setTesting] = useState<string | null>(null);

  const [addName, setAddName] = useState('');
  const [addCommand, setAddCommand] = useState('');
  const [addArgs, setAddArgs] = useState('');

  const refresh = useCallback(async () => {
    try {
      const [st, sv] = await Promise.all([api('/mcp/status'), fetch('/mcp/servers')]);
      if (!st.ok || !sv.ok) throw new Error(`HTTP ${st.status}/${sv.status}`);
      setStatus((await st.json()) as McpStatus);
      setServers((await sv.json()) as McpServerConfig[]);
      setError(null);
    } catch {
      setError('无法加载 MCP 状态，请确认后端已启动');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  async function addServer() {
    if (!addName.trim() || !addCommand.trim()) { setNotice('name 与 command 必填'); return; }
    const resp = await api('/mcp/servers', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        name: addName.trim(),
        command: addCommand.trim(),
        args: addArgs.trim() ? addArgs.trim().split(/\s+/) : [],
      }),
    });
    const data = await resp.json();
    setNotice(resp.ok ? `已新增 MCP server ${data.name}（重启服务或重连后生效）` : data.error || '新增失败');
    if (resp.ok) { setAddName(''); setAddCommand(''); setAddArgs(''); }
    void refresh();
  }

  async function toggleServer(s: McpServerConfig) {
    await api(`/mcp/servers/${s.name}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ enabled: !s.enabled }),
    });
    void refresh();
  }

  async function removeServer(s: McpServerConfig) {
    const resp = await api(`/mcp/servers/${s.name}`, { method: 'DELETE' });
    const data = await resp.json();
    setNotice(data.error || `已删除 MCP server ${s.name}`);
    void refresh();
  }

  async function testServer(s: McpServerConfig) {
    setTesting(s.name);
    try {
      const resp = await api(`/mcp/servers/${s.name}/test`, { method: 'POST' });
      const data = await resp.json();
      setNotice(data.ok ? `${s.name} 连通正常，发现 ${data.tools.length} 个工具` : `${s.name} 连接失败：${data.error}`);
    } finally {
      setTesting(null);
    }
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex flex-shrink-0 items-center justify-between border-b border-line px-5 py-2.5">
        <span className="text-[13px] font-medium text-ink">MCP 外部工具服务</span>
        <div className="flex items-center gap-2">
          <span
            className={`rounded-full border px-2.5 py-1 text-[11px] font-medium ${
              loading
                ? 'border-line bg-black/[0.02] text-ink-3'
                : status?.connected
                  ? 'border-[#BBE7CDAF] bg-[#ECFDF3] text-ok'
                  : 'border-[#F0D48A] bg-[#FFFBEB] text-[#92610A]'
            }`}
          >
            {loading ? '检测中…' : status?.connected ? '已连接 · stdio 子进程' : '未连接'}
          </span>
          <button
            onClick={() => {
              setLoading(true);
              void refresh();
            }}
            title="刷新"
            className="flex h-6 w-6 items-center justify-center rounded-md text-ink-3 transition-colors hover:bg-black/5 hover:text-ink"
          >
            <svg className="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M21 12a9 9 0 1 1-2.64-6.36" />
              <path d="M21 3v6h-6" />
            </svg>
          </button>
        </div>
      </div>

      <div className="flex-1 overflow-y-auto">
        <div className="mx-auto max-w-[860px] animate-fade-up space-y-4 px-6 py-5">
          {error ? (
            <div className="rounded-xl border border-line bg-elevated px-6 py-12 text-center text-[13px] text-ink-3 shadow-card">
              {error}
            </div>
          ) : loading || !status ? (
            <div className="py-14 text-center text-[13px] text-ink-3">加载 MCP 状态中…</div>
          ) : (
            <>
              <div className="grid grid-cols-3 gap-4">
                <InfoCard label="连接状态" value={status.connected ? '已连接' : '未连接'} tone={status.connected ? 'ok' : 'warn'} />
                <InfoCard label="传输协议" value={status.transport} mono />
                <InfoCard label="发现工具" value={`${status.tools.length} 个`} />
              </div>

              {/* server 配置管理 */}
              <div className="overflow-hidden rounded-xl border border-line bg-elevated shadow-card">
                <div className="border-b border-line px-4 py-2.5 text-[12.5px] font-medium text-ink">
                  MCP Server 配置（用户可自行接入外部 MCP 服务）
                </div>
                {servers.map((s) => {
                  const live = status.servers.find((x) => x.name === s.name);
                  return (
                    <div key={s.name} className="flex items-center justify-between border-b border-line px-4 py-2.5 last:border-b-0">
                      <div className="flex min-w-0 items-center gap-2.5">
                        <span className={`h-2 w-2 flex-shrink-0 rounded-full ${s.enabled ? (live?.connected ? 'bg-ok' : 'bg-ink-3/50') : 'bg-ink-3/30'}`} />
                        <span className="text-[13px] text-ink">{s.name}</span>
                        <span className="truncate font-mono text-[11px] text-ink-3">{s.command} {s.args.join(' ')}</span>
                        {!s.enabled && <span className="flex-shrink-0 text-[11px] text-ink-3">已停用</span>}
                        {live?.connected && <span className="flex-shrink-0 text-[11px] text-ok">{live.tools.length} 工具</span>}
                      </div>
                      <div className="flex flex-shrink-0 items-center gap-1">
                        <button onClick={() => void testServer(s)} disabled={testing === s.name}
                          className="rounded-md px-2 py-1 text-[11.5px] text-ink-2 transition-colors hover:bg-black/5 disabled:opacity-50">
                          {testing === s.name ? '测试中…' : '测连通'}
                        </button>
                        <button onClick={() => void toggleServer(s)}
                          className="rounded-md px-2 py-1 text-[11.5px] text-ink-2 transition-colors hover:bg-black/5">
                          {s.enabled ? '停用' : '启用'}
                        </button>
                        <button onClick={() => void removeServer(s)}
                          className="rounded-md px-2 py-1 text-[11.5px] text-danger transition-colors hover:bg-red-50">删除</button>
                      </div>
                    </div>
                  );
                })}
                <div className="grid grid-cols-2 gap-2 border-t border-line bg-black/[0.02] p-4">
                  <input value={addName} onChange={(e) => setAddName(e.target.value)} placeholder="标识（name）"
                    className="h-8 rounded-lg border border-line bg-white px-2.5 text-[12.5px] text-ink outline-none placeholder:text-ink-3 focus:border-accent" />
                  <input value={addCommand} onChange={(e) => setAddCommand(e.target.value)} placeholder="启动命令（command，如 python）"
                    className="h-8 rounded-lg border border-line bg-white px-2.5 text-[12.5px] text-ink outline-none placeholder:text-ink-3 focus:border-accent" />
                  <input value={addArgs} onChange={(e) => setAddArgs(e.target.value)} placeholder="参数（空格分隔，如 -m some_mcp_server）"
                    className="col-span-2 h-8 rounded-lg border border-line bg-white px-2.5 text-[12.5px] text-ink outline-none placeholder:text-ink-3 focus:border-accent" />
                  <button onClick={() => void addServer()}
                    className="col-span-2 h-8 rounded-lg bg-accent px-3 text-[12.5px] font-medium text-white transition-colors hover:bg-accent/90">
                    添加 MCP Server
                  </button>
                </div>
              </div>

              {notice && <p className="text-[12px] text-ink-2">{notice}</p>}

              <div className="overflow-hidden rounded-xl border border-line bg-elevated shadow-card">
                <div className="border-b border-line px-4 py-2.5 text-[12.5px] font-medium text-ink">
                  工具清单（tools/list 真实发现）
                </div>
                {status.tools.map((t, idx) => (
                  <div
                    key={t.name}
                    className={`flex items-start gap-3 px-4 py-2.5 ${idx > 0 ? 'border-t border-line' : ''}`}
                  >
                    <span className="mt-0.5 flex-shrink-0 rounded bg-accent-soft px-1.5 py-0.5 font-mono text-[11px] text-accent">
                      {t.name}
                    </span>
                    <span className="min-w-0 text-[12px] leading-relaxed text-ink-2">{t.description || '—'}</span>
                  </div>
                ))}
                {status.tools.length === 0 && (
                  <div className="px-4 py-10 text-center text-[12.5px] text-ink-3">未发现工具（电商运营工具为内置通道，不在此列）</div>
                )}
              </div>

              <p className="text-[11.5px] leading-relaxed text-ink-3">
                MCP server 以 stdio 子进程连接，工具经 JSON-RPC 真实协议发现后注入执行链路；
                新增/修改配置后重启服务生效。电商运营工具（查库存 / 改价 / 上下架等）为内置平台 API 通道，
                本页面管理的是外部第三方工具。
              </p>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

function InfoCard({ label, value, tone, mono }: { label: string; value: string; tone?: 'ok' | 'warn'; mono?: boolean }) {
  return (
    <div className="rounded-xl border border-line bg-elevated p-4 shadow-card">
      <div className="text-[12px] text-ink-3">{label}</div>
      <div
        className={`mt-1 text-[15px] font-semibold ${
          tone === 'ok' ? 'text-ok' : tone === 'warn' ? 'text-warn' : 'text-ink'
        } ${mono ? 'font-mono text-[13px]' : ''}`}
      >
        {value}
      </div>
    </div>
  );
}