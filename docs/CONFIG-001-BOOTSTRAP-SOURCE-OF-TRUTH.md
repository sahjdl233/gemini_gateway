# CONFIG-001-BOOTSTRAP-SOURCE-OF-TRUTH — Bootstrap Source-of-Truth 策略 (ADR)

Status: **accepted**（规则冻结，本 ADR 不要求任何代码变化）
Baseline: `146dbfc` + CONTROL-005/006/007 生命周期链
审计来源: TASK-CONFIG-001（bootstrap overwrite 安全审计）
后续任务（本 ADR 之外，另行拆分）: 落地 §2 的 mode 校验收紧、§4 的冲突可见性升级

---

## 0. 背景

Bootstrap 数据流：

```
YAML (config.yaml providers.*.resources)
   |
   | seed / import   (ResourceBootstrapService: check | import | overwrite)
   v
ResourceRepository  (memory | postgres)   ← definition source of truth
   |
   | runtime reconcile (RuntimeReconciliationService → pool.reconcile_resources)
   v
Pool (runtime projection)
```

TASK-CONFIG-001 审计确认了三个事实：

1. `mode: overwrite` 是 `config.yaml` 里的合法**常驻启动配置**；
2. repository 路径的 Admin CRUD 从不回写 YAML（DB-RESOURCE-013 Part A），
   因此 YAML seed 必然随时间过时；
3. 两者叠加：任何一次以 `overwrite` 启动的重启，都会把过时 YAML
   静默覆盖较新的 DB（仅 postgres 后端有持久状态可破坏），
   无确认门槛、无逐键记录、只有 INFO 级计数日志。

本 ADR 冻结角色边界与 mode 生命周期，使上述组合**不再合法**。

## 1. Source of truth（冻结）

| 层 | 角色 | 冻结语义 |
|---|---|---|
| YAML (`config.yaml`) | **seed** | 一次性导入源；不是同步源、不是备份、不是第二真相。允许过时，过时本身不是错误。 |
| ResourceRepository (postgres；memory 为其退化形态) | **definition source of truth** | Admin CRUD 的唯一写入口；bootstrap 的唯一写目标；runtime reconcile 的唯一读源。 |
| Pool | **runtime projection** | 由 reconcile 从 repository 投影而来；永不反向写 definition。 |

**禁止**：

```
Admin CRUD ──→ YAML   (禁止回写)
```

理由：

* **双写破坏单入口**。定义写入的校验边界是 strict DTO parse +
  repository（DB-RESOURCE-001-2）；YAML 回写会制造第二个不受该边界
  约束的写路径。
* **绕过 CONTROL-005 锁边界**。repository mutation 已被
  `_repository_lock` 串行化；YAML 文件写不在任何锁的保护范围内，
  回写会重新引入 CONTROL-005 审计过的并发竞态。
* **drift 无法判断来源**。一旦两边都可写，conflict 的
  existing/incoming 谁更新就无从判定；单写方（DB）+ 只读 seed
  （YAML）使 conflict 语义天然明确：incoming 一定是陈旧的。
* YAML 中存在 `${ENV_VAR}` 凭据展开与 legacy secret 字段（AUTH-010
  兼容），回写会把展开后的值或敏感字段带进文件——独立的安全问题。

存量导出需求由**显式一次性命令**满足（`resource export-yaml`，规划中），
不属于 Admin CRUD 的隐式行为。

## 2. Bootstrap mode 生命周期（冻结）

**长期启动配置（`resource_bootstrap.mode`）允许**：

| mode | 语义 | 启动时行为 |
|---|---|---|
| `check` | 只生成 plan，不写 sink | 允许。用于演练与漂移巡检。 |
| `import` | 插入 sink 缺失的定义；冲突只报告不覆盖 | 允许。推荐的生产默认。 |

**禁止作为启动配置**：

| mode | 理由 |
|---|---|
| `overwrite` | overwrite 是 **mutation operation，不是 lifecycle policy**。它把"每次进程启动"变成一次对 DB 的破坏性写授权，违背"启动应是幂等收敛、不是覆盖"的原则；且无人值守重启即可触发（TASK-CONFIG-001 风险 R-A）。 |

`resource_bootstrap_settings` 的校验集收紧（启动 mode 合法集
`check`/`import`，错误信息引导使用一次性命令）由后续 FIX 任务落地；
本 ADR 先行冻结规则。

## 3. overwrite 的定位（冻结）

overwrite 被重定义为**一次性迁移工具**，形态为显式命令：

```
resource import-yaml --overwrite
```

要求：

* **人工执行**——不出现在任何常驻配置、不随启动自动运行；
* **输出 diff**——逐键 canonical diff（existing vs incoming payload），
  执行前可预览（`--dry-run` / `check`），执行后可留存报告；
* **明确确认**——破坏性覆盖需要显式 flag（`--overwrite`），不存在
  缺省开启的路径；
* **可审计**——命令执行与结果记录在操作日志，而非进程启动日志的
  一行 INFO 计数。

**不作为**：`config.yaml` 中的 `bootstrap.mode: overwrite` 这类永久
状态。一次性迁移完成后，系统的稳态是 `import`（或演练态 `check`）。

## 4. Drift 处理（冻结）

IMPORT 模式下 seed 与 DB 的分歧处理：

```
incoming YAML
      |
      v
   compare (canonical payload, 按 (provider, resource_id))
      |
      +-- sink 缺失      → insert（added）
      +-- 完全一致       → no-op（unchanged）
      +-- 内容不同       → conflict → **keep DB**，永不覆盖
      +-- YAML 缺失      → db_only → keep DB（YAML 是 seed，不是同步源，
                            缺席不构成删除信号）
```

* conflict / db_only 保持**非致命**（DB 为真相，启动不因此中止）；
* 可见性要求：conflict 与 db_only 必须以 WARNING 级、逐键 canonical
  diff 呈现（后续 FIX 任务落地；TASK-CONFIG-001 风险 R-B）；
* drift 的正规消解方式只有两种：修改 DB（Admin API），或更新 seed 后
  重新 import（只补缺失键）。**任何试图用 seed 反向覆盖 DB 的路径都
  违反本 ADR**。

## 5. 验收清单

* [x] 本 ADR 明确禁止 YAML 回写（§1）。
* [x] 本 ADR 明确 `overwrite` 非启动模式（§2、§3）。
* [x] 本 ADR 不要求代码变化（Status: accepted；§2/§4 标注的校验收紧
      与可见性升级为后续独立任务）。
