# TASK-007 Research: GCLI Protocol Source Audit

> Audit date: 2026-09-10
> Audited HEAD: cdbaf37 (2026-09-09)
> Audited repo: https://github.com/su-kaka/gcli2api (cloned to local workspace)
> Status: Complete - research only, no Gateway code modified

---

## 1. Executive Summary

**核心结论：gcli2api 的 GCLI 模式不是 API-Key + generativelanguage.googleapis.com 路径，而是 Google OAuth 2.0 + cloudcode-pa.googleapis.com 的 Google Code Assist 内部协议。**

关键事实：

1. **Endpoint 完全不同**：GCLI 模式走 https://cloudcode-pa.googleapis.com/v1internal:generateContent 和 v1internal:streamGenerateContent?alt=sse，而不是公开的 generativelanguage.googleapis.com/v1beta。
2. **认证**：Authorization: Bearer <OAuth access_token>（Google 账号 OAuth，非 API Key）。
3. **请求 envelope**：外层包一层 {model, project, request} - 其中 project 是 cloudaicompanionProject（Code Assist 专属项目 ID），request 是标准 Gemini GenerateContent 请求。
4. **响应 envelope**：结果包在 {"response": {...Gemini Response...}, "traceId": ...} 里。流式是 SSE（data: 前缀），不是 Anonymous Vertex 的 NDJSON。
5. **项目绑定**：cloudaicompanionProject 通过 v1internal:loadCodeAssist（已有账号）或 v1internal:onboardUser（首次激活）自动获取，不需要用户预先提供 GCP Project。
6. **配额**：来自 loadCodeAssist 返回的 tier（FREE/PRO/ULTRA），429 错误体含 quotaResetTimeStamp 或 "Your quota will reset after Xs" 可解析出冷却时间。
7. **多账号**：凭证 = 一个 Google OAuth 账号 + 一个 Code Assist project，完全符合 Gateway 的 Resource 模型。

**结论：IMPLEMENT（可作为独立 GeminiCliProvider，且不引入 gcli2api 依赖）。**

---

## 2. gcli2api GCLI Architecture

