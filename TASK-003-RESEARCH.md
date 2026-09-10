
# TASK-003-RESEARCH.md — Firebase AI Logic Provider 源码研究与接入可行性评估

> 研究目标：先彻底研究 `ishalumi/firebase2api`，确认它实际实现的 Firebase AI Logic 调用链、认证方式、请求协议和可复用边界，再决定是否进入 Gemini Gateway。
>
> 依据：**源码优先**（本项目下载的 firebase2api `main` HEAD 完整源码），官方 Firebase 文档用于交叉验证。
> 本任务**未修改** Gateway Core / Anonymous Vertex / ProviderRegistry / ModelRegistry / Scheduler，未提交任何真实 credential。

---

## 0. 研究对象快照

| 项 | 值 |
| :--- | :--- |
| 仓库 | https://github.com/ishalumi/firebase2api （分支 `main`） |
| 组成 | **单一 Python 文件** `app.py`（约 880 行）+ 静态前端 `static/` + `accounts.json` 凭证文件 + Docker 可选 |
| 语言/运行时 | Python 3.11+（Docker 镜像 python:3.11-slim），FastAPI + httpx |
| 依赖 | `fastapi`, `uvicorn[standard]`, `httpx[socks]`, `python-dotenv`（requirements.txt，仅 4 个） |
| 角色 | 一个 **OpenAI→Gemini 的轻量 HTTP 转换网关**（自带账号池与管理面板） |
| 与 Gateway 的关系 | **同构**：firebase2api 本身就是另一个“Gateway”，暴露 `/v1/chat/completions` + `/v1/models` |

**一句话结论**：firebase2api **不是** 一个可被 Gateway 直接 import 的 Provider 库，而是一个**独立的、自包含的 OpenAI-compatible 网关服务**。它注册到 Google 上游的方式、认证流程、请求/响应/流式/错误/模型逻辑全部集中在 `app.py` 单文件内。

---

## 1. Executive Summary（结论速览）

1. **firebase2api = 一个独立网关，不是一个 Provider 库**。它自带 FastAPI server + Admin 面板 + `/v1/chat/completions` + `/v1/models`，与 Gemini Gateway 功能重叠。**不能**直接 import 进 Gateway 当 Provider；要么“返回一层”（Gateway→firebase2api 本地进程做 HTTP 上游），要么“下沉一层”（把 `app.py` 的认证/转换逻辑重写为 Gateway 的 FirebaseProvider）。
2. **使用 Firebase Web API Key + App Check（debug token → provider token）双认证**：先走 Firebase App Check 交换端点换发短期 JWT（`X-Firebase-AppCheck` header）；**不是** Google OAuth / Service Account / 用户 ID token。`X-Firebase-Appid` 必须来自同一 Firebase Web App（App Check token 绑定 App）。
3. **上游是 Firebase 官方 AI Logic API**（`firebasevertexai.googleapis.com/v1beta`，**不是** Vertex AI `aiplatform.googleapis.com`）。默认路径**不带 `locations/` 段**（刻意选择“免费短路径”）。
4. **Request body 与标准 Gemini `generateContent` 一致**（`contents` / `systemInstruction` / `generationConfig` / `tools` / `toolConfig` 原样构造；Gemini 字段名直接使用）。firebase2api **不丢掉** Gemini 字段——仅做 OpenAI 消息 → Gemini parts 的转换。
5. **Streaming 是真实 `streamGenerateContent`（SSE wire）→ OpenAI SSE**，firebase2api 已完整实现（`alt=sse` + `data:` 分行解析 + `[DONE]`），**流式优先级最高**（正符合 Gateway 的 streaming-first 设计）。
6. **错误处理是“池级重试”而非“分类”**：非 200 时统一转成字符串，401/403/429/5xx 循环换号重试 ≤3 次，404 直接 404，其余一律 502。**无法区分** quota 耗尽 / 项目禁用 / 模型不可用 / billing 等子类；**无 Retry-After 提取**，**无 provider/resource scope**，**无 cooldown 语义**。
7. **quota 模型**：按 **Firebase Project**（每项目每日/每分钟独立配额，README 明确“每个项目每天各模型有独立额度”）。因此理论上 **1 Firebase Project = 1 Resource**，多 Project 自然形成 Resource Pool——但这需要**保留**旧 credential（配置/环境变量允许），并**由 Gateway 统一解析 429 语义**。
8. **模型 Discovery = 硬编码**（`DEFAULT_MODELS` 6 个 + 别名表 + 抗截断前缀衍生），**没有**上游模型发现。firebase2api 不验证模型是否真实存在，靠 404 兜底。
9. **1H1G 可行性高**：4 依赖、纯 Python、无浏览器、无数据库、无重运行时；内存很小（每账号一个 httpx 池 + 一个 JWT 缓存）。
10. **最终建议：IMPLEMENTATION POSSIBLE WITH CONDITIONS**。可行性成立，但**不能“直接作为 Provider 复用”**——需要以 Gateway 原生 Provider 形式实现，把 `app.py` 的核心逻辑（认证、payload 构造、响应转换、流式解析）移植为 Gateway 模块，并补上 Anonymous Vertex 已具备、而 firebase2api 缺失的 4 件事：**ProviderError 分类、Retry-After 提取、cooldown 语义、credential 脱敏与可配置**。

---

## 2. 项目结构（源码确认）

`firebase2api` **是单文件应用**，其余为静态前端 / 示例 / 部署：

