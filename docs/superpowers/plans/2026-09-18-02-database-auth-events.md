# Database, Authentication, and Events Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the durable SQLite state layer, local role-based authentication, credential encryption, and resumable event transport used by every later subsystem.

**Architecture:** SQLite mutable tables are the business source of truth, while `agent_events` is an append-only audit/outbox written in the same transaction. Express exposes cookie-authenticated APIs and a WebSocket event stream; browser clients resume from a monotonic workspace sequence after reconnecting.

**Tech Stack:** TypeScript 5.9, Express 5.2.1, better-sqlite3 13.0.3, Argon2id via argon2 0.45.1, Node crypto, ws 8.21.3, Zod 4.6.5, Vitest.

---

### Task 1: Create the SQLite schema and migration runner

**Files:**
- Create: `packages/database/package.json`
- Create: `packages/database/src/connection.ts`
- Create: `packages/database/src/migrate.ts`
- Create: `packages/database/src/transaction.ts`
- Create: `packages/database/src/migrations/001_core.sql`
- Create: `packages/database/src/index.ts`
- Test: `packages/database/src/migrate.test.ts`

- [ ] **Step 1: Write the failing migration test**

```ts
it('enables WAL and enforces the command id uniqueness boundary', () => {
  const db = openTestDatabase();
  migrate(db);
  expect(db.pragma('journal_mode', { simple: true })).toBe('wal');
  db.prepare(`INSERT INTO price_intents
    (id, workspace_id, app_session_id, command_id, sku, proposed_price_minor, currency, status)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)`)
    .run('i-1', 'w-1', 's-1', 'cmd-1', 'sku-1', 999, 'CNY', 'pending');
  expect(() => db.prepare(`INSERT INTO price_intents
    (id, workspace_id, app_session_id, command_id, sku, proposed_price_minor, currency, status)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)`)
    .run('i-2', 'w-1', 's-1', 'cmd-1', 'sku-1', 999, 'CNY', 'pending'))
    .toThrow(/UNIQUE/);
});
```

- [ ] **Step 2: Run the test and verify failure**

Run: `npx vitest run packages/database/src/migrate.test.ts`

Expected: FAIL because the database package is missing.

- [ ] **Step 3: Add connection and migration code**

`openDatabase(path)` must set `journal_mode=WAL`, `foreign_keys=ON`, `busy_timeout=5000`, and `synchronous=NORMAL`. `001_core.sql` must create `schema_migrations`, `users`, `login_sessions`, `workspaces`, `memberships`, `agent_sessions`, `agent_events`, `price_intents`, `approvals`, `execution_jobs`, `execution_attempts`, `idempotency_records`, `rule_versions`, `model_settings`, `skills`, `sources`, `mcp_servers`, `automations`, `automation_runs`, and `evaluation_runs`. Add `UNIQUE(workspace_id, command_id)`, `UNIQUE(workspace_id, operation_id)`, `UNIQUE(intent_id)` on execution jobs, and `UNIQUE(workspace_id, sequence)` constraints.

```ts
export function inTransaction<T>(db: Database, action: () => T): T {
  return db.transaction(action).immediate();
}
```

- [ ] **Step 4: Verify schema behavior**

Run: `npx vitest run packages/database && npm --workspace @ecomagent/database run typecheck`

Expected: PASS, including foreign-key, rollback, migration-idempotence, and unique-command tests.

- [ ] **Step 5: Commit**

```powershell
git add packages/database
git commit -m "feat: add durable SQLite schema"
```

### Task 2: Implement local login, cookie sessions, CSRF, and RBAC

**Files:**
- Create: `apps/server/package.json`
- Create: `apps/server/src/app.ts`
- Create: `apps/server/src/server.ts`
- Create: `apps/server/src/auth/password.ts`
- Create: `apps/server/src/auth/session.ts`
- Create: `apps/server/src/auth/csrf.ts`
- Create: `apps/server/src/auth/authorize.ts`
- Create: `apps/server/src/routes/auth.ts`
- Create: `apps/server/src/routes/workspaces.ts`
- Test: `tests/integration/auth.test.ts`

- [ ] **Step 1: Write failing authorization tests**

```ts
it.each([
  ['operator', '/api/approvals/a-1/approve', 403],
  ['approver', '/api/approvals/a-1/approve', 404],
  ['operator', '/api/workspaces/w-1/sessions', 200],
])('%s receives the expected status', async (role, path, status) => {
  const cookie = await loginAs(role);
  const response = await request(app).post(path)
    .set('Cookie', cookie).set('Origin', origin).set('x-csrf-token', csrf(cookie));
  expect(response.status).toBe(status);
});
```

- [ ] **Step 2: Verify tests fail**

Run: `npx vitest run tests/integration/auth.test.ts`

Expected: FAIL because the HTTP server and auth modules are missing.

- [ ] **Step 3: Implement the auth boundary**

Hash passwords with Argon2id. Store only a random session-token digest, use `HttpOnly`, `SameSite=Lax`, `Secure` in production, rotate the CSRF token on login, validate `Origin` for HTTP and WebSocket handshakes, and enforce workspace membership plus role.

```ts
export const permissions = {
  operator: new Set(['session:read', 'session:run', 'intent:propose']),
  approver: new Set(['session:read', 'approval:decide']),
  admin: new Set(['session:read', 'session:run', 'intent:propose', 'approval:decide', 'system:manage']),
} as const;
```

Add an `init-admin` CLI that creates the first admin only when no user exists and reads the initial password from stdin, never an argument or log line.

- [ ] **Step 4: Run integration tests**

Run: `npx vitest run tests/integration/auth.test.ts`

Expected: PASS for valid login, invalid password, expired session, missing CSRF, foreign origin, workspace isolation, and all three roles.

