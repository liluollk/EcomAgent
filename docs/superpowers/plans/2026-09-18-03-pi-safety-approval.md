# Pi Runtime, PreToolUse, and Durable Approval Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run Pi Coding Agent in a supervised worker and implement the safe `allow / ask / deny` price-change workflow with deterministic crash recovery.

**Architecture:** The server sends idempotent commands to a separate Pi worker over JSON Lines. Pi's `tool_call` hook provides the pure PreToolUse precheck, while the tool executor performs the authoritative decision and transaction; ASK completes the model tool call with `approval_pending`, then an independent execution job continues after approval.

**Tech Stack:** `@earendil-works/pi-coding-agent` 0.80.6, `@earendil-works/pi-agent-core` 0.80.6, `@earendil-works/pi-ai` 0.80.6, TypeScript, Zod, SQLite, Vitest.

---

### Task 1: Add the Pi worker protocol and supervisor

**Files:**
- Create: `apps/agent-worker/package.json`
- Create: `apps/agent-worker/src/protocol.ts`
- Create: `apps/agent-worker/src/main.ts`
- Create: `apps/server/src/agent/AgentWorkerSupervisor.ts`
- Test: `tests/integration/agent-worker-protocol.test.ts`

- [ ] **Step 1: Write the failing idempotent command test**

```ts
it('returns the same result for a retried command id', async () => {
  const supervisor = await startTestWorker();
  const command = { commandId: 's-1:tc-1', type: 'run_turn', payload: { text: '查询商品' } };
  const first = await supervisor.send(command);
  const second = await supervisor.send(command);
  expect(second).toEqual(first);
  expect(workerExecutionCount('s-1:tc-1')).toBe(1);
});
```

- [ ] **Step 2: Verify failure**

Run: `npx vitest run tests/integration/agent-worker-protocol.test.ts`

Expected: FAIL because the worker does not exist.

- [ ] **Step 3: Implement framed JSONL RPC**

Define Zod-discriminated messages for `open_session`, `run_turn`, `cancel_turn`, `repair_session`, response, and event push. Reject malformed lines without terminating the process. The supervisor must bound request time, correlate by `commandId`, restart after exit, and retry only commands whose durable result is not already present.

- [ ] **Step 4: Verify crash and retry behavior**

Run: `npx vitest run tests/integration/agent-worker-protocol.test.ts`

Expected: PASS for duplicate commands, malformed frames, timeout, process exit, and restart.

- [ ] **Step 5: Commit**

```powershell
git add apps/agent-worker apps/server/src/agent tests/integration/agent-worker-protocol.test.ts
git commit -m "feat: supervise Pi agent worker"
```

### Task 2: Create real AgentSession wiring and a scripted provider

**Files:**
- Create: `apps/agent-worker/src/pi/createSession.ts`
- Create: `apps/agent-worker/src/pi/sessionStore.ts`
- Create: `apps/agent-worker/src/pi/eventMapper.ts`
- Create: `packages/evaluation/src/scriptedProvider.ts`
- Test: `apps/agent-worker/src/pi/createSession.test.ts`

- [ ] **Step 1: Write the failing Pi session test**

```ts
it('runs the real AgentSession loop with a scripted model response', async () => {
  const session = await createTestAgentSession([
    scriptedToolCall('lookup_product', { sku: 'sku-1' }),
    scriptedText('查询完成'),
  ]);
  await session.prompt('查询 sku-1');
  expect(recordedToolCalls()).toEqual([{ name: 'lookup_product', args: { sku: 'sku-1' } }]);
});
```

- [ ] **Step 2: Verify failure**

Run: `npx vitest run apps/agent-worker/src/pi/createSession.test.ts`

Expected: FAIL because Pi dependencies and session construction are absent.

- [ ] **Step 3: Implement session construction**

Depend directly on all three Pi packages at `0.80.6`. Use `createAgentSession()` with a file-backed Pi `SessionManager`, `noTools: 'all'`, only registry-generated `customTools`, and the exact registry allowlist. Map Pi messages, tool calls, tool results, and lifecycle events into shared events without treating Pi JSONL as business truth.

- [ ] **Step 4: Verify the loop and persistence**

Run: `npx vitest run apps/agent-worker/src/pi/createSession.test.ts`

