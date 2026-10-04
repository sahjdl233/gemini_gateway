# ADR-CONFIG-R4 — Definition / Credential / Runtime State Persistence Boundary

Status: accepted（边界冻结；本 ADR 只文档化 + 补 contract tests，不改 runtime 逻辑，无数据库依赖）
Baseline: `107cb09`（CONFIG/R-3 之后）
来源: TASK-CONFIG/R-4-ADR（CONTROL-004..007 生命周期链的边界正式化）
契约测试: `tests/core/test_persistence_boundary_contract.py`

---

## 0. 问题

代码隐含三层持久化语义：

```
ResourceDefinition（durable）
        │
        ├── credential_id  ──→  Credential（encrypted material）
        │
        └── runtime Resource（memory）
                ├── health / cooldown
                ├── counters（total_requests / total_failures / consecutive_failures）
                └── in_flight / scheduler state
```

三层的字段归属、生命周期与失效语义已在一系列任务中逐步定型
（DB-RESOURCE-001、AUTH-002/013/014、TASK-STATE-001、CONTROL-006/007），
但从未被单一文档正式化。本 ADR 冻结边界，并以 DTO 层契约测试防止回退。

## 1. Resource Definition（durable 层）

**载体**：`resource_definitions` 表（postgres）/ `MemoryResourceRepository`
（memory）/ legacy YAML（Part C）。

**允许保存**：

| 字段 | 说明 |
|---|---|
| `provider` | 复合身份的一半 |
| `resource id` | 复合身份的另一半 |
| `enabled` | 定义级开关 |
| `credential_id` | **引用**，不是材料 |
| provider-specific non-secret config | 各 provider DTO 白名单字段（如 project_id、tier） |

**禁止保存**（DTO 层 `extra="forbid"` 直接拒绝）：

* access token / refresh token / API key / 任何 secret material；
* runtime health、cooldown、counters、in_flight 等任何调度状态。

## 2. Credential Material（durable 层）

**载体**：`CredentialRepository`（memory / postgres + CredentialEncryptor）。

**负责**：加密后的 secret material——OAuth token、refresh token、API key、
provider 认证状态。

**规则（AUTH-002/013/014 的正式化）**：

1. `ResourceDefinition` 只能保存 `credential_id` 引用（松引用，无 FK）；
2. DTO 层**永不** resolve credential——definition 世界里不存在解密；
3. runtime Resource 获取 material 必须经 credential store
   （`require_bound_credential` 请求路径 fail-closed，绝不回退 legacy 字段）；
4. 轮换（AUTH-014）只写 credential 的 `refresh_token`，永不触碰
   definition；轮换产物在成为 runtime 状态前先持久化。

## 3. Resource Runtime State（memory 层）

**载体**：`core.resource.Resource` 对象 + `InMemoryPool`。

**负责**：health（HealthState）、failure counters、consecutive failures、
`cooldown_until`、in_flight、未来可能加入的 latency statistics 与
scheduler 内部状态。

**规则（TASK-STATE-001 / CONTROL-007-DECISION-001 的正式化）**：

1. **可丢弃、可重建**——重启即消失，从 definition 全量重建；
2. **不参与 resource identity**——identity 恒为 `(provider, id)`，
   任何状态变更不改变 `resource_key`；
3. 按归属分两类搬运（见 CONTROL-007-RUNTIME-STATE-POLICY.md）：
   观测计数器跟随 resource id；调度状态跟随 credential 身份
   （credential rebind 时重置）；
4. 永不写回 definition / repository。

## 4. 生命周期

**启动**（CONFIG/R-3：单一 reconciliation 边界）：

```
repository
    |
    v
definition (DTO)
    |
    v  RuntimeReconciliationService + registry_runtime_builder
runtime Resource（state 全新）
    |
    v
pools / scheduler / ModelRegistry（lazy discovery）
```

**运行时**（请求路径，runtime state 的唯一写者）：

```
request → scheduler.acquire → provider.complete
    → pool.record_success / record_failure / record_rate_limit
    → runtime state update（cooldown / counters / health）
```

**管理操作**（definition 的唯一写者）：

```
Admin mutation（ResourceManager，_repository_lock 串行化）
    |
    v  校验（DTO 白名单 + credential 存在性，AUTH-016）
definition update（repository）
    |
    v  reconcile 成功
runtime Resource 替换（state 按 key 搬运/重置）
    |
    v
adapter invalidation + ModelRegistry.invalidate()（CONFIG/R-2-C）
```

## 5. 契约测试与 guard

`tests/core/test_persistence_boundary_contract.py`：

* definition 拒绝 secret 字段（Test 1）、拒绝 runtime 字段（Test 2）；
* `credential_id` 引用合法且不被 resolve（Test 3）；
* identity = `(provider, id)`，与 runtime state 无关（Test 4）;
* `to_runtime_definition()` 不携带任何 runtime 状态（Test 5）；
* guard：`core/resource_definition.py` 内不得出现 runtime/secret 字段
  注解（模块级扫描，不做全仓字符串搜索）。

## 6. 边界之外

* bootstrap import 的悬空 `credential_id` 保持 warn-only（ADR-002 §5）；
* Admin 写路径的存在性校验（AUTH-016）已闭合引用完整性；
* legacy YAML 路径（Part C）保留其明文 legacy 字段（AUTH-010 兼容，
  见 roadmap 冻结决策），不在本 ADR 范围内收窄。
