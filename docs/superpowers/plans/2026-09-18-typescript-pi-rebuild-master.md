# EcomAgent TypeScript + Pi Runtime Rebuild Master Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Python/Electron-era repository with a browser-only TypeScript monorepo powered by Pi Coding Agent, with durable approvals, safe price execution, extensible Skills/Sources/MCP, and a versioned benchmark.

**Architecture:** A browser app talks to one HTTP/WebSocket server. The server owns workflow state in SQLite and supervises an isolated Pi worker; all price side effects pass through one executor, while the platform simulator provides deterministic protocol and fault behavior. Implementation is split into five independently verifiable plans so each checkpoint leaves runnable software.

**Tech Stack:** TypeScript 5.9, Node.js 22.19+, npm workspaces, React 18, Vite 6, Express 5, ws, better-sqlite3, Argon2id, Pi Coding Agent 0.80.6, Vitest 4, Playwright 1.63.

---

## Execution rules

- Execute in an isolated worktree created with `using-git-worktrees`; suggested branch: `codex/typescript-pi-rebuild`.
- Do not stage or alter the user's current dirty files on `main`.
- Use TDD for every behavior change: failing test, minimal implementation, passing test, focused commit.
- Keep the supplied Craft archive immutable. Record source version `0.11.4` and SHA-256 `BA48C941238E5FCF6EF6412C7FD53B30C863220EA81395E368013C38EB054871`.
- Do not delete Python, the old frontend, or `desktop/` until the new stack passes the cutover gate in Plan 5.
- Never claim production platform access. Resume numbers come only from a committed benchmark report.

## Target file map

```text
apps/
├─ web/                         browser-only React app
├─ server/                      HTTP, WebSocket, auth, workflow APIs
└─ agent-worker/                Pi AgentSession process
packages/
├─ shared/                      Zod contracts and shared types
├─ database/                    migrations and repositories
├─ safety/                      rules, RBAC, approvals
├─ commerce/                    domain and platform adapters
├─ execution/                   authorized side effects and recovery
├─ skills/                      Skill registry
├─ sources/                     Source registry and encrypted credentials
├─ mcp/                         MCP lifecycle and tool discovery
├─ automations/                 scheduled tasks
├─ evaluation/                  cases, runner, metrics, reports
├─ platform-simulator/          three protocol shapes and fault injection
└─ ui/                          browser-safe Craft-derived components
tests/
├─ architecture/
├─ integration/
├─ web/
└─ benchmark/
```

## Ordered plans

1. [Foundation and browser shell](./2026-09-18-01-foundation-browser-shell.md)
2. [Database, authentication, and events](./2026-09-18-02-database-auth-events.md)
3. [Pi runtime, PreToolUse, and durable approval](./2026-09-18-03-pi-safety-approval.md)
4. [Commerce execution and extension systems](./2026-09-18-04-commerce-extensions.md)
5. [Benchmark, browser verification, and cutover](./2026-09-18-05-benchmark-cutover.md)

Do not start a later plan until the previous plan's exit gate passes and its commits are reviewed.

## Milestone gates

| Milestone | Required evidence |
| --- | --- |
| M1 Browser shell | `npm run build:web` and architecture import scan pass |
| M2 State server | login, RBAC, event outbox, reconnect integration tests pass |
| M3 Pi safety | scripted Pi session, three-state decisions, two-stage ASK and crash repair tests pass |
| M4 Closed loop | three adapters, authorized executor, Skills/Sources/MCP/Automations tests pass |
| M5 Cutover | benchmark release gates, Playwright flow, no Python/Electron scan, clean build pass |

## Final verification

Run from repository root:

```powershell
npm ci
npm run typecheck
npm test
npm run test:integration
npm run test:web
npm run benchmark
npm run build
npm run verify:boundaries
```

Expected: every command exits `0`; the three-state candidate has zero unauthorized side-effect escapes and zero duplicate price changes; the boundary scan finds no project Python or Electron dependency.
