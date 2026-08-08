# multi-ai-quiz · 答案质量评审台

将同一份「问题 + 两个答案」并行发给多个大模型，让它们按统一标准评审哪个答案更优，并以标签页形式并排展示各模型的评审结论、打分明细与原始输出。

项目名沿用了仓库名 `multi-ai-quiz`（多模型对答案的"评判/评审"），页面标题为「答案质量评审台」。

## 功能特性

- **多模型并行评审**：同一问题与两个答案并发发给所选模型，SSE 流式逐个返回结果
- **多协议兼容**：统一走 opencode-go 网关，支持 OpenAI 兼容 `chat`、Anthropic Messages `anthropic`、OpenAI Responses `responses` 三种协议族
- **结构化评审输出**：提示词要求模型输出 JSON（胜者、三维度胜负、1–5 打分、优势/问题/存疑事实、置信度、理由），前端解析渲染；非结构化输出原样展示
- **模型探活**：一键检测所有模型可用性，页面上直接标注可用/不可用及错误原因
- **可定制提示词**：默认模板来自 `prompt.yaml`，页面上可临时修改（`{question}` / `{answer_a}` / `{answer_b}` 占位符自动替换）
- **实时模型发现**：配置 API key 后优先从 `{base}/models` 拉取模型列表（5 分钟缓存），yaml 负责补充显示名、协议族与默认勾选
- **历史记录**：每次评审自动落库 SQLite，支持分页列表、查看详情、删除
- **轻量鉴权**：单访问密钥登录，签发 HttpOnly 会话 Cookie
- **深色模式**：前端自动适配系统主题

## 目录结构

```
.
├── app/
│   ├── __init__.py
│   ├── main.py        # FastAPI 入口：鉴权、模型列表、评审 SSE、历史接口
│   ├── config.py      # .env + models.yaml + prompt.yaml 配置加载
│   ├── gateway.py     # 多模型 LLM 网关：三种协议族调用、探活、重试、JSON 解析
│   └── history.py     # SQLite 评估历史持久化
├── static/
│   ├── index.html     # 评审工作台（模型勾选/探活、流式评审、历史记录）
│   └── login.html     # 登录页
├── models.yaml        # 模型清单（id/显示名/协议族/温度/默认勾选）
├── prompt.yaml        # 默认评审提示词模板
├── requirements.txt   # Python 依赖
├── .env.example       # 环境变量示例（复制为 .env 使用）
├── .gitignore
└── history.db         # SQLite 数据库（运行时生成，不入库）
```

## 快速开始

要求 Python 3.10+。

```bash
# 1. 创建虚拟环境并安装依赖
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# 2. 配置环境变量
cp .env.example .env             # 填入 OPENCODE_API_KEY

# 3. 启动服务
uvicorn app.main:app --reload    # 默认 http://localhost:8000
```

浏览器打开 http://localhost:8000，使用访问密钥登录（见下文「鉴权」）。

## 配置说明

### 环境变量（`.env`）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `OPENCODE_BASE_URL` | `https://api.opencode-go.com/v1` | opencode-go 网关地址，是 `/chat/completions`、`/messages`、`/responses` 等端点的前缀 |
| `OPENCODE_API_KEY` | 空 | API 密钥。为空时模型列表退回 `models.yaml`，且不会尝试实时发现 |
| `OPENCODE_DB` | `./history.db` | SQLite 数据库路径 |
| `OPENCODE_MODELS_YAML` | `./models.yaml` | 模型清单路径（可选覆盖） |
| `OPENCODE_PROMPT_YAML` | `./prompt.yaml` | 提示词模板路径（可选覆盖） |

`.env` 已在 `.gitignore` 中排除，请勿提交。

### 模型清单（`models.yaml`）

```yaml
models:
  - id: deepseek-v4-flash      # 模型 ID（网关调用时使用）
    name: DeepSeek V4 Flash    # 页面显示名
    api: chat                  # 协议族：chat | anthropic | responses
    temperature: 0.2           # 可选，仅 chat 族使用；缺省=不发送该参数
    enabled: true              # 页面默认勾选
```

说明：

- **协议族 `api`**：`chat` = OpenAI 兼容 `/chat/completions`（Bearer 认证）；`anthropic` = `/messages`（`x-api-key` + `anthropic-version: 2023-06-01`，`max_tokens` 固定 4096）；`responses` = `/responses`（Bearer 认证，`input` 字段）
- **`temperature`**：仅 `chat` 族生效。推理模型（如 `kimi-k3`、`kimi-k2.7-code`）只接受 `temperature=1`，缺省则完全不发送该参数
- **实时发现**：配置了 API key 且 `{base}/models` 可访问时，页面模型列表优先来自实时接口（5 分钟缓存）；yaml 中的条目按 `id` 匹配补充显示名、协议族、默认勾选。实时接口不可用时退回纯 yaml 列表
- 协议族未知的实时模型会按 `id` 前缀猜测（`minimax-*`、`qwen3*` → anthropic，`gpt-5.6-luna` → responses，其余 → chat），可在 yaml 中用 `api` 字段覆盖

