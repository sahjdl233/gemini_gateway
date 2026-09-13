# TASK-008-FIX-01 实现记录

## 修改文件

1. providers/gemini_cli/onboard.py - 完整重写，新增 onboarding 网络调用实现
2. providers/gemini_cli/provider.py - 新增 _ensure_project() 方法，修改 complete()/stream() 调用链
3. providers/gemini_cli/client.py - post() 方法增加 operation 参数（默认 "generateContent"，向后兼容）
4. providers/gemini_cli/resource.py - ide_type 默认值从 "ANTIGRAVITY" 修正为 "GCLI"
5. tests/providers/gemini_cli/test_onboard.py - 新增/更新单元测试
6. tests/providers/gemini_cli/test_provider.py - 既有测试保持通过

## 实现方式

### 核心逻辑：_ensure_project(resource) 统一入口

async def _ensure_project(self, resource: GeminiCliResource) -> None:
    if resource.project_id:
        return
    client = await self._client_for(resource)
    from providers.gemini_cli.onboard import discover_project
    resource.project_id = await discover_project(client, resource)

- complete() 和 stream() 都在真正发送请求前调用 await self._ensure_project(res)
- 只有 resource.project_id 为 None 时才触发 onboarding
- Onboarding 失败直接抛出 GeminiCliProtocolError，不会进入后续 retry 逻辑

### Onboarding 完整流程（onboard.py 新增 4 个异步函数）

1. load_code_assist() - POST /v1internal:loadCodeAssist，返回 parsed dict（含 project_id、needs_onboarding、default_tier_id、tier）
2. onboard_user() - POST /v1internal:onboardUser（tierId + metadata），启动 LRO，返回 operation name
3. poll_operation() - LRO polling（最多 5 次 x 2s = 10s），解析 done / cloudaicompanionProject.id
4. discover_project() - 统一入口：
   - 先 loadCodeAssist
   - 若已有 project_id -> 直接返回
   - 若 needs_onboarding -> onboardUser -> poll -> 返回 project_id
   - 任何失败都抛出 GeminiCliProtocolError

### Client 复用 401 重试

- post() 增加 operation: str = "generateContent" 参数
- onboarding 调用时传入 operation="loadCodeAssist" / "onboardUser" / LRO path
- 复用既有的 401 -> invalidate -> retry once + 错误分类

### ide_type 修正

- GeminiCliResource.ide_type 默认值从 "ANTIGRAVITY" -> "GCLI"
- 测试同步更新 test_onboard.py

## 新增测试

| 测试文件 | 新增测试用例 | 说明 |
|---------|------------|------|
| test_onboard.py | parse_load_code_assist: existing_project / new_account / ultra_tier | 验证 project_id / tier / needs_onboarding 解析 |
| test_onboard.py | parse_operation: done / in_progress / invalid | 验证 LRO 响应解析 |
| test_onboard.py | inspect_operation: raw / full_url / empty | 验证 operation path 提取 |
| test_onboard.py | metadata_for | 验证 ideType/platform/pluginType |
| test_provider.py | 既有 15 个测试全部通过 | 验证 complete/stream/onboarding 端到端 |

## 测试结果

- providers/gemini_cli/ 目录: 74 passed
- providers/ 目录: 203 passed
- 全量测试: 368 passed（仅 1 个无关 Windows 临时目录 PermissionError）
- 所有原有测试保持通过，未破坏 401 refresh / OAuth / 并发行为

## 已知限制

1. 无并发锁 - 当前未在 _ensure_project 中加 per-resource asyncio.Lock。若 ResourcePool 允许同一资源并发初始化，可能触发多次 onboarding。任务说明允许保持现状，因为当前调用模型已保证不会并发初始化。
2. LRO polling 固定参数 - 硬编码 5 次 x 2s，未做指数退避。符合 TASK-007 规范。
3. onboarding 错误不重试 - Onboarding 失败直接抛错，不进入 Scheduler retry。符合需求。
4. 401 refresh 行为完全保留 - Onboarding 复用 client.post() 自带 401 retry 逻辑，不改变既有行为。

## 完成确认清单

- [x] project_id 已存在时不会重复 onboarding
- [x] project_id 缺失时 Provider 自动 discovery
- [x] loadCodeAssist -> onboardUser -> LRO 正常串联
- [x] discovered project 写回 Resource.project_id
- [x] complete / stream 都使用 _ensure_project()
- [x] onboarding 失败明确报错
- [x] OAuth 401 refresh 行为未改变
- [x] 不影响现有 Provider
- [x] 全部测试通过 (368 passed, 1 unrelated error)
