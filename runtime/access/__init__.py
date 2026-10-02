"""Workspace 身份边界:密码哈希、JWT、ActorContext 与授权(计划 01 Task 5)。

本包实现架构文档《身份与 Workspace 权限设计》中的本地身份语义:
- 密码仅保存 argon2 强哈希;
- JWT 只携带 sub/iat/exp/jti,角色、权限与资源范围每次从 PostgreSQL 当前
  状态构建为不可覆盖的 ActorContext;
- 授权结果 = 成员角色 ∩ 资源数据范围 ∩ 动作权限。
"""