~~~text
firebase2api/
├── app.py                  # ★ 唯一核心：配置、账号池、认证、OpenAI↔Gemini、流式、错误、路由
├── accounts.example.json   # 凭证模板：api_key / project_id / app_id / debug_token / proxy
├── .env.example            # 环境变量模板（FBVTX_*）
├── requirements.txt        # fastapi / uvicorn[standard] / httpx[socks] / python-dotenv
├── Dockerfile              # python:3.11-slim，uvicorn
├── docker-compose.yml      # 端口 127.0.0.1:7861，挂载 accounts.json
├── run.sh                  # 本地 venv 启动脚本（创建 venv + 装依赖 + uvicorn）
├── examples/
│   ├── chat.py             # OpenAI SDK 示例（base_url=.../v1, api_key 任意）
│   └── chat.sh             # curl 示例
├── static/                 # Web 管理面板（app.js / index.html / styles.css）
└── tests/
    └── test_anti_trunc.py  # 抗截断合成工具（transport tool）单向测试
~~~

`app.py` 内部结构（按模块顺序）：

| 区块 | 行号(约) | 职责 |
| :--- | :--- | :--- |
| 配置加载 | 27–99 | FBVTX_* 环境变量、账号两种给法、模型列表、别名、DEBUG |
| 账号模型 | 103–199 | `Account`：api_key/project/app_id/debug_token/proxy + **App Check JWT** 缓存；账号解析/校验 |
| 管理面板后端 | 199–271 | 从 Firebase Web SDK 配置代码提取字段；accounts.json 原子写；admin token |
| 账号池 | 273–292 | Round-Robin `next_account()` |
| OpenAI → Gemini | 295–560 | parts 转换、tools(function calling)、anti-trunc 合成工具、generationConfig |
| Gemini → OpenAI | 563–633 | parts→(text,tool_calls)、finish_reason、usage |
| 流式 | 636–743 | `_sse_events` 解析、`stream_gemini`、`stream_failover` |
| 路由 | 746–867 | `/healthz` `/v1/models` `/admin/api/accounts` `/v1/chat/completions` |
| 入口 | 870–884 | uvicorn 启动，端口默认 7861 |

---

## 3. 一条完整请求的实际调用链（源码追踪）

