import { describe, expect, it } from "vitest";

import type { AgentEvent, SessionSnapshot } from "./types";
import { projectionFromSnapshot, reduceAgentEvent } from "./eventReducer";

function snapshot(runId: string, lastSequenceNo: number, status: string): SessionSnapshot {
  return {
    id: "session-1",
    title: null,
    status: "ACTIVE",
    active_run_id: runId,
    messages: [],
    runs: [{ id: runId, status, events_cursor: lastSequenceNo }],
    pending_inputs: [],
    pending_confirmations: [],
  };
}

function event(sequenceNo: number, kind: string, overrides: Partial<AgentEvent> = {}): AgentEvent {
  return {
    id: `evt-${sequenceNo}-${kind}`,
    runId: "run-1",
    sequenceNo,
    kind,
    payload: {},
    ...overrides,
  };
}

describe("eventReducer 计划原文语义", () => {
  it("stops on an event gap and never reverses a terminal run", () => {
    const baseline = projectionFromSnapshot(snapshot("run-1", 4, "RUNNING"));
    const completed = reduceAgentEvent(baseline, event(5, "RUN_COMPLETED"));
    const withGap = reduceAgentEvent(completed, event(7, "RUN_RUNNING"));
    expect(withGap.needsSnapshot).toBe(true);
    expect(withGap.runs["run-1"].status).toBe("COMPLETED");
    const late = reduceAgentEvent(withGap, event(4, "RUN_RUNNING"));
    expect(late.runs["run-1"].status).toBe("COMPLETED");
  });
});

describe("eventReducer 快照重建", () => {
  it("projectionFromSnapshot 设置 lastSequenceNo、cursor 与活动 Run", () => {
    const projection = projectionFromSnapshot(snapshot("run-1", 4, "RUNNING"));

    expect(projection.needsSnapshot).toBe(false);
    expect(projection.cursor).toBe(4);
    expect(projection.activeRunId).toBe("run-1");
    expect(projection.runs["run-1"]).toMatchObject({
      id: "run-1",
      status: "RUNNING",
      lastSequenceNo: 4,
    });
  });

  it("快照重建后恢复事件消费(needsSnapshot 复位)", () => {
    const withGap = reduceAgentEvent(
      projectionFromSnapshot(snapshot("run-1", 4, "RUNNING")),
      event(6, "RUN_RUNNING"),
    );
    expect(withGap.needsSnapshot).toBe(true);

    const rebuilt = projectionFromSnapshot({ ...snapshot("run-1", 6, "RUNNING") });
    expect(rebuilt.needsSnapshot).toBe(false);
    const resumed = reduceAgentEvent(rebuilt, event(7, "RUN_COMPLETED"));
    expect(resumed.needsSnapshot).toBe(false);
    expect(resumed.runs["run-1"].status).toBe("COMPLETED");
  });
});

describe("eventReducer 去重与序号推进", () => {
  it("重复 event id 被忽略(at-least-once 去重)", () => {
    const baseline = projectionFromSnapshot(snapshot("run-1", 4, "RUNNING"));
    const first = reduceAgentEvent(baseline, event(5, "RUN_COMPLETED"));
    const duplicate = reduceAgentEvent(first, event(5, "RUN_COMPLETED"));

    expect(duplicate.runs["run-1"].status).toBe("COMPLETED");
    expect(duplicate.runs["run-1"].lastSequenceNo).toBe(5);
    expect(duplicate.runs["run-1"].lastSequenceNo).toBe(first.runs["run-1"].lastSequenceNo);
    expect(duplicate.needsSnapshot).toBe(false);
  });

  it("旧序号事件被忽略且不产生缺口", () => {
    const baseline = projectionFromSnapshot(snapshot("run-1", 4, "RUNNING"));
    const stale = reduceAgentEvent(baseline, event(2, "RUN_COMPLETED"));

    expect(stale.runs["run-1"].lastSequenceNo).toBe(4);
    expect(stale.runs["run-1"].status).toBe("RUNNING");
    expect(stale.needsSnapshot).toBe(false);
  });

  it("序号按事件推进,后续连续事件可继续消费", () => {
    const baseline = projectionFromSnapshot(snapshot("run-1", 4, "RUNNING"));
    const started = reduceAgentEvent(baseline, event(5, "RUN_STARTED"));
    const completed = reduceAgentEvent(started, event(6, "RUN_COMPLETED"));

    expect(completed.runs["run-1"].lastSequenceNo).toBe(6);
    expect(completed.runs["run-1"].status).toBe("COMPLETED");
  });
});

