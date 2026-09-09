# Gemini Gateway

独立于 OmniRoute 的 Gemini 专用 Gateway：对外提供统一的 OpenAI-compatible API，对内通过可插拔 Provider Adapter 接入不同 Gemini 上游。

> 当前阶段（TASK-000）只提供架构与骨架：不接入任何真实 Google 服务，不存储任何 Google 凭据。

## 架构

```
客户端 (SillyTavern / OmniRoute / OpenAI-compatible client)
        |
        v
Gemini Gateway (FastAPI + Scheduler)
        |
        v
Provider Adapter (Provider 抽象)
        |
        v
Google 上游 (未来任务实现)
```

核心原则（TASK-000 第 1-27 条）：

- Provider 与 Scheduler 彻底分离：Scheduler 不知道 Google 协议。
- Resource 是核心抽象：Firebase=Project、Vertex=Egress/Session、CLI=Credential/Account。
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

第一版不使用数据库，使用 YAML 文件：

- config.yaml —— 运行时配置（可选，缺省使用内置 fake 配置）
- config/providers.yaml —— 各 Provider 开关与资源（TASK-002 起）
- config/resources/ —— 资源描述（TASK-002 起）

敏感信息一律通过环境变量占位符注入，禁止写入 Git（见 config.yaml.example）。

## 目录结构

```
app/          FastAPI 应用与路由
core/         models / provider / resource / pool / scheduler / errors / health / cooldown
protocol/     openai / gemini / common（HTTP <-> 内部模型 <-> Provider）
transport/    http / proxy / streaming（Proxy 独立于 Provider）
providers/    fake(已实现) / firebase / vertex / express / cli / build / antigravity(接口预占)
config/       YAML/JSON 加载与 env 占位符替换
tests/        core / protocol / providers / app
```

## 测试原则

每个真实 Provider 落地时必须配套：Unit / Integration / Streaming / Failure 测试，重点覆盖 200/401/403/404/429/500/502/503/timeout/malformed SSE/connection reset。TASK-001 起用 FakeProvider 离线验证 Scheduler/Pool/Retry/Cooldown/SSE/Fallback。

## 状态

见 PROJECT_STATE.md。
