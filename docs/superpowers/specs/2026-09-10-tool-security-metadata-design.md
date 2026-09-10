# Tool Security Metadata Design

## Goal

Make tool permission decisions depend on explicit security metadata rather than tool-name prefixes. Unknown tools, including MCP tools without metadata, must default to the safer write/approval-required behavior.

## Scope

- Add a security policy for each built-in tool.
- Make `PreToolUse` mode decisions consume that policy.
- Treat tools without a declared policy as side-effecting/approval-required.
- Preserve the existing built-in-tool precedence when merging MCP tools.
- Add regression tests for built-in, unknown, MCP, and permission-mode behavior.

Out of scope: changing CommerceProvider, CommerceAdapter, MCP transport, business handlers, or the user-facing permission card.

## Policy model

Each tool has a small internal policy, not part of the model-facing function schema:

- `side_effect`: `read` or `write`.
- `requires_approval`: whether ASK mode must wait for a human decision.

Built-in read tools explicitly declare `read` and do not require approval. Built-in mutating tools explicitly declare `write` and require approval. An absent policy resolves to `write` plus approval required.

## Runtime flow

```text
model tool call
  -> resolve tool security policy
  -> missing policy becomes write/approval-required
  -> RBAC and business-rule checks
  -> mode gate
  -> ALLOW, BLOCK, or ASK
```

The permission layer must not infer safety from names such as `query_`, `get_`, or `list_`.

## Compatibility and safety

- Existing built-in tools retain their current intended behavior through explicit metadata.
- Unknown MCP tools may require approval even when they are read-only; this is an intentional conservative default.
- Duplicate tool names continue to prefer built-in handlers. The change does not alter source merge behavior.
- The metadata remains internal so OpenAI/Anthropic tool schemas stay valid and unchanged.

## Verification

Tests must prove:

1. Explicit read tools are allowed in ASK mode.
2. Explicit write tools produce ASK in ASK mode.
3. Unknown tools produce ASK in ASK mode, regardless of name prefix.
4. READONLY blocks explicit and unknown write tools.
5. EXECUTE allows tools unless another rule blocks them.
6. The built-in tool policy map covers every built-in handler.