describe("eventReducer 缺口检测", () => {
  it("序号跳变置 needsSnapshot 并停止消费后续事件", () => {
    const baseline = projectionFromSnapshot(snapshot("run-1", 4, "RUNNING"));
    const withGap = reduceAgentEvent(baseline, event(6, "RUN_COMPLETED"));

    expect(withGap.needsSnapshot).toBe(true);
    // 缺口后事件不再消费,状态维持不变
    const ignored = reduceAgentEvent(withGap, event(7, "RUN_FAILED"));
    expect(ignored.needsSnapshot).toBe(true);
    expect(ignored.runs["run-1"].status).toBe("RUNNING");
    expect(ignored.runs["run-1"].lastSequenceNo).toBe(4);
  });

  it("未知 Run 的事件触发快照重建", () => {
    const baseline = projectionFromSnapshot(snapshot("run-1", 4, "RUNNING"));
    const unknown = reduceAgentEvent(
      baseline,
      event(5, "RUN_COMPLETED", { runId: "run-unknown" }),
    );

    expect(unknown.needsSnapshot).toBe(true);
  });
});

describe("eventReducer 终态不可逆", () => {
  it.each(["COMPLETED", "FAILED", "CANCELLED"])(
    "终态 %s 后忽略一切状态迁移事件(含迟到 RUN_STARTED)",
    (terminal) => {
      const baseline = projectionFromSnapshot(snapshot("run-1", 4, terminal));
      const afterMigration = reduceAgentEvent(baseline, event(5, "RUN_STARTED"));

      expect(afterMigration.runs["run-1"].status).toBe(terminal);
      // 序号仍需推进,否则后续事件会误判缺口
      expect(afterMigration.runs["run-1"].lastSequenceNo).toBe(5);
      expect(afterMigration.needsSnapshot).toBe(false);
    },
  );

  it("RUN_FAILED 的 failure_code 进入投影,但终态后不再覆盖", () => {
    const baseline = projectionFromSnapshot(snapshot("run-1", 4, "RUNNING"));
    const failed = reduceAgentEvent(
      baseline,
      event(5, "RUN_FAILED", { payload: { failure_code: "BACKEND_FAILED" } }),
    );
    expect(failed.runs["run-1"].status).toBe("FAILED");
    expect(failed.runs["run-1"].failureCode).toBe("BACKEND_FAILED");

    const overwritten = reduceAgentEvent(
      failed,
      event(6, "RUN_FAILED", { payload: { failure_code: "OTHER" } }),
    );
    expect(overwritten.runs["run-1"].status).toBe("FAILED");
    expect(overwritten.runs["run-1"].failureCode).toBe("BACKEND_FAILED");
  });
});

describe("eventReducer 事件驱动投影(按后端实际 event_kind)", () => {
  it("RUN_WAITING_INPUT / RUN_WAITING_CONFIRMATION 更新状态并请求快照以获取 Request 详情", () => {
    const baseline = projectionFromSnapshot(snapshot("run-1", 4, "RUNNING"));
    const waitingInput = reduceAgentEvent(baseline, event(5, "RUN_WAITING_INPUT"));
    expect(waitingInput.runs["run-1"].status).toBe("WAITING_INPUT");
    expect(waitingInput.needsSnapshot).toBe(true);

    const rebuilt = projectionFromSnapshot({ ...snapshot("run-1", 5, "WAITING_INPUT") });
    const waitingConfirmation = reduceAgentEvent(rebuilt, event(6, "RUN_WAITING_CONFIRMATION"));
    expect(waitingConfirmation.runs["run-1"].status).toBe("WAITING_CONFIRMATION");
    expect(waitingConfirmation.needsSnapshot).toBe(true);
  });

  it("TOOL_CALL_PROPOSED / TOOL_CALL_DENIED 驱动 ToolCall 投影且不改变 Run 状态", () => {
    const baseline = projectionFromSnapshot(snapshot("run-1", 4, "RUNNING"));
    const proposed = reduceAgentEvent(
      baseline,
      event(5, "TOOL_CALL_PROPOSED", {
        payload: { call_id: "call-1", tool_name: "update_price", ordinal: 1 },
      }),
    );
    expect(proposed.runs["run-1"].status).toBe("RUNNING");
    expect(proposed.toolCalls["run-1:1"]).toMatchObject({
      runId: "run-1",
      ordinal: 1,
      toolName: "update_price",
      status: "PROPOSED",
    });

    const denied = reduceAgentEvent(
      proposed,
      event(6, "TOOL_CALL_DENIED", {
        payload: { call_id: "call-1", tool_name: "update_price", reason_code: "TOOL_UNKNOWN" },
      }),
    );
    expect(denied.toolCalls["run-1:1"]).toMatchObject({
      status: "DENIED",
      reasonCode: "TOOL_UNKNOWN",
    });
  });

  it("RUN_RESUMED 将 Run 置为 QUEUED 并清除该 Run 的待处理交互", () => {
    const withPending = projectionFromSnapshot({
      ...snapshot("run-1", 4, "WAITING_INPUT"),
      pending_inputs: [
        {
          id: "req-1",
          run_id: "run-1",
          field_paths: ["store_id"],
          question: "请选择操作店铺",
          status: "PENDING",
        },
      ],
      pending_confirmations: [
        { id: "req-2", run_id: "run-1", status: "PENDING" },
      ],
    });
    expect(withPending.interactions.inputs).toHaveLength(1);
    expect(withPending.interactions.confirmations).toHaveLength(1);

    const resumed = reduceAgentEvent(withPending, event(5, "RUN_RESUMED"));
    expect(resumed.runs["run-1"].status).toBe("QUEUED");
    expect(resumed.interactions.inputs).toHaveLength(0);
    expect(resumed.interactions.confirmations).toHaveLength(0);
  });

  it("TOOL_CALL_PROPOSED 保留 call_id,DENIED 按 call_id 匹配(后端 payload 无 ordinal)", () => {
    const baseline = projectionFromSnapshot(snapshot("run-1", 4, "RUNNING"));
    const proposed = reduceAgentEvent(
      baseline,
      event(5, "TOOL_CALL_PROPOSED", {
        payload: { call_id: "call-9", tool_name: "update_price", ordinal: 2 },
      }),
    );
    expect(proposed.toolCalls["run-1:2"]).toMatchObject({ callId: "call-9", ordinal: 2 });

    const denied = reduceAgentEvent(
      proposed,
      event(6, "TOOL_CALL_DENIED", {
        payload: { call_id: "call-9", tool_name: "update_price", reason_code: "TOOL_UNKNOWN" },
      }),
    );
    expect(denied.toolCalls["run-1:2"]).toMatchObject({
      status: "DENIED",
      reasonCode: "TOOL_UNKNOWN",
    });
  });
});

