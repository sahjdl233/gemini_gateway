# CONFIG-R2B-MODEL-REGISTRY-INVALIDATION — ModelRegistry invalidation boundary (ADR)

Status: accepted（契约冻结；本 ADR 只引入 `ModelRegistry.invalidate()` 与其测试契约，无 event bus / 回调 / 重构）
Baseline: `107cb09`（CONFIG-003 之后）
来源: TASK-CONFIG/R-2-B（R-2 ModelRegistry 滞后窗口的可观测性缺口，CONFIG-001 审计提出）

---

## 0. 现状与问题

`ModelRegistry` 的索引通过 TTL（默认 300s）+ 首查询懒构建 + 手动
`refresh()` 维护，Discovery 失败保留 last known good（TASK-MODEL-001）。
控制面（resource/credential definition 变更）发生后，索引最长滞后一个
TTL：disable/delete 掉某 provider 最后一个资源后，`/v1/models` 仍
advertise 其模型至多 300s（请求最终以干净的 503 告终，不会误用资源，
但对外观感是"有模型却不可用"）。

CONTROL-006/CONFIG-001 审计将其列为 R-2，定性为**需要明确契约**而非
立即修复。本 ADR 冻结该契约。

## 1. 契约：`ModelRegistry.invalidate()`

```python
registry.invalidate()   # 标记索引过期；下一次查询触发一次全新 Discovery
```

性质（由 `tests/core/test_model_registry_invalidation.py` 锁定）：

| 性质 | 语义 |
|---|---|
| **Lazy** | invalidate 本身从不调用 provider，只清除 freshness 标记；下一次 `_ensure_fresh` / `refresh` 重建一次（single-flight 仍生效）。 |
| **State-preserving** | per-provider last-known-good Discovery 状态、failure 计数、fallback 语义完全不受影响。 |
| **Idempotent** | 重复 invalidate 安全，不叠加、不产生额外 refresh。 |
| **手动 refresh 兼容** | `refresh()` 语义不变；`invalidate() → refresh()` 与 `refresh() → invalidate()` 两种顺序均可组合。 |

## 2. 允许的触发者（control-plane definition 变化）

只有**定义层**变化才允许调用 invalidate：

* provider definition 变化（新增/删除 provider）；
* resource definition 变化（create/update/delete，含 `enabled`、
  `credential_id` 重绑——它们决定 discovery 资源选择器能否选中资源）；
* credential / capability 变化且影响 discovery 所能返回的结果。

调用者是执行该变更的控制面组件（如 `ResourceManager._reconcile_runtime`
成功后的通知点），由未来 FIX 任务接线；本 ADR 只冻结 API 与边界。

## 3. 禁止的触发者（runtime scheduling state）

以下状态**永不**触发 invalidate（`test_runtime_state_changes_never_invalidate_the_index`
钉死：池状态变更后 freshness 标记与索引均不变）：

* health / cooldown 状态及其变化；
* retry、backoff、429 限流事件；
* in-flight 计数、success/failure 计数器等观测状态。

理由：模型索引回答的是"哪些模型存在"，由 definition 决定；runtime
调度状态回答的是"现在该用谁"，由 pool 决策。把 scheduler health hook
接进 registry 会让 404 语义随负载抖动，并使 discovery 流量随故障波动。

## 4. 边界之外（本 ADR 明确不做）

* ❌ ResourceRepository callback / 自动监听 DB
* ❌ Observer / EventEmitter / event bus
* ❌ ModelRegistry 重构、provider interface 变更、scheduler 路由变更
* TTL 行为、手动 refresh、discovery failure fallback 全部保持原样

## 5. 验收清单

* [x] `pytest tests/core/test_model_registry*` 通过（16 既有 + 6 新契约）。
* [x] invalidate contract test 存在（Case A/B/C + runtime boundary + manual refresh 组合）。
* [x] 未引入 event bus / repository callback / observer。
* [x] 未修改 ResourceRepository、Scheduler 路由、TTL 行为。
* [x] manual refresh 与 discovery failure fallback 保留。