Expected: PASS and a reopened Pi JSONL session preserves model context.

- [ ] **Step 5: Commit**

```powershell
git add apps/agent-worker/src/pi packages/evaluation
git commit -m "feat: integrate Pi AgentSession"
```

### Task 3: Build one registry for tools, hooks, and executors

**Files:**
- Create: `apps/agent-worker/src/tools/ToolRegistry.ts`
- Create: `apps/agent-worker/src/tools/definitions.ts`
- Create: `apps/agent-worker/src/tools/preToolUse.ts`
- Test: `apps/agent-worker/src/tools/ToolRegistry.test.ts`
- Test: `tests/architecture/tool-registry.test.ts`

- [ ] **Step 1: Write failing parity and unknown-tool tests**

```ts
it('keeps Pi tools, prechecks, and executors in exact parity', () => {
  expect(registry.piToolNames().sort()).toEqual(registry.precheckNames().sort());
  expect(registry.piToolNames().sort()).toEqual(registry.executorNames().sort());
});

it('denies an unknown tool before execution', async () => {
  expect(await preToolUse({ toolName: 'shell', args: {} })).toMatchObject({ decision: 'deny' });
});
```

- [ ] **Step 2: Verify failure**

Run: `npx vitest run apps/agent-worker/src/tools tests/architecture/tool-registry.test.ts`

Expected: FAIL because no registry exists.

- [ ] **Step 3: Implement the registry and PreToolUse Pipeline entry**

```ts
export type RegisteredTool<A, R> = {
  name: string;
  schema: z.ZodType<A>;
  precheck: (context: ToolContext, args: Readonly<A>) => Promise<PrecheckResult>;
  execute: (context: ToolContext, args: A) => Promise<R>;
};
```

Pi's `tool_call` hook must parse and deep-freeze arguments, emit the precheck audit event, and block only `deny`. Both `allow` and `ask` enter the registered executor, which reloads current state and makes the authoritative decision. Unknown names, schema failures, and registry mismatch are startup failures or denials.

- [ ] **Step 4: Verify mutation resistance and parity**

Run: `npx vitest run apps/agent-worker/src/tools tests/architecture/tool-registry.test.ts`

Expected: PASS, including a test that mutates raw arguments after the hook and cannot change executed values.

- [ ] **Step 5: Commit**

```powershell
git add apps/agent-worker/src/tools tests/architecture/tool-registry.test.ts
git commit -m "feat: add PreToolUse tool registry"
```

### Task 4: Implement the versioned three-state rule engine

**Files:**
- Create: `packages/safety/package.json`
- Create: `packages/safety/src/types.ts`
- Create: `packages/safety/src/evaluate.ts`
- Create: `packages/safety/src/rules/*.ts`
- Create: `packages/safety/src/ruleRepository.ts`
- Test: `packages/safety/src/evaluate.test.ts`

- [ ] **Step 1: Write the failing decision table**

```ts
it.each([
  ['lookup_product', 'operator', 'allow'],
  ['request_price_change', 'operator', 'ask'],
  ['request_price_change', 'approver', 'deny'],
  ['unknown_tool', 'admin', 'deny'],
])('%s for %s becomes %s', async (tool, role, expected) => {
  expect((await evaluate(context({ tool, role }))).decision).toBe(expected);
});
```

- [ ] **Step 2: Verify failure**

Run: `npx vitest run packages/safety/src/evaluate.test.ts`

Expected: FAIL because the safety package is absent.

- [ ] **Step 3: Implement deterministic rule composition**

Evaluate RBAC, platform capability, price floor, maximum change percentage, campaign stacking, prompt-injection flags, and write authorization. `deny` dominates `ask`, which dominates `allow`; return rule id, version, reason codes, and the canonical input hash. Persist immutable published rule versions and snapshot the selected version on every intent.

- [ ] **Step 4: Run table, version, and hash tests**

Run: `npx vitest run packages/safety`

Expected: PASS for order independence, stable hashes, boundary prices, campaign conflicts, and old-version replay.

- [ ] **Step 5: Commit**

```powershell
git add packages/safety
git commit -m "feat: add versioned three-state rules"
```

### Task 5: Implement the two-stage ASK transaction