describe("eventReducer 快照投影内容", () => {
  it("快照 messages 按序进入投影,tool_calls 归属活动 Run", () => {
    const projection = projectionFromSnapshot({
      ...snapshot("run-1", 4, "RUNNING"),
      messages: [
        {
          id: "m-2",
          run_id: "run-1",
          role: "ASSISTANT",
          status: "COMPLETED",
          content: "SKU-001 当前价格:99.00 元",
          sequence_no: 2,
        },
        {
          id: "m-1",
          run_id: "run-1",
          role: "USER",
          status: "COMPLETED",
          content: "把 SKU-001 改成 99 元",
          sequence_no: 1,
        },
      ],
      tool_calls: [
        {
          id: "tc-1",
          ordinal: 1,
          status: "SUCCEEDED",
          raw_arguments: { sku: "SKU-001", price: 99 },
          failure_code: null,
        },
      ],
    });

    expect(projection.messages.map((message) => message.id)).toEqual(["m-1", "m-2"]);
    expect(projection.toolCalls["run-1:1"]).toMatchObject({
      runId: "run-1",
      ordinal: 1,
      status: "SUCCEEDED",
      rawArguments: { sku: "SKU-001", price: 99 },
    });
  });

  it("快照 pending 请求进入 interactions 投影", () => {
    const projection = projectionFromSnapshot({
      ...snapshot("run-1", 4, "WAITING_CONFIRMATION"),
      pending_inputs: [
        {
          id: "req-1",
          run_id: "run-1",
          field_paths: ["store_id"],
          question: "请选择操作店铺",
          status: "PENDING",
        },
      ],
      pending_confirmations: [
        {
          id: "req-2",
          run_id: "run-1",
          summary: "将 SKU-001 价格 128.00 改为 99.00",
          resource_versions: { "sku/SKU-001": "v3" },
          status: "PENDING",
        },
      ],
    });

    expect(projection.interactions.inputs[0]).toMatchObject({ id: "req-1" });
    expect(projection.interactions.confirmations[0]).toMatchObject({
      id: "req-2",
      resourceVersions: { "sku/SKU-001": "v3" },
    });
  });
});

describe("eventReducer 纯函数", () => {
  it("reduceAgentEvent 不修改入参投影并返回新对象", () => {
    const baseline = projectionFromSnapshot(snapshot("run-1", 4, "RUNNING"));
    const frozen = structuredClone(baseline);
    const next = reduceAgentEvent(baseline, event(5, "RUN_COMPLETED"));

    expect(baseline).toEqual(frozen);
    expect(next).not.toBe(baseline);
    expect(next.runs["run-1"]).not.toBe(baseline.runs["run-1"]);
    expect(baseline.runs["run-1"].status).toBe("RUNNING");
  });

  it("needsSnapshot 的投影在快照重建前保持不变(停止消费)", () => {
    const withGap = reduceAgentEvent(
      projectionFromSnapshot(snapshot("run-1", 4, "RUNNING")),
      event(9, "RUN_COMPLETED"),
    );
    expect(reduceAgentEvent(withGap, event(10, "RUN_COMPLETED"))).toBe(withGap);
  });
});
