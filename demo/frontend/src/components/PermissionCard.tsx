import { useState } from 'react';
import type { PermissionInfo } from '../types';
import { CHANNEL_META, prettyJSON, toolLabel, type ChannelId } from '../lib/format';

interface PermissionCardProps {
  permission: PermissionInfo;
  onRespond: (requestId: string, approved: boolean) => void;
}

/** 权限确认卡片：ASK 模式下 Agent 挂起等待，用户批准后继续执行、拒绝则回传 LLM */
export function PermissionCard({ permission, onRespond }: PermissionCardProps) {
  const [decision, setDecision] = useState<'approved' | 'denied' | null>(null);

  const respond = (approved: boolean) => {
    if (decision) return;
    setDecision(approved ? 'approved' : 'denied');
    onRespond(permission.requestId, approved);
  };

  // 调价审批的上下文：批之前得看清「改哪个平台的哪个商品、改成多少、触发了哪条规则」。
  // 非调价工具的审批没有这些字段（可选），卡片退化回原有的「工具名 + 入参」形态。
  const platformLabel = permission.platform
    ? (CHANNEL_META[permission.platform as ChannelId]?.label ?? permission.platform)
    : null;
  const productRefLabel = permission.productRef
    ? [permission.productRef.product_id, permission.productRef.sku_id].filter(Boolean).join(' / ')
    : null;
  const hasPriceContext =
    Boolean(platformLabel || productRefLabel || permission.operationId) || permission.targetPrice != null;

  return (
    <div
      className={`animate-fade-up rounded-xl border p-3.5 ${
        decision === 'denied'
          ? 'border-line bg-inset'
          : 'border-[#F0D48A] bg-[#FFFBEB]'
      }`}
    >
      <div className="flex flex-wrap items-center gap-2">
        <svg
          className={`h-4 w-4 flex-shrink-0 ${decision ? 'text-ink-3' : 'text-warn'}`}
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          strokeLinecap="round"
          strokeLinejoin="round"
        >
          <path d="M10.29 3.86 1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z" />
          <line x1="12" y1="9" x2="12" y2="13" />
          <line x1="12" y1="17" x2="12.01" y2="17" />
        </svg>
        <span className="text-[13px] font-semibold text-[#92610A]">权限确认</span>
        <span className="text-[12.5px] text-ink-2">
          Agent 请求执行 <span className="font-medium text-ink">{toolLabel(permission.toolName)}</span>
        </span>
        {decision ? (
          <span
            className={`ml-auto rounded-md px-2 py-0.5 text-[11px] font-medium ${
              decision === 'approved' ? 'bg-[#ECFDF3] text-ok' : 'bg-black/[0.06] text-ink-2'
            }`}
          >
            {decision === 'approved' ? '已批准' : '已拒绝'}
          </span>
        ) : (
          <span className="ml-auto rounded-md bg-[#F7E8B8] px-2 py-0.5 text-[11px] font-medium text-[#92610A]">
            等待确认
          </span>
        )}
      </div>
      {hasPriceContext && (
        <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[12px] text-ink-2">
          {platformLabel && (
            <span>
              平台 <span className="font-medium text-ink">{platformLabel}</span>
            </span>
          )}
          {productRefLabel && (
            <span>
              商品 <span className="font-mono text-[11.5px] text-ink">{productRefLabel}</span>
            </span>
          )}
          {permission.targetPrice != null && (
            <span>
              目标价 <span className="font-medium text-ink">¥{permission.targetPrice}</span>
            </span>
          )}
          {permission.operationId && (
            <span className="break-all text-ink-3">
              操作 <span className="font-mono text-[11px]">{permission.operationId}</span>
            </span>
          )}
        </div>
      )}
      {permission.ruleSummary && (
        <p className="mt-1.5 text-[12px] leading-relaxed text-ink-2">规则校验：{permission.ruleSummary}</p>
      )}
      {permission.reason && <p className="mt-2 text-[12.5px] leading-relaxed text-ink-2">{permission.reason}</p>}
      <pre className="mt-2 overflow-x-auto rounded-lg bg-white/70 px-3 py-2 font-mono text-[11.5px] leading-relaxed text-ink-2">
        {prettyJSON(permission.toolInput)}
      </pre>

      {decision ? (
        <p className="mt-2 text-[11px] text-ink-3">
          {decision === 'approved' ? '已批准，Agent 正在继续执行该操作。' : '已拒绝，Agent 将收到拒绝结果并调整方案。'}
        </p>
      ) : (
        <div className="mt-2.5 flex items-center gap-2">
          <button
            onClick={() => respond(true)}
            className="flex h-7 items-center gap-1 rounded-lg bg-ok px-3 text-[12px] font-medium text-white transition-opacity hover:opacity-90"
          >
            <svg className="h-3.5 w-3.5" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
              <polyline points="20 6 9 17 4 12" />
            </svg>
            批准
          </button>
          <button
            onClick={() => respond(false)}
            className="flex h-7 items-center gap-1 rounded-lg border border-danger/40 px-3 text-[12px] font-medium text-danger transition-colors hover:bg-[#FDEDED]"
          >
            <svg className="h-3 w-3" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.5" strokeLinecap="round">
              <line x1="18" y1="6" x2="6" y2="18" />
              <line x1="6" y1="6" x2="18" y2="18" />
            </svg>
            拒绝
          </button>
          <span className="text-[11px] text-ink-3">Agent 已挂起，等待你的决定</span>
        </div>
      )}
    </div>
  );
}
