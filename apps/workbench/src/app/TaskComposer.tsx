import { useState } from "react";

import { getJSON, postJSON } from "../api/client";

interface AgentSummary {
  id: string;
  stable_key: string;
  name: string;
}

interface SessionCreated {
  id: string;
}

interface RunDispatched {
  run_id: string;
}

export interface TaskComposerProps {
  workspaceId: string;
  onSessionOpened: (sessionId: string) => void;
}

/**
 * 新任务编辑器:提交任务时创建会话(默认绑定目录内第一个 Agent)并
 * 发送首条用户消息,成功后打开该会话的 SessionPage。
 */
export function TaskComposer({ workspaceId, onSessionOpened }: TaskComposerProps) {
  const [draft, setDraft] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const send = async () => {
    const content = draft.trim();
    if (!content || sending) {
      return;
    }
    setSending(true);
    setError(null);
    try {
      const { agents } = await getJSON<{ agents: AgentSummary[] }>("/agents");
      const agent = agents[0];
      if (!agent) {
        throw new Error("工作区没有可用的 Agent");
      }
      const session = await postJSON<SessionCreated>("/sessions", {
        workspace_id: workspaceId,
        agent_id: agent.id,
      });
      await postJSON<RunDispatched>(`/sessions/${session.id}/messages`, { content });
      setDraft("");
      onSessionOpened(session.id);
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : String(cause));
    } finally {
      setSending(false);
    }
  };

  return (
    <section aria-label="新任务">
      <h2>提交新任务</h2>
      <textarea
        aria-label="新任务输入"
        value={draft}
        disabled={sending}
        onChange={(changeEvent) => setDraft(changeEvent.target.value)}
      />
      <button
        type="button"
        onClick={send}
        disabled={sending || draft.trim().length === 0}
      >
        发送任务
      </button>
      {error && <p role="alert">{error}</p>}
    </section>
  );
}
