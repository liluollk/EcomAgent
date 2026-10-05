import { useState } from "react";

import type { InputInteraction } from "./eventReducer";

export interface InputRequestCardProps {
  request: InputInteraction;
  onAnswer: (requestId: string, values: Record<string, unknown>) => Promise<void> | void;
}

/**
 * Input Request 卡片:field_paths 表单。
 * 单字段且有候选项(如店铺)→ 候选按钮,点击即以该 value 提交回答;
 * 其余情况 → 文本输入 + 提交回答按钮。
 */
export function InputRequestCard({ request, onAnswer }: InputRequestCardProps) {
  const [values, setValues] = useState<Record<string, string>>({});
  const [submitting, setSubmitting] = useState(false);

  const singleField = request.fieldPaths.length === 1 ? request.fieldPaths[0] : null;
  const hasOptions = request.options.length > 0 && singleField !== null;

  const ready = request.fieldPaths.every((path) => (values[path] ?? "").trim().length > 0);
  const canSubmit = ready && !submitting;

  const submitValues = async (answer: Record<string, unknown>) => {
    setSubmitting(true);
    try {
      await onAnswer(request.id, answer);
    } finally {
      setSubmitting(false);
    }
  };

  const submit = async () => {
    if (!canSubmit) {
      return;
    }
    await submitValues(
      Object.fromEntries(request.fieldPaths.map((path) => [path, values[path] ?? ""])),
    );
  };

  return (
    <section aria-label={`输入请求:${request.question}`} data-request-id={request.id}>
      <h3>需要补充信息</h3>
      <p>{request.question}</p>
      {request.fieldPaths.map((path) => (
        <fieldset key={path}>
          <legend>{path}</legend>
          {hasOptions && path === singleField ? (
            request.options.map((option) => (
              <button
                key={option.value}
                type="button"
                disabled={submitting}
                onClick={() => void submitValues({ [singleField]: option.value })}
              >
                {option.label}
              </button>
            ))
          ) : (
            <input
              type="text"
              aria-label={path}
              value={values[path] ?? ""}
              onChange={(changeEvent) =>
                setValues((prev) => ({ ...prev, [path]: changeEvent.target.value }))
              }
            />
          )}
        </fieldset>
      ))}
      {!hasOptions && (
        <button type="button" onClick={submit} disabled={!canSubmit}>
          提交回答
        </button>
      )}
    </section>
  );
}
