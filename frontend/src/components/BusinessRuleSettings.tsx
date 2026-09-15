import { useEffect, useState } from 'react';
import type { WorkspaceConfig } from '../types';

interface BusinessRuleSettingsProps {
  workspaceId: string;
}

export function BusinessRuleSettings({ workspaceId }: BusinessRuleSettingsProps) {
  const [enabled, setEnabled] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    setNotice(null);
    fetch('/workspaces')
      .then(async (response) => {
        if (!response.ok) throw new Error('加载工作空间失败');
        return (await response.json()) as WorkspaceConfig[];
      })
      .then((workspaces) => {
        if (cancelled) return;
        const workspace = workspaces.find((item) => item.workspace_id === workspaceId);
        if (!workspace) throw new Error('当前工作空间不存在');
        setEnabled(workspace.rules.some((rule) => rule.type === 'price_above_cost' && rule.enabled !== false));
      })
      .catch((reason: unknown) => {
        if (!cancelled) setError(reason instanceof Error ? reason.message : '加载业务规则失败');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [workspaceId]);

  async function changeRule(next: boolean) {
    if (!next && !window.confirm('关闭成本保护后，低于成本价的调价将不再被业务规则拦截，确定关闭吗？')) {
      return;
    }
    const previous = enabled;
    setEnabled(next);
    setSaving(true);
    setError(null);
    setNotice(null);
    try {
      const response = await fetch(`/workspaces/${encodeURIComponent(workspaceId)}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          rules: next ? [{ type: 'price_above_cost', enabled: true }] : [],
        }),
      });
      const data = (await response.json()) as { error?: string; rules?: WorkspaceConfig['rules'] };
      if (!response.ok) throw new Error(data.error || '保存业务规则失败');
      setEnabled((data.rules ?? []).some((rule) => rule.type === 'price_above_cost' && rule.enabled !== false));
      setNotice(next ? '成本保护已启用' : '成本保护已关闭');
    } catch (reason: unknown) {
      setEnabled(previous);
      setError(reason instanceof Error ? reason.message : '保存业务规则失败');
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="space-y-5">
      <div>
        <div className="text-[12.5px] font-medium text-ink">业务规则</div>
        <p className="mt-1 text-[11.5px] leading-relaxed text-ink-3">
          规则由服务端在工具执行前强制判断，前端开关不会绕过服务端安全检查。
        </p>
      </div>

      {loading ? (
        <p className="text-[12px] text-ink-3">加载中…</p>
      ) : (
        <ToggleRow
          label="成本保护"
          desc="调价前必须能从内部成本数据确认目标价不低于成本价"
          checked={enabled}
          disabled={saving}
          onChange={(value) => void changeRule(value)}
        />
      )}
      {saving && <p className="text-[11.5px] text-ink-3">保存中…</p>}
      {notice && <p className="text-[11.5px] text-ok">{notice}</p>}
      {error && <p className="text-[11.5px] text-danger">{error}</p>}
    </div>
  );
}

function ToggleRow({
  label,
  desc,
  checked,
  disabled,
  onChange,
}: {
  label: string;
  desc: string;
  checked: boolean;
  disabled?: boolean;
  onChange: (value: boolean) => void;
}) {
  return (
    <div className="flex items-center justify-between">
      <div className="pr-4">
        <div className="text-[12.5px] font-medium text-ink">{label}</div>
        <div className="mt-0.5 text-[11.5px] leading-relaxed text-ink-3">{desc}</div>
      </div>
      <button
        type="button"
        disabled={disabled}
        onClick={() => onChange(!checked)}
        className={`relative h-5 w-9 flex-shrink-0 rounded-full transition-colors disabled:cursor-wait disabled:opacity-60 ${
          checked ? 'bg-accent' : 'bg-ink-3/40'
        }`}
        aria-label={label}
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