**Files:**
- Create: `apps/agent-worker/src/tools/requestPriceChange.ts`
- Create: `apps/server/src/intents/IntentService.ts`
- Create: `apps/server/src/approvals/ApprovalService.ts`
- Create: `apps/server/src/routes/approvals.ts`
- Test: `tests/integration/approval-pending.test.ts`
- Test: `tests/integration/approval-idempotency.test.ts`

- [ ] **Step 1: Write the failing contract tests**

```ts
it('completes the Pi tool call with approval_pending and no price write', async () => {
  const result = await runRequestPriceChange({ sku: 'sku-1', priceMinor: 8800, currency: 'CNY' });
  expect(result).toMatchObject({ status: 'approval_pending', intentId: expect.any(String), approvalId: expect.any(String) });
  expect(platformApplyCount()).toBe(0);
});

it('deduplicates retries by appSessionId plus toolCallId', async () => {
  const a = await invokeWithToolCallId('tc-1');
  const b = await invokeWithToolCallId('tc-1');
  expect(b).toEqual(a);
  expect(intentCount()).toBe(1);
});
```

- [ ] **Step 2: Verify failure**

Run: `npx vitest run tests/integration/approval-pending.test.ts tests/integration/approval-idempotency.test.ts`

Expected: FAIL because intent and approval services are missing.

- [ ] **Step 3: Implement authoritative execution and approval creation**

Derive `commandId = appSessionId + ':' + toolCallId`. Inside one immediate transaction, reload product/rules/role, decide, create or reuse the intent, create ASK approval, append events, and commit. Return one of:

```ts
type PriceChangeToolResult =
  | { status: 'proposed'; intentId: string }
  | { status: 'approval_pending'; intentId: string; approvalId: string }
  | { status: 'denied'; reasonCodes: string[] };
```

Approval decisions require approver/admin, forbid the original requester from approving by default, are compare-and-set from `pending`, store actor/time/comment, and atomically create exactly one execution job when approved. Expired, revoked, rule-invalidated, or product-version-invalid approvals cannot create a job. They never reopen the original JavaScript Promise.

- [ ] **Step 4: Verify all three states and races**

Run: `npx vitest run tests/integration/approval-*.test.ts`

Expected: PASS for allow/propose, ask, deny, duplicate tool delivery, simultaneous approvers, rejection, and expired approval.

- [ ] **Step 5: Commit**

```powershell
git add apps/agent-worker/src/tools/requestPriceChange.ts apps/server/src/intents apps/server/src/approvals apps/server/src/routes/approvals.ts tests/integration/approval-*.test.ts
git commit -m "feat: add durable ASK approval workflow"
```

### Task 6: Repair unmatched Pi tool calls after a crash

**Files:**
- Create: `apps/agent-worker/src/pi/repairSession.ts`
- Modify: `apps/agent-worker/src/pi/createSession.ts`
- Test: `tests/integration/pi-crash-repair.test.ts`

- [ ] **Step 1: Write the exact crash-window test**

```ts
it('repairs a crash after approval commit and before Pi tool result append', async () => {
  await crashAfterApprovalTransaction({ appSessionId: 's-1', toolCallId: 'tc-9' });
  expect(databaseIntent('s-1:tc-9')).toMatchObject({ status: 'awaiting_approval' });
  await restartWorkerAndOpen('s-1');
  expect(piToolResults('tc-9')).toHaveLength(1);
  expect(piToolResults('tc-9')[0]).toMatchObject({ status: 'approval_pending' });
  expect(intentCountForCommand('s-1:tc-9')).toBe(1);
});
```

- [ ] **Step 2: Verify failure**

Run: `npx vitest run tests/integration/pi-crash-repair.test.ts`

Expected: FAIL because unmatched calls are not repaired.

- [ ] **Step 3: Implement repair-before-create ordering**

On open, call `SessionManager.open`, scan unmatched assistant Tool Calls, derive each command id, query durable command results, and append the matching tool result with the original `toolCallId` via `SessionManager.appendMessage`. Only after repair completes may code construct/continue `AgentSession`. If durable state is absent, replay the idempotent executor; never invent success.

- [ ] **Step 4: Run the crash matrix**

Run: `npx vitest run tests/integration/pi-crash-repair.test.ts`

Expected: PASS for crash before transaction, after commit/before result, after result, repeated restart, and missing durable result.