- [ ] **Step 5: Commit**

```powershell
git add apps/server tests/integration/auth.test.ts
git commit -m "feat: add local authentication and RBAC"
```

### Task 3: Encrypt credentials and redact secrets

**Files:**
- Create: `apps/server/src/security/credentialCipher.ts`
- Create: `apps/server/src/security/redact.ts`
- Test: `apps/server/src/security/credentialCipher.test.ts`
- Test: `apps/server/src/security/redact.test.ts`

- [ ] **Step 1: Write failing crypto tests**

```ts
it('binds ciphertext to workspace and record identity', () => {
  const cipher = createCredentialCipher(Buffer.alloc(32, 7));
  const sealed = cipher.encrypt('secret', { workspaceId: 'w-1', recordId: 'src-1' });
  expect(cipher.decrypt(sealed, { workspaceId: 'w-1', recordId: 'src-1' })).toBe('secret');
  expect(() => cipher.decrypt(sealed, { workspaceId: 'w-2', recordId: 'src-1' })).toThrow();
});
```

- [ ] **Step 2: Verify tests fail**

Run: `npx vitest run apps/server/src/security`

Expected: FAIL because the modules are missing.

- [ ] **Step 3: Implement AES-256-GCM and recursive redaction**

Read a 32-byte base64 master key from `ECOMAGENT_MASTER_KEY`; fail startup if absent outside tests. Store `{version, iv, ciphertext, tag}` and use `workspaceId:recordId` as additional authenticated data. Redact keys matching `authorization`, `cookie`, `password`, `secret`, `token`, and `apiKey` before logs or events.

- [ ] **Step 4: Verify tamper and leakage tests**

Run: `npx vitest run apps/server/src/security`

Expected: PASS for round trip, wrong context, tampered tag, missing key, and nested-object redaction.

- [ ] **Step 5: Commit**

```powershell
git add apps/server/src/security
git commit -m "feat: protect stored credentials"
```

### Task 4: Add transactional event outbox and resumable WebSocket delivery

**Files:**
- Create: `packages/database/src/eventRepository.ts`
- Create: `apps/server/src/events/EventPublisher.ts`
- Create: `apps/server/src/events/websocket.ts`
- Test: `tests/integration/event-outbox.test.ts`
- Test: `tests/integration/event-resume.test.ts`

- [ ] **Step 1: Write the failing atomicity and resume tests**

```ts
it('rolls business state and its event back together', () => {
  expect(() => createIntentAndEvent(db, invalidIntent)).toThrow();
  expect(count(db, 'price_intents')).toBe(0);
  expect(count(db, 'agent_events')).toBe(0);
});

it('resumes strictly after the acknowledged sequence', async () => {
  await appendEvents(1, 2, 3);
  const received = await connectEvents({ workspaceId: 'w-1', after: 1 });
  expect(received.map((event) => event.sequence)).toEqual([2, 3]);
});
```

- [ ] **Step 2: Verify tests fail**

Run: `npx vitest run tests/integration/event-outbox.test.ts tests/integration/event-resume.test.ts`

Expected: FAIL because the repository and WebSocket endpoint are missing.

- [ ] **Step 3: Implement append and catch-up delivery**

Allocate workspace sequence numbers inside the same immediate transaction as the state mutation. `GET /api/workspaces/:id/events?after=N` and `WS /api/events?workspaceId=...&after=N` must first emit durable catch-up rows ordered by sequence, then live notifications. Clients deduplicate by event id and sequence.

- [ ] **Step 4: Verify reconnect and ordering**

Run: `npx vitest run tests/integration/event-outbox.test.ts tests/integration/event-resume.test.ts`

Expected: PASS with no lost, duplicated, cross-workspace, or out-of-order event.

- [ ] **Step 5: Commit**

```powershell
git add packages/database/src/eventRepository.ts apps/server/src/events tests/integration/event-*.test.ts
git commit -m "feat: add transactional event delivery"
```

### Task 5: Connect the browser to the authenticated server

**Files:**
- Modify: `apps/web/src/api/WebApiClient.ts`
- Create: `apps/web/src/auth/AuthProvider.tsx`
- Create: `apps/web/src/pages/LoginPage.tsx`
- Modify: `apps/web/src/App.tsx`
- Test: `tests/web/auth.spec.ts`

- [ ] **Step 1: Write the failing browser test**

```ts
test('operator logs in and reconnects to workspace events', async ({ page }) => {
  await page.goto('/login');
  await page.getByLabel('用户名').fill('operator');
  await page.getByLabel('密码').fill('operator-pass');
  await page.getByRole('button', { name: '登录' }).click();
  await expect(page).toHaveURL(/workbench/);
  await expect(page.getByText('operator')).toBeVisible();
});
```

- [ ] **Step 2: Verify failure**

Run: `npm run test:web -- tests/web/auth.spec.ts`

Expected: FAIL because the shell still uses its in-memory client.

- [ ] **Step 3: Implement the HTTP/WebSocket client**

Use `credentials: 'include'`, send the CSRF header on mutations, persist only the last acknowledged event sequence in browser storage, reconnect with bounded exponential backoff, and fetch `/api/auth/me` at startup. Do not store the session cookie or credentials in JavaScript storage.

- [ ] **Step 4: Run the end-to-end auth flow**

Run: `npm run test:web -- tests/web/auth.spec.ts && npm run verify:boundaries`

Expected: PASS, including operator/approver/admin navigation visibility and a forced WebSocket reconnect.

- [ ] **Step 5: Commit**

```powershell
git add apps/web tests/web/auth.spec.ts
git commit -m "feat: connect browser authentication and events"
```

**Plan 2 exit gate:** migrations, auth, credential protection, transactional events, reconnect tests, web auth flow, and typecheck all pass.
