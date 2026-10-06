# Gemini Gateway

独立于 OmniRoute 的 Gemini 专用 Gateway：对外提供统一的 OpenAI-compatible API，对内通过可插拔 Provider Adapter 接入不同 Gemini 上游。

> 当前阶段：Post-WEBUI-002 / Post-AUTH-015。已集成多个 Provider（fake / anonymous_vertex / firebase / gemini_cli / antigravity），并具备 Credential 架构、PostgreSQL 加密落库与 Vue 3 Admin WebUI。

## 架构

```
客户端 (SillyTavern / OmniRoute / OpenAI-compatible client)
        |
        v
Gemini Gateway (FastAPI + Scheduler)
        |
        v
Provider Adapter (Provider 抽象)
        |            |
        v            v
  AnonymousVertex  FakeProvider  GeminiCLI  Firebase  Antigravity
        |
        v
Anonymous Vertex / Agent Platform studio (batchGraphql)
```

核心原则（源自项目初始设计约定）：

- Provider 与 Scheduler 彻底分离：Scheduler 不知道 Google 协议。
- Resource 是核心抽象：Firebase=Project、Vertex=Egress/Session、CLI=Credential/Account。
- 敏感认证材料不作为 Resource 字段直接存储：Resource 只持有 `credential_id`，凭据由 Credential 体系管理。
- 429 是一等公民：RateLimitError 携带 retry_after / provider / resource_id / scope。
- Cooldown 支持 Retry-After，缺失时用指数退避 + jitter，禁止写死 sleep。
- Streaming 从第一天设计：Provider -> AsyncIterator[ChatChunk] -> Gateway SSE。
- Proxy/Egress 独立于 Provider：Provider 不知道 sing-box 存在。
- 禁止把 "换 IP = 无限额度" 写进代码。
- 日志必须自动脱敏：凭据绝不进入 Git / 日志 / 异常堆栈。

## 快速开始

```bash
# 1. 创建虚拟环境并安装依赖（按 pyproject.toml 的 dependencies / dev）
python -m venv .venv
.venv/Scripts/python -m pip install ".[dev]"

# 可选：anonymous_vertex 节点池使用 socks5 出站时才需要（direct/http/https 无需安装）
.venv/Scripts/python -m pip install "gemini-gateway[socks]"
# 以上需要网络访问；若离线，依赖已满足即可直接从项目根目录运行：

# 2. 启动（无 config.yaml 时使用内置 fake 配置）
.venv/Scripts/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000

# 3. 测试
.venv/Scripts/python -m pytest
```

## API

| 方法 | 路径 | 说明 |
|------|------|------|
| GET  | /v1/models             | 所有已启用 Provider 的模型 |
| POST | /v1/chat/completions   | 会话补全（支持 stream=true） |

示例（非流式）：

```bash
curl -X POST http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gemini-3.8-flash","messages":[{"role":"user","content":"hi"}]}'
```

示例（流式）：

```bash
curl -N -X POST http://127.0.0.1:8000/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{"model":"gemini-3.8-flash","messages":[{"role":"user","content":"hi"}],"stream":true}'
```

## 配置

配置使用 YAML 文件：

- config.yaml —— 运行时配置（可选，缺省使用内置 fake 配置）
- config.yaml.example —— 配置示例

敏感信息一律通过环境变量占位符注入，禁止写入 Git。
- GEMINI_GATEWAY_ENCRYPTION_KEYS / GEMINI_GATEWAY_ENCRYPTION_KEY_ID 控制 AES-256-GCM 加密 keyring / key rotation
- GEMINI_GATEWAY_ENCRYPTION_KEY 提供 legacy 兼容 key（如源码仍保留）
- GEMINI_GATEWAY_DATABASE_URL 控制 PostgreSQL Credential backend

### Credential 配置

长期认证材料统一放在 `credentials` 里，Resource 通过 `credential_id` 引用，
Resource 上只保留 `project_id` 等非敏感字段：

```yaml
credentials:
  - id: firebase-01
    type: api_key
    payload:
      api_key: ${FIREBASE_01_API_KEY}
      app_id: ${FIREBASE_01_APP_ID}
      debug_token: ${FIREBASE_01_DEBUG_TOKEN}
  - id: google-oauth-01
    type: oauth
    payload:
      refresh_token: ${GEMINI_CLI_01_REFRESH_TOKEN}
      client_id: ${GEMINI_CLI_01_CLIENT_ID}
      client_secret: ${GEMINI_CLI_01_CLIENT_SECRET}

providers:
  firebase:
    enabled: true
    resources:
      - id: firebase-project-01
        credential_id: firebase-01
        project_id: FIREBASE_PROJECT_01
```

Legacy 的 provider-specific 凭据字段（直接写在 Resource 上的
`api_key` / `refresh_token` 等）仍然受支持，仅作为迁移 / 兼容路径；在
postgres backend 下会自动迁移为加密 Credential 并回填 `credential_id`。

## Admin WebUI

Admin WebUI 使用 **Vue 3 + TypeScript + Vite**，前端源码位于 `webui/`：

- Dashboard / Resources / Credentials 三个视图，敏感字段在 UI 与 API 响应中均脱敏
- Admin API 由 `ADMIN_TOKEN` 环境变量保护

构建：

```bash
cd webui
npm install
npm run build
```

产物输出到 `webui/dist`，由 FastAPI 通过 `mount_admin_assets()` 提供服务。

生产部署只需 **Python / Uvicorn + `webui/dist`**：Node.js / npm 仅用于前端开发
和重新构建 bundle，gateway 运行本身不需要 Node。

## 目录结构

```
app/          FastAPI 应用与路由
core/         models / provider / resource / pool / scheduler / errors / health / cooldown
protocol/     openai / gemini / common（HTTP <-> 内部模型 <-> Provider）
transport/    http / proxy / streaming（Proxy 独立于 Provider）
execution/    ExecutionBackend 抽象与 HTTP 实现（当前仅 antigravity 已接入）
providers/    fake / anonymous_vertex / firebase / gemini_cli / antigravity
config/       YAML/JSON 加载与 env 占位符替换
tests/        core / protocol / providers / app
webui/        Admin WebUI 前端（Vue 3 + TypeScript + Vite），构建产物在 webui/dist
```

当前在 `app/bootstrap.py` 中注册的 builtin provider 为：
`fake` / `anonymous_vertex` / `firebase` / `gemini_cli` / `antigravity`。
`providers/vertex/` 目录虽然存在，但未注册为 builtin provider，不属于当前
运行时 provider 集合。

## 测试原则

每个真实 Provider 落地时必须配套：Unit / Integration / Streaming / Failure 测试，重点覆盖 200/401/403/404/429/500/502/503/timeout/malformed SSE/connection reset。TASK-001 起用 FakeProvider 离线验证 Scheduler/Pool/Retry/Cooldown/SSE/Fallback。

## 状态

见 PROJECT_STATE.md。