- [ ] **Step 5: Commit**

```powershell
git add apps/agent-worker/src/pi tests/integration/pi-crash-repair.test.ts
git commit -m "feat: repair durable Pi tool results"
```

### Task 7: Add staged context assembly and explicit compaction policy

**Files:**
- Create: `apps/agent-worker/src/context/stages.ts`
- Create: `apps/agent-worker/src/context/assembleContext.ts`
- Create: `apps/agent-worker/src/context/compactAtBoundary.ts`
- Test: `apps/agent-worker/src/context/assembleContext.test.ts`

- [ ] **Step 1: Write the failing stage test**

```ts
it.each(['perceive', 'locate', 'compare', 'decide', 'execute_verify'] as const)('assembles only %s context', async (stage) => {
  const context = await assembleContext(fixture, stage);
  expect(context.sections.map((section) => section.stage)).toEqual([stage]);
  expect(context.ruleVersion).toBe('rules-v3');
});
```

- [ ] **Step 2: Verify failure**

Run: `npx vitest run apps/agent-worker/src/context/assembleContext.test.ts`

Expected: FAIL because stage assembly is absent.

- [ ] **Step 3: Implement stage-specific prompts**

Define the five stages with explicit allowed tools and context selectors. Record stage transitions and token estimates as events. Disable reliance on automatic mid-turn compaction; request compaction only between completed turns and retain intent ids, rule version, approval status, and unresolved tool calls.

- [ ] **Step 4: Verify boundaries and retained facts**

Run: `npx vitest run apps/agent-worker/src/context`

Expected: PASS for tool availability, deterministic section order, cross-workspace exclusion, and compaction retention.

- [ ] **Step 5: Commit**

```powershell
git add apps/agent-worker/src/context
git commit -m "feat: add staged agent context"
```

### Task 8: Configure Pi AI models through browser settings

**Files:**
- Create: `apps/server/src/models/ModelSettingsService.ts`
- Create: `apps/server/src/routes/modelSettings.ts`
- Create: `apps/agent-worker/src/pi/modelResolver.ts`
- Modify: `apps/web/src/pages/SettingsPage.tsx`
- Test: `tests/integration/model-settings.test.ts`
- Test: `tests/web/model-settings.spec.ts`

- [ ] **Step 1: Write failing model-resolution and browser tests**

```ts
it('resolves the saved provider and model through pi-ai without exposing its key', async () => {
  await settings.save('w-1', { provider: 'openai', modelId: 'test-model', apiKey: 'key-secret' });
  expect(await resolver.resolve('w-1')).toMatchObject({ provider: 'openai', modelId: 'test-model' });
  expect(JSON.stringify(await settings.publicView('w-1'))).not.toContain('key-secret');
});
```

The Playwright test must save settings as admin, test the connection with a fake Pi AI provider, reload the page, and observe only a masked credential.

- [ ] **Step 2: Verify failure**

Run: `npx vitest run tests/integration/model-settings.test.ts && npm run test:web -- tests/web/model-settings.spec.ts`

Expected: FAIL because model configuration is not implemented.

- [ ] **Step 3: Implement encrypted model settings and Pi resolution**

Store provider, model id, non-secret options, and encrypted API key per workspace. Validate the provider/model using `pi-ai`, allow only admin mutations, send credentials only to the worker process for session creation, and redact them from HTTP, events, errors, and logs. A failed connection test must not replace the currently active settings.

- [ ] **Step 4: Verify settings and worker use**

Run: `npx vitest run tests/integration/model-settings.test.ts && npm run test:web -- tests/web/model-settings.spec.ts`

Expected: PASS for save, test, failed test rollback, masked reload, non-admin denial, and AgentSession construction with the selected model.

- [ ] **Step 5: Commit**

```powershell
git add apps/server/src/models apps/server/src/routes/modelSettings.ts apps/agent-worker/src/pi/modelResolver.ts apps/web/src/pages/SettingsPage.tsx tests/integration/model-settings.test.ts tests/web/model-settings.spec.ts
git commit -m "feat: add Pi AI model settings"
```

**Plan 3 exit gate:** the real Pi loop, registry parity, tri-state decisions, two-stage approval, duplicate command handling, exact crash repair, staged-context tests, and model settings all pass.
