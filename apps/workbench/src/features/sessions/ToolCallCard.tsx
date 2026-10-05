import type { ToolCallProjection } from "./eventReducer";

const toolStatusLabels: Record<string, string> = {
  PROPOSED: "待执行",
  VALIDATING: "校验中",
  WAITING_INPUT: "等待输入",
  WAITING_CONFIRMATION: "等待确认",
  SUCCEEDED: "成功",
  FAILED: "失败",
  DENIED: "已拒绝",
  SKIPPED: "已跳过",
};

export interface ToolCallCardProps {
  toolCall: ToolCallProjection;
}

/** ToolCall 卡片:名称、参数摘要与结果状态。 */
export function ToolCallCard({ toolCall }: ToolCallCardProps) {
  const statusLabel = toolStatusLabels[toolCall.status] ?? toolCall.status;
  return (
    <li data-ordinal={toolCall.ordinal}>
      <span>#{toolCall.ordinal}</span>
      <span>{toolCall.toolName ?? "工具调用"}</span>
      <span>{statusLabel}</span>
      {toolCall.rawArguments && <span>参数:{JSON.stringify(toolCall.rawArguments)}</span>}
      {toolCall.reasonCode && <span>拒绝原因:{toolCall.reasonCode}</span>}
      {toolCall.failureCode && <span>失败代码:{toolCall.failureCode}</span>}
    </li>
  );
}