~~~text
OpenAI client (base_url=http://127.0.0.1:7861/v1, api_key=任意非空)
   |  POST /v1/chat/completions
   v
firebase2api app.py chat_completions()
   |  1. 校验 body（invalid json → 400）
   |  2. resolve_model() 解析别名/-nothink/抗截断前缀
   |  3. build_gemini_payload() → Gemini JSON payload
   |  4. 取号 next_account()（round-robin）
   v
Account：两步认证（见 §4）
   |  ① POST https://firebaseappcheck.googleapis.com/v1
   |        /projects/{project_id}/apps/{app_id}:exchangeDebugToken?key={api_key}
   |        header: Content-Type, x-goog-api-client, x-goog-api-key
   |        body: { "debug_token": ..., "limited_use": false }
   |        ← 200: { "token": "<AppCheckProviderTokenJWT>", "ttl": "3600s" }   (缓存至过期前 300s)
   |  ② POST https://firebasevertexai.googleapis.com/v1beta
   |        /projects/{project_id}/models/{model}:generateContent        (非流式)
   |        /projects/{project_id}/models/{model}:streamGenerateContent?alt=sse   (流式)
   |        header:
   |          Content-Type: application/json
   |          x-goog-api-client: gl-js/@firebase/ai/2.15.0 fire/2.15.0
   |          x-goog-api-key: <Firebase Web API Key>
   |          X-Firebase-Appid: <Web App appId>
   |          X-Firebase-AppCheck: <AppCheckProviderTokenJWT>
   |        body: Gemini generateContent JSON
   v
Google: Firebase AI Logic API（firebasevertexai，无 billing 路径）
   v
Gemini backend（免费额度；模型由 project 配额决定）
   v
响应（200 非流式）→ gemini_to_openai() → OpenAI chat.completion JSON
响应（200 流式）→ stream_gemini() 逐 SSE 帧 → OpenAI SSE 帧（含 [DONE]）
非 200 → 归并成字符串 → 换号重试 ≤3 次 → 502 pool_exhausted
~~~

### 3.1 Request Endpoint（源码确认）

| 用途 | 端点 | Method | 来源 |
| :--- | :--- | :--- | :--- |
| App Check 换发 | `https://firebaseappcheck.googleapis.com/v1/projects/{pid}/apps/{app_id}:exchangeDebugToken` | POST | `EXCHANGE_URL` |
| 非流式 | `https://firebasevertexai.googleapis.com/v1beta/projects/{pid}/models/{model}:generateContent` | POST | `GEN_URL` |
| 流式 | `https://firebasevertexai.googleapis.com/v1beta/projects/{pid}/models/{model}:streamGenerateContent?alt=sse` | POST | `GEN_URL` + `params={"alt":"sse"}` |
| 模型列表 | **无**（硬编码） | – | 无路由 |
| Count Tokens | **无** | – | 无路由 |

### 3.2 HTTP Method / Headers / Body

- Method：`POST`（生成类全部）。
- Headers（`Account.sdk_headers()`，**redacted**）：

~~~text
Content-Type: application/json
x-goog-api-client: gl-js/@firebase/ai/2.15.0 fire/2.15.0
x-goog-api-key: <Firebase Web API Key (redacted)>
X-Firebase-Appid: <appId: 1:<projectNumber>:web:<hash>>
X-Firebase-AppCheck: <AppCheck Provider Token JWT (redacted)>
~~~

- Request body（`build_gemini_payload()`）= 标准 Gemini generateContent body：

~~~json
{
  "contents": [ { "role": "user", "parts": [{ "text": "..." }] } ],
  "systemInstruction": { "parts": [{ "text": "..." }] },
  "tools": [ { "functionDeclarations": [ { "name": "...", "description": "...", "parameters": {...} } ] } ],
  "toolConfig": { "functionCallingConfig": { "mode": "AUTO"|"NONE"|"ANY", "allowed_function_names": [...] } },
  "generationConfig": {
    "temperature": 0.7,
    "maxOutputTokens": 8192,
    "topP": 1.0,
    "stopSequences": [...],
    "thinkingConfig": { "thinkingBudget": 0 }   // BUDGET0，或 {"thinkingLevel":"LOW|MEDIUM|HIGH"}
  }
}
~~~

- **关键点**：firebase2api **保留** 标准 Gemini 字段名（`maxOutputTokens` / `topP` / `stopSequences` / `thinkingConfig` / `thoughtSignature`），所以 body 与 Gemini generateContent **一致**。它只负责 OpenAI 消息列表 → Gemini `contents`/parts 的转换，以及把 `temperature` 等映射进 `generationConfig`。
- **未使用**：`safetySettings`（firebase2api 不注入；保持默认）。而 Anonymous Vertex 注入固定 4×BLOCK_NONE。若 Firebase 默认安全策略会挡内容，Gateway 需要可配置项（TASK-003 只记录，不实现）。

---

## 4. 认证机制（TASK-003 核心）

### 4.1 firebase2api 实际使用的凭证

| 凭证 | 存在? | 用途 | 类型 |
| :--- | :---: | :--- | :--- |
| **Firebase Web API Key**（`api_key`） | 是 | ① App Check 交换请求的 `?key=` query + `x-goog-api-key` header；② AI Logic 请求的 `x-goog-api-key` header | Firebase API Key（匿名 key，形如 `AIzaSy...`） |
| **App ID**（`app_id`） | 是 | `X-Firebase-Appid` header；App Check 交换路径的 `apps/{app_id}` 段 | `1:<projectNumber>:web:<hash>` |
| **Debug Token**（`debug_token`） | 是 | App Check **Debug Provider** 的调试令牌，用于换取正式 Provider Token | UUID 字符串 |
| **App Check Provider Token (JWT)** | 是（运行时换取，非用户配置） | `X-Firebase-AppCheck` header | 短期 JWT，默认 ttl 3600s，缓存到过期前 300s |
| Project ID | 是 | URL 路径 `projects/{project_id}` | 字符串 |
| OAuth Access Token | 否 不存在 | 无 | – |
| Service Account | 否 不存在 | 无 | – |
| Firebase Auth ID Token | 否 不存在 | 无 | – |
| 用户 Google 登录 | 否 不存在 | 无 | – |

**Credential: api_key + app_id + debug_token（项目级）＋运行时 App Check JWT**
**Project: project_id（路径级，免费额度归属）**
**App: app_id（App Check 绑定的 Web App）**
**API Key: Firebase Web API Key（非 OAuth）**
**OAuth: 不存在**
**App Check: 存在，且是核心（debug token 交换 → provider token）**

### 4.2 关键确认：Firebase Web API Key vs Google OAuth credential

- **是两个完全不同的事物**：
  - **Firebase Web API Key**（`AIzaSy...`）：Firebase Console 生成的项目级匿名 key，标识项目、用于 App Check 交换与 AI Logic 请求鉴权。**不是** OAuth 凭据，**不是** Service Account。
  - **Google OAuth Access Token**：OAuth 2.0 授权流程产物，用于 Google 平台的用户授权访问。firebase2api **完全不使用**。
- **App Check token 才是真正的“门禁”**：`X-Firebase-AppCheck` 携带的短期 JWT 证明“请求来自被信任的 App Check 环境（这里是 Debug Provider）”。**API key 和 App Check token 二者缺一不可**（403 App attestation failed / 429 常见于 token、app 或配额不匹配）。

### 4.3 认证链路小结

~~~text
(api_key + app_id + debug_token)  → 持久配置（项目级）
        |  POST exchangeDebugToken（?key=api_key, x-goog-api-key）
        v
App Check Provider Token（JWT，ttl≈3600s，缓存）
        |  注入 X-Firebase-AppCheck + X-Firebase-Appid + x-goog-api-key
        v
Firebase AI Logic API 接受请求（免费额度按 project 结算）
~~~

### 4.4 每次请求实际发生的认证成本

- 首次：1 次 App Check 交换（~秒级）→ 缓存至过期前 300s（`jwt_exp > now+300` 才复用）→ 之后同账号免费复用。
- 401 → `force=True` 强制换发重试一次（`request()` 的 `for attempt in (0,1)`）。
- 结论：**认证开销极低**，适合 1H1G 长跑。

---

## 5. 认证 / Project / App / API Key / quota 的关系

~~~text
Firebase Account (Google)
        |
        +-- Firebase Project  (project_id, 免费额度载体 ★)
        |       |
        |       +-- projectNumber (1:<projectNumber>:...)  → 派生进 app_id
        |       +-- Web API Key  (apiKey: AIzaSy...)       → x-goog-api-key
        |       +-- Firebase Web App (appId: 1:<pn>:web:<hash>)
        |       |       +-- App Check: Debug Provider + Debug Token → Provider JWT
        |       |               +-- X-Firebase-Appid + X-Firebase-AppCheck
        |       +-- Gemini 免费配额 (per-project per-model — README 确认)
~~~

- **quota 归属于 Firebase Project**（README：每个项目每天各模型有独立额度；429 RESOURCE_EXHAUSTED 即“该项目当天免费配额用完”）。
- **一个 Firebase Project 可以、也应该作为 Gateway 的一个独立 Resource**。
- **多 Firebase Project 自然形成 Resource Pool**：每个 `Account`（= 一个 project 的 4 项凭证 + 可选 proxy）就是一个独立资源。firebase2api 已用 round-robin + 换号重试验证了这个结论。
- 注意：**App 与 quota 解耦** —— quota 看 project，App Check 看 app。若一 project 下多个 Web App 各配 debug token，只是多个“入口”共享同一项目配额，**不会增加额度**。
- **Proxy 每账号独立**（accounts.example.json 字段），与 Anonymous Vertex 的 per-resource proxy 设计一致，可直接复用。

---

## 6. Firebase AI Logic 真实架构（源码 + 已知官方文档交叉验证）

| 问题 | 结论 |
| :--- | :--- |
| firebase2api 是否用官方 Firebase API？ | 是，但**不是**官方 `firebase-admin` SDK，而是**手写 HTTP 协议层模仿 JS SDK**（`gl-js/@firebase/ai/2.15.0 fire/2.15.0` UA）。 |
| 实际 host？ | `firebasevertexai.googleapis.com`（Firebase AI Logic 专用 host），**不是** `aiplatform.googleapis.com`。 |
| 经过代理还是直连？ | **直连 Firebase AI Logic 代理**（它本身就是官方代理服务），firebase2api 只是它的客户端。 |
| 用 Gemini Developer API 还是 Agent Platform？ | **都不是显式选择**。固定用**无 `locations` 的短路径**（`/projects/{pid}/models/...`），注释明确：带 `locations/` 的新路径会转发到 aiplatform Agent Platform API 并要求 billing，本项目只走免费用量短路径。 |
| 能否切 backend？ | firebase2api 没有开关；切换 backend 需换 URL 模板（可在 Gateway 中做可配置）。 |

> 结论确认：firebasevertexai = Firebase AI Logic 代理，**不是** Vertex AI API。它按 project 免费额度代理 Gemini backend。官方支持的 backend（Gemini Developer API / Agent Platform Gemini API）由“带不带 `locations/` 段 + 是否 billing”决定；firebase2api 选了免费短路径。

---

## 7. Request Mapping（OpenAI → Gemini）

### 7.1 置信度图例
SUPPORTED / PARTIAL / UNSUPPORTED / UNKNOWN —— 均以源码为准。

| OpenAI 字段/特性 | firebase2api 行为 | 状态 | 证据 |
| :--- | :--- | :---: | :--- |
| `messages[].role=system` | 汇总进 `systemInstruction.parts[].text` | SUPPORTED | `convert_messages()` |
| `messages[].role=developer` | **静默丢弃**（`continue`） | UNSUPPORTED | `convert_messages()` |
| `messages[].role=user` (str) | → `contents[].parts=[{text}]` | SUPPORTED | `_content_to_parts()` |
| `messages[].role=user` (数组) | text / image_url(data-url) / input_audio → 对应 parts | SUPPORTED(partial) | `_content_to_parts()` |
| `messages[].role=assistant` content | → `role=model` parts | SUPPORTED | `convert_messages()` |
| `messages[].role=assistant` tool_calls | → `functionCall` parts（带 pending name 队列） | SUPPORTED | `convert_messages()` |
| `messages[].role=tool` | → `functionResponse`（content 字符串 try-JSON 解析） | SUPPORTED | `convert_messages()` |
| `image_url` data URL | → `inline_data`（仅 base64 data URL；外链/URL 报错） | PARTIAL | `_data_url_to_inline()` |
| `input_audio` | → `inline_data`（mime=format） | SUPPORTED | `_content_to_parts()` |
| `tools` (function) | → `tools=[{functionDeclarations:[{name,description,parameters}]}]` | SUPPORTED | `build_gemini_payload()` |
| `tool_choice` | AUTO / NONE / {type:function,name} → ANY(+allowed) | SUPPORTED | `build_gemini_payload()` |
| `response_format` / JSON mode | **无处理**（不映射） | UNSUPPORTED | 全文件 grep 无 response_format |
| `reasoning_effort` low/med/high | → `thinkingConfig.thinkingLevel` | SUPPORTED | `build_generation_config()` |
| `thinking` (非标 0/none/BUDGET0/…) | → `thinkingConfig.thinkingBudget=0` 或 level | SUPPORTED(非标) | `build_generation_config()` |
| 无 thinking 且模型是 `gemini-3.7-flash` | 强制兜底 `FBVTX_DEFAULT_THINKING`（默认 LOW）防“黑洞休眠” | SUPPORTED(私有行为) | `build_generation_config()` |
| `n`（候选数） | **无处理**（仅取 candidates[0]） | UNSUPPORTED | `gemini_to_openai()` |
| `seed` / `logprobs` / `presence_penalty` / `frequency_penalty` | **无处理**（丢弃） | UNSUPPORTED | 无代码 |
| `stream` | 走流式分支 | SUPPORTED | `chat_completions()` |
| `max_tokens` / `max_completion_tokens` | → `maxOutputTokens` | SUPPORTED | `build_generation_config()` |
| `temperature` | → `temperature` | SUPPORTED | 同上 |
| `top_p` | → `topP` | SUPPORTED | 同上 |
| `stop` | → `stopSequences` | SUPPORTED | 同上 |
| `safetySettings` | 不注入（用默认） | UNKNOWN(建议可配置) | 无代码 |

### 7.2 消息转换细节
- system 消息**拼接**（多 system 合并为一段，`"".join(...)`）。
- assistant 的 tool_calls → 连续 `functionCall` parts，name 以 **FIFO 队列**与后续 tool 消息配对。
- tool 消息非对象内容 → `{"result": str}` 包装，失败 try-JSON。
- 空 assistant 内容 → `{"role":"model","parts":[{"text":""}]}`（占位）。
- **抗截断（`抗截断-<model>`）是 firebase2api 独有增强**：注入 `emit_content_here` 合成工具 + SYSTEM DIRECTIVE，要求模型把完整正文写进工具参数，响应侧自动还原为 `assistant.content`，并在下一轮用 `thoughtSignature` 闭合 functionCall/functionResponse（全局可变 `_ANTI_TRUNC_SIGNATURE`，进程内缓存）。这是为规避上游输出截断设计的 hack，**不建议**进 Gateway 默认列表（可作可选实验特性）。

---

## 8. Response Mapping（Gemini → OpenAI）

| Gemini 字段 | OpenAI 字段 | 状态 | 证据 |
| :--- | :--- | :---: | :--- |
| `candidates[0].content.parts[].text` | `choices[0].message.content`（或多 text part 拼接） | SUPPORTED | `_parts_to_openai()` |
| parts[].functionCall | `choices[0].message.tool_calls[].function{name,arguments}`（id 随机 `call_uuid`，arguments JSON 字符串） | SUPPORTED | `_parts_to_openai()` |
| parts[].functionCall(args.content)（抗截断工具） | 合并进 `message.content`，不暴露 tool_calls | SUPPORTED(私有) | `_parts_to_openai()` |
| parts[].thought / thoughtSignature | **丢弃**（不进 OpenAI 响应） | UNSUPPORTED(默认) | 无映射 |
| `candidates[0].finishReason` | `finish_reason` 映射：STOP→stop, MAX_TOKENS→length, SAFETY/RECITATION→content_filter, 其他→stop | SUPPORTED | `_finish_reason()` |
| `usageMetadata.promptTokenCount` | `usage.prompt_tokens` | SUPPORTED | `gemini_to_openai()` |
| `usageMetadata.candidatesTokenCount`(+thoughtsTokenCount) | `usage.completion_tokens` | SUPPORTED | 同上 |
| `usageMetadata.totalTokenCount` | `usage.total_tokens` | SUPPORTED | 同上 |
| `id` | `chatcmpl-<uuid>`（随机） | SUPPORTED(伪造) | 同上 |
| 多候选（candidates[n>0]） | **丢弃**（只取 [0]） | UNSUPPORTED | 同上 |
| 其他 Gemini 富字段（citation/grounding/executableCode…） | **丢弃** | UNSUPPORTED(默认) | 无映射 |

---

## 9. Streaming（wire format）

| 项 | 结论 |
| :--- | :--- |
| 是否支持？ | 是，`streamGenerateContent` 原生流式 |
| upstream wire format | **SSE**（`params={"alt":"sse"}`）。`_sse_events()` 按行读取，只取 `data:` 前缀行（忽略空行/`[DONE]`）。 |
| OpenAI 输出格式 | **SSE**（`data: {json}` + 空行，结尾 `data: [DONE]`）；`media_type=text/event-stream` |
| 流式帧映射 | 每帧取 `candidates[0].content.parts` → `delta.content` / `delta.tool_calls`；首个正文前发 `delta{role:assistant}`；`finishReason` 出现即时发 `finish_reason`；结束补 `finish_reason:stop` + `usage` + `[DONE]` |
| 工具流 | 首个 tool_calls 前发 `delta{role:assistant,tool_calls:[]}`，随后逐 index 增量 |
| 抗截断流 | 对 synthetic functionCall 重复返回做**去重/增量**（`synthetic_emitted` 前缀裁剪） |
| 是否已实现 OpenAI-compatible SSE？ | 是（`stream_failover` 做 ≤3 号池级 failover） |
| 是否可直接 Adapter 化？ | **不直接**：firebase2api 内嵌于其 FastAPI handler，无独立可 import 的流式库；但其解析逻辑（`_sse_events` + parts→delta 映射）约 60 行，可直接改写成 Gateway 的 FirebaseStreamParser |

> 关键差异：Anonymous Vertex 上游是 **NDJSON（brace-counting）非 SSE**；firebase2api 上游是 **SSE**。两者解析器**不可共用**，但**输出到 OpenAI SSE 的部分**（`format_sse`, `DONE_SSE`）可复用 `protocol/common.py`。

---

## 10. 错误映射（尤其 429）

### 10.1 firebase2api 实际行为（源码确认）

- **非流式**：`Account.request()` 200→转换；404→`404 model_not_found`；401/403/429/500/502/503 → 记住字符串，**换下一账号重试**（≤ min(账号数,3)）；其他状态 → 502 `upstream_error`；全部失败 → 502 `pool_exhausted`。
- **流式**：首 chunk 前任何异常（429/403/连接错误/黑洞超时）→ 换号重试 ≤3；全失败 → 502 SSE `{"error":...,"type":"pool_exhausted"}` + `[DONE]`。
- **JWT 交换失败**：`RuntimeError("jwt exchange fail ...")` → 进 502 `pool_exhausted`（**未分类**为认证错误）。
- **上游 429 body**：未解析结构，仅取前 300 字符作字符串（README 典型为 `RESOURCE_EXHAUSTED`，Google gRPC 状态语义）。
- **Retry-After**：**未提取、未透传**（无任何处理）。

### 10.2 建议映射（Gateway ProviderError，TASK-003 只记录建议，不实现）

| 上游 | 识别 | 建议 Gateway 错误 | scope | cooldown? | disable? |
| :--- | :--- | :--- | :--- | :---: | :--- |
| 429 | `RESOURCE_EXHAUSTED` + quota 字样 | `RateLimitError` | `resource`（=该 project） | 是（Retry-After 或指数退避） | 若项目级日额度耗尽 → 建议 **disable 该 Resource**（当日不可用） |
| 429 | 仅 `RESOURCE_EXHAUSTED`（无法细分） | `RateLimitError` | `resource` | 是 | 保守：cooldown，不 disable（无法可靠区分） |
| 429 | `RATE_LIMIT_EXCEEDED`/RPM 类 | `RateLimitError` | `resource` | 是（短） | 否 |
| 401 | JWT 交换失败 / `UNAUTHENTICATED` | `AuthenticationError` | `resource` | 否 | 是（credential 问题，重试无意义） |
| 403 | `App attestation failed` / `PERMISSION_DENIED` | `AuthorizationError` | `resource` | 否 | 视情况（App Check 绑定失败 → disable；billing 相关 → 提示） |
| 404 | model not available | `ModelNotFoundError` | 请求 | 否 | 否 |
| 400 | 非法请求 | `InvalidRequestError` | 请求 | 否 | 否 |
| 429/5xx/网络/超时 | 通用 | `RateLimitError(429)` / UpstreamUnavailable / NetworkError / TimeoutError | `unknown` | 是 | 否 |
| 503 | project/服务禁用 | `UpstreamUnavailableError` | `resource` | 是 | 若持续 → disable |

- **无法可靠区分的组合**：RPM exceeded / Daily quota exceeded / Project disabled / Authentication failed / Model unavailable / Billing required —— firebase2api 全部归并成字符串，**仅靠 429 状态码+消息文本无法可靠区分**。Gateway 若要可靠区分，需**实测抓取不同 429 body 样本**建立分类器，或本地维护 per-project quota 状态机（推荐）。

### 10.3 与 Anonymous Vertex 对比
- Anonymous Vertex：已实现 **Retry-After 提取** + **井井有条的 ProviderError 分类** + **cooldown 语义**（core/cooldown.py）。
- firebase2api：仅池级重试，**无 Retry-After / scope / cooldown**。
- → Gateway 接入时，**必须**由新 Firebase Provider 实现这些（复用 core/errors.py 与 core/cooldown.py 即可）。

---

## 11. Quota / Resource 模型

| 问题 | 答案 |
| :--- | :--- |
| quota 属于谁？ | **Firebase Project**（README：每个项目每天各模型独立额度；429 RESOURCE_EXHAUSTED = 当日免费配额用完）。App Check / App 不增加额度。 |
| 1 个 Firebase Project 能否作为 1 个 Resource？ | 可以。Resource 需要：project_id + api_key + app_id + debug_token（+可选 proxy）。 |
| 多 Project 能否自然形成 ResourcePool？ | 可以。firebase2api 的 round-robin 池就是**同一结论的实证**：每个 `Account` × 独立配额 × 独立 proxy。 |
| 前提/条件 | （1）**保留旧凭证**（原账号轮换后旧 Project 仍在池中，配额独立累计）；（2）Gateway 统一解析 429 → `RateLimitError(resource_id=project)`；（3）Resource 生命周期与 Gateway 共存，无数据库即可（config 文件 + env）。 |
| per-model per-project 细分 | firebase2api 未细分（model 级 429 与 project 级 429 无法从代码区分）。Gateway 可把 `resource_id` 定为 project，把 model 放进 message/scope 供调度参考。 |
| 建议 Resource 身份 | `resource_id = firebase-project-<name>`，`ProviderError.resource_id` 传 project_id。 |

---

## 12. Model Discovery

| 项 | 结论 |
| :--- | :--- |
| firebase2api 是否 GET models？ | 否，**无**任何 models 列表请求。 |
| 机制 | **硬编码** `DEFAULT_MODELS`（6 个）：`gemini-3.8-flash` / `gemini-3.8-flash-nothink` / `gemini-3.7-flash` / `gemini-3.7-flash-nothink` / `gemini-3.6-flash` / `gemini-3.5-flash`；+ 别名表（FBVTX_MODEL_ALIASES）；+ 每个模型衍生的 `抗截断-<model>`。 |
| gemini-3.7/3.8 如何被识别？ | 纯字符串匹配：`resolve_model()` —— 别名 → 默认表 → `models/xxx` 去前缀 → 含 `/` 去前缀 → 原样透传。**不验证模型存在性**，靠上游 404 兜底。 |
| 验证 | 若 Gateway 默认列表加入这些模型，**必须**以实际 endpoint / backend 支持情况为准（firebase2api 只是“广告”它们，未探测）。当前 Gateway 也已在 anonymous_vertex TEXT_MODELS 中列出 gemini-3.7/3.8-flash——两 Provider 的模型表**概念一致**，但**既不能证明存在、也不能证明额度**。 |
| 建议 | FirebaseProvider.list_models() 用配置快照（类 anonymous_vertex），**不**做上游 GET；把 404/模型不可用 → `ModelNotFoundError` 交给调度器。 |

---

## 13. OpenAI 兼容度总评（SUPPORTED / PARTIAL / UNSUPPORTED / UNKNOWN）

| 特性 | 等级 |
| :--- | :--- |
| system message | SUPPORTED |
| user message | SUPPORTED |
| assistant message | SUPPORTED |
| multimodal content (image base64 / audio) | PARTIAL（仅 data URL / inline；无外链 fetch） |
| tool calls | SUPPORTED |
| function calling (tools/tool_choice) | SUPPORTED |
| response format / JSON mode | **UNSUPPORTED** |
| thinking / reasoning（reasoning_effort） | SUPPORTED（LOW/MEDIUM/HIGH→thinkingLevel；BUDGET0→thinkingBudget=0） |
| usage | SUPPORTED（prompt/completion/total） |
| finish_reason | SUPPORTED（STOP/MAX_TOKENS/SAFETY/RECITATION 映射） |
| developer message | **UNSUPPORTED**（丢弃） |
| n>1 | UNSUPPORTED |
| seed/logprobs/penalty | UNSUPPORTED |
| 抗截断（`抗截断-`） | 独有增强（可选实验特性，不建议默认） |

---

## 14. 与 Anonymous Vertex 的代码复用分析

### 14.1 复用边界

| Gateway 组件 | firebase2api 对应物 | 可复用? | 说明 |
| :--- | :--- | :---: | :--- |
| `core/errors.py` ProviderError 体系 | 无分类（字符串） | 直接复用 | Firebase Provider 直接 import `RateLimitError` 等 |
| `core/cooldown.py` | 无 cooldown（池级重试） | 直接复用 | Retry-After + 指数退避 |
| `core/resource.py` Resource 基类 | `Account` | 直接复用 | FirebaseResource 继承 Resource，字段=project 凭证 |
| `core/models.py` ChatRequest/ChatChunk/ChatResponse | 手写 dict 转换 | 直接复用 | Provider 输出这些内部对象 |
| `protocol/common.py` (format_sse/DONE_SSE) | `sse_chunk()` | 直接复用 | 输出侧 SSE 一致 |
| `providers/anonymous_vertex/transport.py` HTTPTransport | httpx 直连 | 直接复用 | Gateway 已有抽象 |
| `providers/anonymous_vertex/errors.py` 分类模式 | 无 | 复用“模式”（重写映射表） | 上游仍是 Google/gRPC 风格 |
| `protocol/gemini.py` | 无（内嵌） | 建议未来抽象 | `to_internal_response/chunk` 现 NotImplemented，TASK-003 不实现 |
| firebase2api 的 SSE 上游解析（`_sse_events`） | 自身 | 重写为新模块（约 60 行） | 与 Anonymous Vertex 的 NDJSON **不可共用**；新写 `FirebaseStreamParser` |
| firebase2api 的 OpenAI→Gemini 转换 | 自身 | 重写为协议模块 | 逻辑可借鉴，需对齐 Gateway `core/models.py` 并补 JSON mode 缺口 |
| firebase2api 的 JWT 交换 | 自身 | 重写为 `FirebaseAuth` | 带缓存 + 401 强制刷新逻辑移植 |
| firebase2api 的模型硬编码表 | 自身 | 改为配置快照 | 不照抄；与 anonymous_vertex 同源思路（config 出身） |

### 14.2 建议复用 / 建议未来抽象 / 暂不抽象

- **建议复用**：core/errors、core/cooldown、core/resource、core/models、protocol/common、transport 抽象、anonymous_vertex 的 error 分类**模式**。
- **建议未来抽象（TASK-003 不做重构）**：`protocol/gemini.py` 的 Gemini→内部模型转换（两 Provider 各自现写，未来统一）。
  - 理由：Anonymous Vertex 上游是 NDJSON/GraphQL 信封，Firebase 上游是 SSE/标准 Gemini REST，共享 Gemini 内容模型**可行但收益有限**，现阶段若抽象反而拖慢落地。
- **暂时不要抽象**：firebase2api 的 `emit_content_here` 抗截断机制（依赖进程内全局可变签名、跨轮闭合，与 Gateway 无状态调度相冲突，暂不纳入）。

---

## 15. 1H1G 可行性评估

| 维度 | 评估 |
| :--- | :--- |
| CPU | 极低。纯 IO（httpx）+ 简单 JSON；无重计算。 |
| RAM | 很低。每账号 1 个 httpx 连接池 + 1 个 JWT 字符串缓存；FastAPI/uvicorn 常驻约 50–150MB。 |
| 依赖数量 | 仅 4 个 Python 包，**无浏览器**（Chromium 否）、无数据库、无重运行时。 |
| Node/Python runtime | Python 3.11+；Gateway 本身 Python 3.12+，兼容。 |
| 后台进程 | 1 个 uvicorn 进程即可；两形态都只需 1 进程（原生 Provider 甚至 0 额外进程）。 |
| 缓存 | 仅内存（JWT 缓存），无持久缓存。 |
| 数据库 | 无（accounts.json 可选持久化；Gateway 可用 config/env 替代）。 |
| 浏览器 | 仅管理面板前端（可选）；Gateway 不需要。 |
| **结论** | **可长期运行于 1H1G**。无论“Gateway→firebase2api 本地适配”还是“Gateway 原生 FirebaseProvider”，都满足轻量约束。 |

---

## 16. 推荐 Adapter 架构

### 16.1 候选形态对比

| 形态 | 说明 | 1H1G 成本 | 维护 | 结论 |
| :--- | :--- | :---: | :---: | :--- |
| **A. Gateway 原生 FirebaseProvider（推荐）** | 把 `app.py` 的 4 块逻辑（JWT 认证、payload 构造、响应转换、SSE 解析）移植为 Gateway `providers/firebase/` 模块；错误分类/cooldown 用 core。 | 最省（无第二进程） | 好（复用 Gateway 调度） | 推荐 |
| B. Gateway→firebase2api 本地进程适配 | firebase2api 作为本地 HTTP 上游（127.0.0.1:7861），Gateway 新增 “local adapter” Provider | 多 1 进程（仍 1H1G 可行） | 差（双网关、两套轮询/错误语义打架） | 可行但不推荐 |
| C. 直接 import firebase2api 当 Provider | 不可能：它是 FastAPI app 不是库 | – | – | 否 |

### 16.2 推荐架构（原生 Provider）

~~~text
FirebaseProvider (providers/firebase/provider.py)
   | 实现 Provider: list_models / complete / stream / health_check
   +-- FirebaseResource (resource.py)      # 继承 core.Resource；字段: project_id, api_key, app_id, debug_token, proxy
   +-- FirebaseAuth (auth.py)              # App Check debug token → JWT（缓存 + 401 强制刷新）★ 移植自 app.py Account.get_jwt
   +-- FirebaseClient (client.py)          # URL/headers 构造 + POST + 401 刷新重试 ★ 移植自 app.py Account.request/sdk_headers
   +-- FirebasePayloadBuilder (payload.py) # OpenAIChatRequest → Gemini generateContent payload ★ 移植自 app.py
   +-- FirebaseResponseParser (response.py)# Gemini response → ChatResponse（usage/finish_reason/tool_calls）★ 移植自 app.py
   +-- FirebaseStreamParser (streaming.py) # SSE 上游逐帧 → ChatChunk ★ 重写自 app.py（上游是 SSE，勿复用 NDJSON 扫描器）
   +-- FirebaseErrorMapper (errors.py)     # 上游 400/401/403/404/429/5xx → core ProviderError；429 提取 Retry-After ★ 借鉴 anonymous_vertex/errors.py
~~~

代码归属：
- **留在 Gateway**：Provider/Resource/Factory（注册进 ProviderRegistry）、错误分类、cooldown、流式到 OpenAI SSE、调度、健康检查、凭证脱敏日志。
- **离开 Gateway**：firebase2api 的整个 server（FastAPI app、`/v1/*`、`/admin/*`、`static/` 前端、账号池 round-robin、池级重试）—— 这些职责 Gateway 已有，**不复制**。
- **不复制**：`accounts.json` 运行时热更新机制、Docker/compose、抗截断 hack、双网关轮询。

---

## 17. 风险 / 未知项（UNKNOWN 清单）

1. **UNKNOWN**：Firebase AI Logic 免费额度的精确 RPM/RPD 值（README 只确认 per-project per-model 存在“当日额度”，未给数字；“5 RPM/20 RPD”的经验值**未在源码确认**，需实测或官方配额文档）。
2. **UNKNOWN**：429 的具体 JSON 结构（是否含 `Retry-After` / `retryInfo` / `quotaExceeded` 细分字段）—— 源码未解析，也未提供 fixture；需真实 429 抓包才能可靠分类。**当前无法可靠区分 RPM / 日额度 / 项目禁用 / 认证失败 / 模型不可用 / billing。**
3. **UNKNOWN**：免费短路径（无 `locations`）当前是否仍被 Google 接受（上游策略可能变更；firebase2api 版本即本项目快照）。
4. **UNKNOWN**：`gemini-3.7/3.8-flash` 在这些 Firebase 项目上的真实可用性与额度（firebase2api 仅“广告”，未探测；需实测）。
5. **UNKNOWN (documented)**：App Check Debug Provider 令牌有效期/策略（Debug Provider 通常长期有效，但 Google 可撤；`ttl` 响应可解析）。
6. **UNKNOWN**：safetySettings 默认策略是否产生隐藏拦截（firebase2api 不注入 safety；若默认挡内容，Gateway 需可配置项）。
7. **UNKNOWN**：官方文档精确描述（Firebase AI Logic 文档 / REST 参考），因当前环境无网络访问，未直接抓取；以**源码为准**（host/协议已从源码确认；文档仅交叉验证，源码证据充分）。
8. **风险**：若 Google 收紧 App Check Debug Provider（生产必需真 attestation），免费入口可能失效。此为**上游生态风险**，非代码风险。
9. **风险**：Firebase Web API Key 若未设置域名/用途限制会暴露；Gateway 必须**把 api_key/debug_token 视为 secret**，走 env 占位符（config.yaml.example 已示范），日志必须脱敏（firebase2api `log_request_raw` 仅前 20 字符，可借鉴）。

---

## 18. Final Decision

# IMPLEMENTATION POSSIBLE WITH CONDITIONS

**理由**：
- 1H1G 可行；依赖轻（4 包/无浏览器/无数据库）；协议为普通 HTTP(S)（POST + JSON + SSE），符合“原生 Provider”条件。
- 上游是官方 Firebase AI Logic API（firebasevertexai），认证流程明确（Web API Key + App Check debug token 换 JWT）。
- request/response/streaming/模型/错误全链路已从源码确认；与 Anonymous Vertex 的复用边界清晰。
- 但**不能把 firebase2api 直接当作 Provider 复用**：它是独立网关。需要按下述条件在 Gateway 内实现原生 FirebaseProvider。

**实施条件（进入 TASK-004 实现前必须满足）**：
1. 以 **A 形态（原生 Provider）** 实现，不复制 firebase2api 的 server/admin/pool。
2. FirebaseProvider 必须补齐 firebase2api 缺失的 4 件事：ProviderError 分类、Retry-After 提取、cooldown 语义、credential 全程脱敏与 env 占位符注入。
3. 429 分类策略：**先按“resource(project)”统一 RateLimitError + cooldown**；在拿到真实 429 样本前**不做** RPM/日额度/禁用的硬编码细分（写入代码注释与文档）。
4. Resource = 1 Firebase Project（project_id+api_key+app_id+debug_token+proxy）；多 Project = ResourcePool。**凭证必须留存**（Config 层），否则换号后旧配额不可恢复。
5. 模型默认列表**不照抄** firebase2api 硬编码表；以官方 endpoint/backend 支持情况 + 实测 404 为准（当前可复用 anonymous_vertex 同源模型列表思路：config 快照）。
6. 把“`抗截断-`”与 `emit_content_here` 排除在默认范围外（可选实验特性，不进核心默认列表）。
7. 不引入 firebase2api 的 accounts.json 热更新 / Admin 面板；Gateway 用 config/env 统一管理（后续管理 API TASK-011 再议）。
8. 保持 TASK-003 的禁止事项：不修改 Gateway Core / Anonymous Vertex / ProviderRegistry / ModelRegistry / Scheduler 抽象（新增 Provider 模块 + bootstrap 注册即可）。

---

## 附：源码证据索引（供审计，← 为行号来源）

| 结论 | 证据（app.py 行号/片段） |
| :--- | :--- |
| 独立网关（FastAPI + /v1/*） | L99 `app = FastAPI(...)`；L746–867 路由 |
| 上游 host/URL | L37 `EXCHANGE_URL` / `GEN_URL`；L150–151、L664–665 |
| 认证 4 件套 header | L169–175 `sdk_headers()` |
| App Check 交换 | L127–143 `get_jwt()`；L128 `exchangeDebugToken`；L135 `limited_use` |
| 免费短路径（无 locations） | L111–113 注释；L150–151；L664–665 |
| 请求 body 与 Gemini 一致 | L500–560 `build_gemini_payload()`；L458–497 `build_generation_config()` |
| 流式 SSE | L637–649 `_sse_events()`；L665 `alt=sse`；L656–723 `stream_gemini()`；L723 `[DONE]` |
| 错误=池级重试 | L857–867 非流式；L726–743 `stream_failover()` |
| 无 Retry-After | 全文件无 Retry-After / retry_after |
| 模型硬编码 | L60–67 `DEFAULT_MODELS`；L283–292 `resolve_model()`；L752–757 `/v1/models` |
| quota=project | README FAQ（429 RESOURCE_EXHAUSTED → 换项目）；无代码级 quota 管理 |
| 抗截断 hack | L68–75、L338–340、L390–418、L508–511、L527–540、L575–588、L693–699 |
| 能力边界（developer 丢弃/json mode 缺失） | L384–385 `role=="developer": continue`；全文件 grep 无 response_format |
| 无 billing 设计 | L6 模块 docstring；L112–113；“只面向免费额度” |