### 提示词模板（`prompt.yaml`）

```yaml
template: |
  你是一名严谨的答案质量评审员。……
```

- 默认模板要求模型输出结构化 JSON：`winner`（a/b/tie）、`dimensions`（正确性/严谨性/简洁 三维度胜负）、`scores`（双答案各维度 1–5 打分）、`strengths` / `problems` / `suspect_facts`、`confidence`、`rationale`
- 模板中 `{question}` / `{answer_a}` / `{answer_b}` 占位符会被自动替换；若模板不含这三个占位符，问题与两个答案会自动追加到末尾
- 页面「评审提示词」折叠区内可临时覆盖模板（仅当次请求生效）

## 鉴权

- 访问密钥 `ACCESS_KEY` 硬编码在 `app/config.py`（`MDAwNjA5`），需要时直接修改该处
- `POST /api/login` 校验密钥后签发 HttpOnly 会话 Cookie `quiz_session`（HMAC 签名值，30 天有效）
- 除 `/login`、`/api/login`、`/static/*` 外的所有路径均需登录：API 返回 `401`，页面重定向到 `/login`
- `POST /api/logout` 注销

> 安全提示：会话 Cookie 未设置 `Secure` 标志，生产环境请部署在 HTTPS 反向代理之后；访问密钥建议部署前修改。

## API 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/login` | 登录，body `{"key": "..."}`，成功签发 Cookie |
| POST | `/api/logout` | 注销 |
| GET | `/api/models` | 模型列表（实时发现优先，5 分钟缓存） |
| GET | `/api/prompt` | 默认提示词模板，返回 `{"template": "..."}` |
| POST | `/api/check` | 并发探活所有模型，返回 `{"results": [{"model", "ok", "error?"}]}` |
| POST | `/api/evaluate` | 多模型并发评审，SSE 流式返回 |
| POST | `/api/evaluate/one` | 单模型评审（`models` 必须恰好一个），JSON 返回 |
| GET | `/api/history?limit=&offset=` | 历史列表（`limit` 默认 20、上限 200），含判定分布汇总 |
| GET | `/api/history/{id}` | 历史详情（问题、双答案、提示词、模型、各模型结果） |
| DELETE | `/api/history/{id}` | 删除历史记录 |

### 评审请求体

```json
{
  "question": "……",
  "answer_a": "……",
  "answer_b": "……",
  "models": ["deepseek-v4-flash", "glm-5.1"],
  "prompt": "可选，覆盖默认模板"
}
```

`models` 缺省时使用 `enabled: true` 的模型。校验规则：`question`、`answer_a`、`answer_b` 非空；`models` 必须是已知模型；至少一个模型。

### SSE 评审流（`POST /api/evaluate`）

所选模型并发调用（单次超时 60 秒；5xx 自动重试一次，间隔 1 秒），每个模型的结果到达即推出一条事件：

```text
data: {"model": "deepseek-v4-flash", "api": "chat", "ok": true, "raw": "……", "result": {"winner": "a", "dimensions": {...}, "scores": {...}, "rationale": "……"}}

data: {"model": "glm-5.1", "api": "chat", "ok": false, "error": "TimeoutError: ……"}
```

- `result` 为模型输出解析出的 JSON 对象（自动剥离 ``` 代码围栏，容错提取首个 JSON 对象）；解析失败时 `result` 为 `null`，页面按非结构化输出展示
- `raw` 始终为模型原始输出
- 流结束后本次评审自动写入历史记录（含使用的提示词与全部结果）

### 历史判定分布

列表接口对每条记录统计 `winners`：`a` / `b` / `tie` / `fail`（调用失败）/ `other`（返回非预期结构），页面以「A×n · B×n · 平×n · 失败×n」形式展示。

## 数据存储

- SQLite 数据库默认位于项目根目录 `history.db`（可用 `OPENCODE_DB` 覆盖），表 `evaluations` 自动创建
- 字段：`id`、`created_at`（本地时间）、`question`、`answer_a`、`answer_b`、`prompt`、`models`（JSON）、`results`（JSON）
- 该文件已在 `.gitignore` 中排除

## 部署建议

```bash
# 生产环境启动（HTTPS 反向代理之后）
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4
```

- 前端为纯静态页面，由 FastAPI 直接托管（`/` → `index.html`，`/login` → `login.html`，`/static/*`）
- 单实例部署即可；若多 worker 共享同一 SQLite，注意并发写锁（当前场景写入频率低，一般无碍）

## 常见问题

- **模型列表为空？** 确认 `.env` 中 `OPENCODE_API_KEY` 已配置，且 `{base}/models` 可访问；实时发现失败会自动退回 `models.yaml` 列表
- **某些模型一直失败？** 点击「检测模型可用性」查看具体错误；`anthropic` 族模型需网关支持 `x-api-key` 认证
- **评审输出不是 JSON？** 模型输出无法解析时页面会原样展示 `raw`；可尝试在提示词中强调"只输出 JSON"
- **忘记访问密钥？** 查看/修改 `app/config.py` 中的 `ACCESS_KEY`
