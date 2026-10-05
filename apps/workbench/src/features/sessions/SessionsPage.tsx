import { useState } from "react";

import { SessionPage } from "./SessionPage";

export interface SessionsPageProps {
  /** 打开会话时回调(App 用它同步 ?session= 深链地址)。 */
  onSessionOpened?: (sessionId: string) => void;
}

/**
 * Sessions 页面(计划 04 Task 9/Task 10)。
 *
 * 后端目前只提供 POST /api/sessions 与 GET /api/sessions/{id},尚无会话列表
 * 查询端点,故先按会话 ID 打开;会话内对话、Run 状态与人机交互见 SessionPage。
 */
export function SessionsPage({ onSessionOpened }: SessionsPageProps) {
  const [openSessionId, setOpenSessionId] = useState<string | null>(null);
  const [draft, setDraft] = useState("");

  if (openSessionId !== null) {
    return (
      <div>
        <button type="button" onClick={() => setOpenSessionId(null)}>
          返回会话列表
        </button>
        <SessionPage sessionId={openSessionId} />
      </div>
    );
  }

  return (
    <section aria-label="Sessions">
      <h1>Sessions</h1>
      <p>后端暂未提供会话列表查询,当前按会话 ID 打开会话。</p>
      <form
        onSubmit={(submitEvent) => {
          submitEvent.preventDefault();
          const sessionId = draft.trim();
          if (sessionId.length > 0) {
            setOpenSessionId(sessionId);
            onSessionOpened?.(sessionId);
          }
        }}
      >
        <input
          aria-label="会话 ID"
          value={draft}
          onChange={(changeEvent) => setDraft(changeEvent.target.value)}
        />
        <button type="submit">打开会话</button>
      </form>
    </section>
  );
}
