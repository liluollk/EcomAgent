import { api } from '../../lib/api';
import { useCallback, useEffect, useState } from 'react';
import { relativeTime } from '../../lib/format';

interface PendingApproval {
  request_id: string;
  session_id: string;
  tool_name: string;
  tool_input: Record<string, unknown>;
  reason: string;
  timestamp: number;
}

interface ApprovalRecord {
  request_id: string;
  session_id: string;
  tool_name: string;
  tool_input?: Record<string, unknown>;
  approved: boolean;
  timestamp: number;
}

/** 审批中心 — 跨会话权限请求的聚合审批与历史（REST 双入口之一，对话内权限卡照常可用）。 */
export function ApprovalsPage() {
  const [pending, setPending] = useState<PendingApproval[]>([]);
  const [history, setHistory] = useState<ApprovalRecord[]>([]);
  const [deciding, setDeciding] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [p, h] = await Promise.all([
        api('/approvals/pending').then((r) => r.json()),
        api('/approvals/history').then((r) => r.json()),
      ]);
      setPending(p.pending ?? []);
      setHistory(h.history ?? []);
    } catch {
      /* 静默：下一轮轮询重试 */
    }
  }, []);

  useEffect(() => {
    void refresh();
    const timer = setInterval(() => void refresh(), 5000);
    return () => clearInterval(timer);
  }, [refresh]);

  const decide = useCallback(
    async (requestId: string, approved: boolean) => {
      setDeciding(requestId);
      try {
        await api(`/approvals/${requestId}/decision`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ approved }),
        });
        await refresh();
      } finally {
        setDeciding(null);
      }
    },
    [refresh],
  );

  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-y-auto">
      <div className="flex flex-shrink-0 items-center border-b border-line px-5 py-2.5">
        <span className="text-[13px] font-medium text-ink">待审批</span>
        {pending.length > 0 && (
          <span className="ml-2 rounded-full bg-[#c2403a] px-2 py-0.5 text-[11px] font-semibold text-white">
            {pending.length}
          </span>
        )}
        <span className="ml-auto text-[11.5px] text-ink-3">每 5 秒自动刷新</span>
      </div>

      <div className="flex flex-col gap-2.5 px-5 py-4">
        {pending.length === 0 && (
          <div className="rounded-xl border border-line bg-elevated px-5 py-8 text-center text-[12.5px] text-ink-3 shadow-card">
            当前没有待审批的权限请求
          </div>
        )}
        {pending.map((item) => (
          <div
            key={item.request_id}
            className="flex flex-col gap-2 rounded-xl border border-line bg-elevated px-4 py-3 shadow-card"
          >
            <div className="flex items-center gap-2">
              <span className="rounded-md bg-inset px-2 py-0.5 font-mono text-[12px] text-ink">
                {item.tool_name}
              </span>
              <span className="text-[11.5px] text-ink-3">会话 {item.session_id.slice(0, 8)}</span>
              <span className="ml-auto text-[11.5px] text-ink-3">{relativeTime(item.timestamp)}</span>
            </div>
            {item.reason && <div className="text-[12.5px] leading-relaxed text-ink-2">{item.reason}</div>}
            <div className="rounded-lg bg-inset px-3 py-2 font-mono text-[11.5px] leading-relaxed text-ink-2">
              {JSON.stringify(item.tool_input)}
            </div>
            <div className="flex gap-2">
              <button
                onClick={() => void decide(item.request_id, true)}
                disabled={deciding === item.request_id}
                className="rounded-lg bg-accent px-3.5 py-1.5 text-[12px] font-medium text-white transition-colors hover:bg-accent-hover disabled:opacity-50"
              >
                批准执行
              </button>
              <button
                onClick={() => void decide(item.request_id, false)}
                disabled={deciding === item.request_id}
                className="rounded-lg border border-line px-3.5 py-1.5 text-[12px] font-medium text-ink-2 transition-colors hover:bg-inset disabled:opacity-50"
              >
                拒绝
              </button>
            </div>
          </div>
        ))}
      </div>

      <div className="flex flex-shrink-0 items-center border-b border-line px-5 py-2.5">
        <span className="text-[13px] font-medium text-ink">审批历史</span>
        <span className="ml-auto text-[11.5px] text-ink-3">共 {history.length} 条</span>
      </div>
      <div className="flex flex-col gap-1.5 px-5 py-4">
        {history.length === 0 && (
          <div className="text-center text-[12px] text-ink-3">暂无审批记录</div>
        )}
        {history.map((rec) => (
          <div
            key={rec.request_id}
            className="flex items-center gap-2.5 rounded-lg border border-line bg-elevated px-3.5 py-2 shadow-card"
          >
            <span
              className={`text-[13px] font-semibold ${rec.approved ? 'text-[#1a7f5a]' : 'text-[#c2403a]'}`}
            >
              {rec.approved ? '✓' : '✗'}
            </span>
            <span className="font-mono text-[12px] text-ink">{rec.tool_name}</span>
            <span className="text-[11.5px] text-ink-3">会话 {rec.session_id.slice(0, 8)}</span>
            <span className="ml-auto text-[11.5px] text-ink-3">
              {rec.approved ? '已批准' : '已拒绝'} · {relativeTime(rec.timestamp)}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
