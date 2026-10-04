import { useState } from "react";

import type { InputInteraction } from "./eventReducer";

export interface InputRequestCardProps {
  request: InputInteraction;
  onAnswer: (requestId: string, values: Record<string, unknown>) => Promise<void> | void;
}

/** Input Request 卡片:field_paths 表单(有 options 用单选,否则文本输入)。 */
export function InputRequestCard({ request, onAnswer }: InputRequestCardProps) {
  const [values, setValues] = useState<Record<string, string>>({});
  const [submitting, setSubmitting] = useState(false);

  const ready = request.fieldPaths.every((path) => (values[path] ?? "").trim().length > 0);
  const canSubmit = ready && !submitting;

  const submit = async () => {
    if (!canSubmit) {
      return;
    }
    setSubmitting(true);
    try {
      await onAnswer(
        request.id,
        Object.fromEntries(request.fieldPaths.map((path) => [path, values[path] ?? ""])),
      );
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <section aria-label={`输入请求:${request.question}`} data-request-id={request.id}>
      <h3>需要补充信息</h3>
      <p>{request.question}</p>
      {request.fieldPaths.map((path) => (
        <fieldset key={path}>
          <legend>{path}</legend>
          {request.options.length > 0 ? (
            request.options.map((option) => (
              <label key={option}>
                <input
                  type="radio"
                  name={`${request.id}:${path}`}
                  value={option}
                  checked={values[path] === option}
                  onChange={() => setValues((prev) => ({ ...prev, [path]: option }))}
                />
                {option}
              </label>
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
      <button type="button" onClick={submit} disabled={!canSubmit}>
        提交回答
      </button>
    </section>
  );
}
