import { useState } from "react";

import type { ConfirmationInteraction } from "./eventReducer";

export type ConfirmationDecision = "accept" | "reject";

export interface ConfirmationCardProps {
  request: ConfirmationInteraction;
  onDecide: (requestId: string, decision: ConfirmationDecision) => Promise<void> | void;
}

/** Confirmation Request 卡片:展示摘要与资源版本,单次确认/拒绝。 */
export function ConfirmationCard({ request, onDecide }: ConfirmationCardProps) {
  const [busy, setBusy] = useState<ConfirmationDecision | null>(null);

  const decide = async (decision: ConfirmationDecision) => {
    if (busy !== null) {
      return;
    }
    setBusy(decision);
    try {
      await onDecide(request.id, decision);
    } finally {
      setBusy(null);
    }
  };

  const resourceVersions = Object.entries(request.resourceVersions);

  return (
    <section aria-label="写操作确认" data-request-id={request.id}>
      <h3>写操作确认</h3>
      {request.summary && <p>{request.summary}</p>}
      {request.normalizedArguments && (
        <p>参数:{JSON.stringify(request.normalizedArguments)}</p>
      )}
      {resourceVersions.length > 0 && (
        <p>
          资源版本:
          {resourceVersions.map(([key, value]) => `${key}=${String(value)}`).join(",")}
        </p>
      )}
      <button type="button" onClick={() => decide("accept")} disabled={busy !== null}>
        确认执行
      </button>
      <button type="button" onClick={() => decide("reject")} disabled={busy !== null}>
        拒绝
      </button>
    </section>
  );
}