[gcli2api](https://github.com/su-kaka/gcli2api) HEAD cdbaf37（2026-09-09）：

Client (OpenAI/Gemini/Claude format)
  -> gcli2api FastAPI (port 7861)
    -> src/router/geminicli/{openai,gemini,model_list}.py   # 格式入口
    -> src/converter/openai2gemini.py                       # OpenAI<->Gemini 转换
    -> src/converter/gemini_fix.py                          # Gemini 规范化(thinking/tools)
    -> src/api/geminicli.py                                 # 请求封装+重试+凭证轮换
      -> src/credential_manager.py                          # 多账号凭证池
      -> post_async / stream_post_async                     # httpx 客户端
        -> https://cloudcode-pa.googleapis.com/v1internal:*

关键模块职责（仅 GCLI 模式）：

| 模块 | 职责 | Gateway 是否需要 |
|------|------|------------------|
| src/router/geminicli/* | HTTP 入口 + 格式路由 | 不需要（Gateway 已有 OpenAI API） |
| src/converter/openai2gemini.py | OpenAI<->Gemini 转换 | 参考（可复用设计） |
| src/converter/gemini_fix.py | thinking/tools 规范化 | 参考（可复用设计） |
| src/api/geminicli.py | 请求封装、重试、凭证轮换 | 核心参考 |
| src/credential_manager.py | 凭证池 | 参考（Gateway ResourcePool 替代） |
| src/google_oauth_api.py | OAuth + loadCodeAssist + onboardUser | 核心参考 |
| src/auth.py | 浏览器 OAuth 流程 | 不需要（一次性脚本用途） |
| src/storage/* | SQLite/MongoDB | 不需要 |
| src/panel/* | Web 面板 | 不需要 |
| src/api/antigravity.py | Antigravity 模式 | 不需要 |

---

## 3. OAuth Flow（源码确认）

### 3.1 Client 常量（src/utils.py）

CLIENT_ID = "681255809395-oo8ft2oprdrnp9e3aqf6av3hmdib135j.apps.googleusercontent.com"
CLIENT_SECRET = "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"
SCOPES = [cloud-platform, userinfo.email, userinfo.profile]
TOKEN_URL = "https://oauth2.googleapis.com/token"
CALLBACK_HOST = "localhost"

### 3.2 授权 URL（src/google_oauth_api.py Flow.get_auth_url）

https://accounts.google.com/o/oauth2/auth?
  client_id=...&
  redirect_uri=http://localhost:{port}&
  scope=cloud-platform+userinfo.email+userinfo.profile&
  response_type=code&
  access_type=offline&
  prompt=consent&
  include_granted_scopes=true&
  state={state}

### 3.3 授权码换 token（Flow.exchange_code）

POST https://oauth2.googleapis.com/token
Content-Type: application/x-www-form-urlencoded

client_id=...&client_secret=...&redirect_uri=...&code=...&grant_type=authorization_code

=> { access_token, refresh_token, expires_in: 3600 }

### 3.4 回调服务器（src/auth.py）

- 动态分配端口（默认 11451，搜索可用端口）
- AuthCallbackHandler 接收 ?code=...&state=...
- 同步轮询 wait_for_callback_sync（超时 300s）


---

## 4. Credential Structure（源码确认）

凭证最终以 JSON 文件落盘（creds/ 目录），字段：

{
  "client_id": "681255809395-....apps.googleusercontent.com",
  "client_secret": "GOCSPX-...",
  "token": "ya29.a0...",
  "refresh_token": "1//0g...",
  "scopes": ["cloud-platform", ...],
  "token_uri": "https://oauth2.googleapis.com/token",
  "project_id": "gen-lang-...",
  "expiry": "2026-01-10T01:55:31+00:00"
}

| 字段 | 必需 | 运行时生成/更新 | 说明 |
|------|------|----------------|------|
| token | 是 | 每次刷新更新 | access token，Bearer 头 |
| refresh_token | 是 | 很少更新 | OAuth refresh token |
| client_id/secret | 是 | 否 | 固定常量 |
| project_id | 是 | 初次 onboarding 生成 | cloudaicompanionProject |
| expiry | 可选 | 每次刷新更新 | 提前 3 分钟判定过期 |
| scopes/token_uri | 可选 | 否 | 元信息 |

---

## 5. Access Token Refresh（源码确认）

Credentials.refresh()（src/google_oauth_api.py）：

POST {OAUTH_PROXY_URL}/token
Content-Type: application/x-www-form-urlencoded

client_id=...&client_secret=...&refresh_token=...&grant_type=refresh_token

=> { access_token, expires_in }

逻辑：

1. is_expired()：expires_at - 3min <= now 判定过期（提前 3 分钟）。
2. refresh_if_needed()：过期则 refresh；无 refresh_token 抛 TokenError。
3. 刷新失败：record_api_call_error -> 凭证被禁用 -> 获取下一个凭证。
4. **未看到 401 强制刷新的逻辑** - geminicli 按 401 属于非重试错误直接返回。Gateway 需要自己加 401 -> force refresh -> retry。

---

## 6. Code Assist Endpoint 完整表格

典型 BASE：https://cloudcode-pa.googleapis.com（可配 CODE_ASSIST_ENDPOINT 覆盖）

| Operation | HTTP Method | URL Path | Auth | Body |
|-----------|------------|----------|------|------|
| loadCodeAssist | POST | /v1internal:loadCodeAssist | Bearer access_token | {metadata:{ideType:ANTIGRAVITY}} |
| onboardUser | POST | /v1internal:onboardUser | Bearer access_token | {tierId, metadata:{ideType,platform,pluginType}}（LRO 轮询 10s） |
| generateContent | POST | /v1internal:generateContent | Bearer access_token | {model, project, request} |
| streamGenerateContent | POST | /v1internal:streamGenerateContent?alt=sse | Bearer access_token | {model, project, request} |

注意：gcli2api 源码中**没有**独立的 retrieveUserQuota 调用 - 配额/项目信息统一由 loadCodeAssist 返回。

---

## 7. Project / cloudaicompanionProject 流程（重点）

### 7.1 首次（loadCodeAssist 无 currentTier）

1. POST /v1internal:loadCodeAssist body {metadata:{ideType:ANTIGRAVITY}}
2. 响应含 allowedTiers[]，_get_onboard_tier 找 isDefault 的 tier（回退 LEGACY）。
3. POST /v1internal:onboardUser body {tierId, metadata:{ideType:ANTIGRAVITY, platform:PLATFORM_UNSPECIFIED, pluginType:GEMINI}}
4. onboardUser 是 LRO：轮询（最多 5x2s=10s），done=true 后取 response.cloudaicompanionProject.id。

### 7.2 已有账号（loadCodeAssist 有 currentTier）

1. POST /v1internal:loadCodeAssist
2. 直接取 cloudaicompanionProject 字段作为 project_id。
3. 同时读 paidTier.id / currentTier.id -> tier，paidTier.availableCredits[0].creditAmount -> 积分。

### 7.3 tier 映射（_map_raw_tier）

g1-ultra-tier / ws-ai-ultra-business-tier -> ultra
g1-pro-tier / helium-tier / standard-tier   -> pro
free-tier                                  -> free
unknown                                    -> pro (默认)

### 7.4 红线确认

- cloudaicompanionProject **不用用户预先提供**，是 Code Assist 自动为账号创建/返回的内部项目。
- 一个 OAuth 账号 <-> 一个 cloudaicompanionProject（1:1）。
- 不同账号 project 不同（每凭证独立落盘）。
- project 即 quota 载体（tier/credits 挂在 cloudaicompanionProject 上）。


---

## 8. generateContent 请求 envelope（源码确认）

### 8.1 外层 Code Assist envelope（src/api/geminicli.py prepare_request_headers_and_payload）

{
  "model": "gemini-2.5-flash",
  "project": "gen-lang-client-xxxxx",
  "request": {
    "contents": [...],
    "systemInstruction": {...},
    "generationConfig": {...},
    "tools": [...]
  }
}

Headers:
Authorization: Bearer ya29.a0...
Content-Type: application/json
User-Agent: Mozilla/5.0 (compatible; Google-Gemini-CLI/1.0; +https://github.com/google-gemini/gemini-cli) {model}

### 8.2 内层 Gemini GenerateContent request

- contents[]: {role: user|model, parts:[{text|inlineData|functionCall|functionResponse|thought}]}
- systemInstruction: {parts:[{text}]}
- generationConfig: {temperature, maxOutputTokens, topK, topP, stopSequences, thinkingConfig:{thinkingBudget|thinkingLevel, includeThoughts}}
- tools[]: {functionDeclarations} 或 {googleSearchRetrieval}（-search 模型）
- safetySettings[]: 固定 10 项 BLOCK_NONE（含 CIVIC_INTEGRITY / IMAGE_* / JAILBREAK）

### 8.3 OpenAI -> Code Assist 映射（src/converter/openai2gemini.py + gemini_fix.py）

| OpenAI 字段 | Gemini request 字段（request 内层） |
|------------|-------------------------------------|
| messages[role=system] | systemInstruction（合并多条 system） |
| messages[role=user] | contents[].parts[].text / inlineData |
| messages[role=assistant] | contents[].parts[].text |
| messages[role=assistant].tool_calls | parts[].functionCall |
| messages[role=tool] | parts[].functionResponse |
| tools[] | tools[].functionDeclarations |
| tool_choice | toolConfig.functionCallingConfig |
| temperature | generationConfig.temperature |
| max_tokens | generationConfig.maxOutputTokens |
| top_p | generationConfig.topP |
| top_k | generationConfig.topK |
| stop | generationConfig.stopSequences |
| reasoning_effort | thinkingConfig.thinkingBudget/thinkingLevel（由 model 后缀推导） |

### 8.4 模型后缀 -> thinking 预算推导（gemini_fix.py get_thinking_settings）

Gemini 2.5 系列 (thinkingBudget 数字):
  -max=32768(pro)/24576(flash), -high=16000, -medium=8192, -low=1024,
  -minimal=128(pro)/0(flash), 无后缀=None(默认)
Gemini 3 系列 (thinkingLevel 等级):
  -high/-medium/-low/-minimal -> 对应等级；flash 支持 medium，pro 不支持 medium

### 8.5 响应解包（router/geminicli/gemini.py）

非流式 200 响应：{"response": {...Gemini Response...}, "traceId": "..."} -> 解包出 response 字段返回给客户端。
流式 SSE：每条 data: {"response": {...chunk...}, "traceId": "..."}；无 [DONE] 哨兵（用空行/流结束）。

---

## 9. streamGenerateContent 协议（源码确认）

### 9.1 传输形态

- **SSE 行模式**（stream_post_async native=False）：aiter_lines() 逐行读取，每行是 data: {json}。

示例真实响应（Response_example.txt）：

data: {"response": {"candidates": [{"content": {"role": "model", "parts": [{"text": "Why did the scarecrow..."}], "thought": false}, "finishReason": "STOP"}], "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 17, "totalTokenCount": 51, "trafficType": "PROVISIONED_THROUGHPUT", "promptTokensDetails": [...], "candidatesTokensDetails": [...], "thoughtsTokenCount": 24}, "modelVersion": "gemini-2.5-flash", "createTime": "2026-01-10T01:55:29.168589Z", "responseId": "kbFhaY2lCr-ZseMPqMiDmAU"}, "traceId": "55650653afd3c738"}

空行/空 chunk 后结束。

### 9.2 与已有 Provider streaming 对比

| Provider | 格式 | 结束哨兵 | 外层 envelope |
|----------|------|----------|---------------|
| Anonymous Vertex | NDJSON（brace-counting） | finishReason=UNSPECIFIED 忽略 | batchGraphql envelopes |
| Firebase | SSE | data: [DONE] | 无（直接 candidates） |
| GCLI (Code Assist) | SSE（data: 行） | 空行（无 [DONE]） | {"response":{...}, "traceId"} |

注意：GCLI 需要**解包 response envelope**，这是与 Firebase 的 key 差异。

### 9.3 finishReason / usage 获取

- finishReason：response.candidates[0].finishReason（STOP/MAX_TOKENS/SAFETY/RECITATION）。
- usage：response.usageMetadata（promptTokenCount/candidatesTokenCount/thoughtsTokenCount/totalTokenCount）。
- thinking：thought: true 的 part 或 usageMetadata.thoughtsTokenCount>0（配合 RETURN_THOUGHTS_TO_FRONTEND 决定是否输出 reasoning_content）。

---

## 10. Error Handling（源码确认）

### 10.1 重试策略（src/api/geminicli.py + api/utils.py）

可重试状态码：429, 500, 503 + AUTO_BAN_ERROR_CODES（默认 [403]）。

| 状态码 | 行为 |
|--------|------|
| 429 | 解析 cooldown -> 记录错误 -> 切换到下一凭证 -> 重试（最多 max_retries，默认 5） |
| 403 | 自动禁用该凭证（auto-ban）-> 换凭证重试 |
| 404 | 若模型名含 preview：标记该凭证 preview=False，换凭证；否则直接返回 |
| 500/503 | 换凭证重试 |
| 401 | 非重试错误码，直接透传（gcli2api 不自动刷新重试） |
| 400 | 直接透传 |
| timeout/网络错误 | 捕获异常，asyncio.sleep(retry_interval) 后继续 |

### 10.2 cooldown 解析（api/utils.py parse_quota_reset_timestamp）

429 错误体两种来源：

1. details[].metadata.quotaResetTimeStamp（ISO8601）或 quotaResetDelay（13h19m1.20964964s 格式）。
2. message 内 "Your quota will reset after 6h 30m 15s."（RATE_LIMIT_EXCEEDED）。
3. 都没解析出来：RESOURCE_EXHAUSTED 默认冷却 4 小时。

### 10.3 凭证轮换细节

- 预热：asyncio.create_task(get_valid_credential(...)) 提前拿下一个凭证。
- 切换：_switch_credential_for_retry 优先用预热任务结果，失败回退同步刷新。
- 每次切换只更新 Authorization 头和 final_payload[project]，不重建请求体。

---

## 11. Multi-account / Resource Model

**Gateway 资源粒度结论：一个 Code Assist OAuth 凭证 = 一个独立 Resource。**

Resource 内含：

- account identity（OAuth token）
- cloudaicompanionProject（project_id）
- tier（FREE/PRO/ULTRA，来自 loadCodeAssist）
- quota 状态（429 cooldown_until，可落 Resource cooldown）
- preview 支持位（404 preview 模型时置 False）

gcli2api 的 credential_manager（随机负载均衡 + 冷却 + 自动禁用 + 按模型 preview 过滤）与 Gateway 的 ResourcePool 职责重叠，**不应整体搬入**，而是用 ResourcePool 的标准机制表达：

- 一个 resource = 一个凭证文件（或 DB 行）。
- cooldown / health / 轮换 交给 Core 现成机制。
- 凭证级自动禁用（403）可用 health + cooldown 表达。


---

## 12. What to Reuse（必须提取进 GeminiCliProvider）

| 逻辑 | 来源文件 | 提取方式 |
|------|----------|----------|
| loadCodeAssist / onboardUser / project 获取 | google_oauth_api.py | 移植（async httpx，无浏览器） |
| Credentials.from_dict / to_dict / is_expired / refresh | google_oauth_api.py | 移植 |
| access token refresh（oauth2 token endpoint） | google_oauth_api.py | 移植 |
| tier 映射表 | google_oauth_api.py | 移植 |
| 外层 envelope 构建 {model, project, request} | api/geminicli.py | 移植 |
| SSE data: 解析 + response envelope 解包 | api/geminicli.py + router/gemini.py | 移植 |
| quota cooldown 解析 | api/utils.py | 移植 |
| thinking 预算/等级推导 | converter/gemini_fix.py | 移植 |
| safetySettings 固定 10 项 BLOCK_NONE | converter/gemini_fix.py | 移植 |
| User-Agent 模板 | utils.py | 移植 |

---

## 13. What NOT to Reuse（明确不搬）

- Web Console / panel/
- SQLite / MongoDB / psql 存储层
- src/api/antigravity.py 及 router/antigravity/ 全部
- src/converter/anti_truncation.py（抗截断，与 GCLI 协议无关的招数）
- src/converter/fake_stream.py（假流式，Gateway 不需要）
- src/converter/thoughtSignature_fix.py（工具签名占位 hack）
- gcli2api 自己的认证密码体系（API_PASSWORD/PANEL_PASSWORD）
- Docker / render.yaml / zeabur.yaml / 安装脚本
- OpenAI/Claude converter（Gateway 已有 OpenAI 接口，只做 OpenAI->Gemini 即可）
- credential_manager（用 ResourcePool 替代）
- task_manager / token_estimator / keeplive

---

## 14. 与当前 Gateway 的边界

### Core（已有，不改）

- OpenAI API（/v1/chat/completions, /v1/models）
- Scheduler / 模型路由 / fallback
- ResourcePool / health / cooldown / round-robin
- Provider 抽象 / 统一错误层级

### GeminiCliProvider（新建）

- OAuth token 管理与刷新（httpx，无浏览器）
- loadCodeAssist / onboardUser（project & tier 获取）
- Code Assist endpoint 调用（v1internal:generateContent / streamGenerateContent）
- Code Assist envelope 构建 + response envelope 解包
- SSE streaming 解析
- quota cooldown 解析 -> 上游 ProviderError（RateLimitError + retry_after）
- 401 -> force refresh -> retry（Gateway 增强，gcli2api 没有）
- 错误映射（429/403/404/401/5xx -> ProviderError 层级）

### 一次性凭证引导（独立脚本，不入 Core）

- 浏览器 OAuth 授权 -> creds/<name>.json
- 调用 loadCodeAssist/onboardUser 生成 project_id
- 输出与 Gateway 兼容的凭证文件

---

## 15. 建议的 GeminiCliProvider 架构

providers/geminicli/
  __init__.py          # 导出 Provider/Resource/Factories
  provider.py          # GeminiCliProvider（Provider 接口）
  resource.py          # GeminiCliResource（OAuth 凭证 + project + tier + cooldown）
  factory.py           # ProviderFactory + ResourceFactory
  auth.py              # Credentials 模型 + access token refresh（httpx）
  onboard.py           # loadCodeAssist / onboardUser / tier 映射
  protocol.py          # Code Assist envelope 构建 + response 解包
  streaming.py         # SSE data: 行解析 + envelope 解包 + chunk 归一化
  errors.py            # 上游错误 -> Gateway ProviderError 映射 + quota 解析
  user_agent.py        # User-Agent 模板

接入点：app/bootstrap.py register_builtin_providers() 注册 geminicli。

---

## 16. Risks / Limitations

1. **内部 endpoint**：v1internal:* 是 Google 未公开文档的内部协议，Google 可能随时变更/封禁。
2. **OAuth 首次登录需浏览器**（无头环境需一次性脚本引导）。
3. **client_id/secret 硬编码**是 gcli2api 反向分析出的第三方值，Google 可能吊销。
4. **409/401 语义**：gcli2api 未实现 401->refresh->retry，Gateway 需自行补齐。
5. **tier/配额由 Google 决定**：免费 tier 配额低，需要多账号轮换（ResourcePool 天然支持）。
6. **重复请求负载**：Gateway 并发时若每个请求都 loadCodeAssist 会造成额外开销 -> 应缓存 project/tier，定期或 401 时刷新。
7. **合规风险**：使用他人逆向的 OAuth client + 内部 endpoint 可能违反 Google ToS（与 Anonymous Vertex/Firebase 同样风险）。
8. gcli2api 为 CNC-1.0 协议（反商业），只能参考协议思路，**不可逐字复制代码**（尤其 OAuth/onboard 逻辑自行重写）。

---

## 17. Implementation Checklist

- [ ] TASK-007 research approved
- [ ] Create providers/geminicli/ package skeleton
- [ ] auth.py: Credentials + refresh (httpx, 401 force-refresh)
- [ ] onboard.py: loadCodeAssist + onboardUser + tier mapping + project caching
- [ ] protocol.py: envelope build {model, project, request} + response unwrap
- [ ] streaming.py: SSE line parser + chunk normalization (ChatChunk)
- [ ] errors.py: 429 quota cooldown parse -> RateLimitError + retry_after
- [ ] resource.py: GeminiCliResource (token/project/tier/preview/health)
- [ ] provider.py: non-stream + stream + model list providers
- [ ] factory.py + bootstrap registration
- [ ] standalone oauth-onboard script (browser once -> creds JSON)
- [ ] Tests: mock upstream (protocol/streaming/errors/onboard), no network
- [ ] Docs: TASK-007-IMPLEMENTATION.md

---

## 18. Final Decision

IMPLEMENT

**理由**：

1. 协议已被完整逆向并可由 httpx 原生实现（不需要 Node/CLI runtime）。
2. 认证 = OAuth refresh 流程，无头长期运行可行（一次性引导后）。
3. 一个 OAuth 账号 <-> 一个 cloudaicompanionProject 的 1:1 结构天然契合 ResourcePool 的 Resource 模型。
4. 客户端（Gateway）输出仍是 OpenAI API，不影响 SillyTavern。
5. 已有 Anonymous Vertex / Firebase 两种 backend 的前提下，GCLI 补齐第三种差异化通道（Google 官方 Code Assist 额度）。

**不引入 gcli2api**：其价值（web 面板、SQLite 多账号、converter、假流式、抗截断）与 Gateway 现有能力重复，只需提取协议层。

**Gate for TASK-007 implementation approval**：完成本报告 review；OAuth 凭证引导脚本与 Provider 分离；不引入浏览器依赖。

---

*Audit notes: all endpoint/envelope/credential facts above are cited from cloned source at HEAD cdbaf37 (2026-09-09); no Gateway production code was modified during this audit.*


