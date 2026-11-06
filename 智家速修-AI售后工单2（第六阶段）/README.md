# 智家速修 AI 售后工单系统（第三阶段 · 管理后台）

> 智能家居 / 家电后市场服务（FSM 现场服务管理）场景下的**售后制度智能问答 + 管理后台**。
> 本阶段在阶段 1（RAG 命令行原型）、阶段 2（SSE 对话接口设计）之上，补齐**登录鉴权、知识库管理、提示词模板管理、对话审计**四大管理能力，做成可直接演示的 SaaS 风格单体应用。

---

## 一、快速开始

```bash
# 1. 安装依赖（Python 3.11+）
pip install -r requirements.txt

# 2. （可选）配置大模型，不配也能跑——自动降级
cp .env.example .env

# 3. 启动
uvicorn app.main:app --reload --port 8010
```

打开 http://127.0.0.1:8010

**默认账号**

| 角色 | 用户名 | 密码 | 权限 |
|---|---|---|---|
| 管理员 | `admin` | `admin123` | 知识库 / 提示词 / 审计 / 问答 |
| 员工 | `emp001` | `emp123456` | 仅问答 |
| 员工 | `emp002` | `emp123456` | 仅问答 |

> 首次启动会自动建表、写入 `data/报销制度.md` 作为种子语料（7 个切片），并创建默认提示词模板 v1。

---

## 二、页面清单

| # | 路径 | 页面 | 角色 | 功能 |
|---|---|---|---|---|
| 1 | `/login` | 登录页 | 匿名 | 用户名密码登录，签发 JWT 写入 HttpOnly Cookie |
| 2 | `/` | 员工问答页 | 员工/管理员 | SSE 流式对话、打字机效果、"正在检索制度…"状态、回答底部引用来源、会话列表与历史 |
| 3 | `/admin/kb` | 制度知识库 | 管理员 | 上传 md/pdf → 自动切片 → 向量化入库；文档列表（切片数/上传人/时间）；删除；全部重新向量化 |
| 4 | `/admin/prompts` | 提示词模板 | 管理员 | 编辑系统提示词、保存为新版本、历史版本列表、一键切换生效（改完立即对问答生效） |
| 5 | `/admin/audit` | 对话审计 | 管理员 | 按员工 / 时间区间 / 只看已标记 筛选问答记录；展开查看引用来源；标记 / 取消标记"疑似违规"并留痕 |

---

## 三、接口清单

### 页面路由（服务端渲染）
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/login` | 登录页 |
| POST | `/login` | 表单登录，成功 303 → `/`，失败 401 回登录页 |
| GET | `/logout` | 清除 Cookie |
| GET | `/` | 员工问答页（未登录 303 → `/login`） |
| GET | `/admin/kb` | 知识库页（非管理员 403） |
| GET | `/admin/prompts` | 提示词页（非管理员 403） |
| GET | `/admin/audit` | 审计页（非管理员 403） |

### 管理动作（表单 POST，302/303 重定向带中文提示）
| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/admin/kb/upload` | 上传 md/pdf，限 10MB，切片后向量化入库 |
| POST | `/admin/kb/{doc_id}/delete` | 删除文档（级联删除切片） |
| POST | `/admin/kb/revectorize` | 全部文档重新向量化（换 Embedding 供应商后用） |
| POST | `/admin/prompts/save` | 保存提示词为新版本并立即生效 |
| POST | `/admin/prompts/{id}/activate` | 切换到指定历史版本 |
| POST | `/admin/audit/{message_id}/flag` | 标记疑似违规（可填原因） |
| POST | `/admin/audit/{message_id}/unflag` | 取消标记 |

### JSON API
| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 健康检查，返回当前 LLM/Embedding 模式、文档数与切片数 |
| POST | `/api/chat` | **核心**：SSE 流式问答，请求体 `{"session_id": "...", "question": "..."}` |
| POST | `/api/sessions` | 新建会话 |
| GET | `/api/sessions` | 当前用户的会话列表 |
| GET | `/api/sessions/{sid}/messages` | 某会话的消息（含引用与标记状态） |

**`/api/chat` SSE 事件序列**

```
session   → 新会话时回传 session_id
status    → {stage:"retrieving", text:"正在检索制度…"}
citations → [ {index,file,heading,snippet,score,vector_rank,bm25_rank} ]
status    → {stage:"generating", text:"正在生成回答…"}
delta × N → {text:"..."}          ← 打字机逐段推送
done      → {message_id, model, degraded, usage, latency_ms}
error     → {code, message}        ← 仅运行时错误（检索/模型），流内上报
```

---

## 四、错误码约定

| 段位 | 含义 | 示例 |
|---|---|---|
| 1xxx | 参数错误（HTTP 400） | 1001 问题不能为空；1002 问题超长 |
| 2xxx | 会话错误（403/404） | 2001 会话不存在；2002 会话不属于当前用户 |
| 3xxx | 知识库 | 3001 知识库为空（503）；3002 未命中（**不算失败**）；3003 检索失败 |
| 4xxx | 模型 | 4001 大模型调用失败（**自动降级，不算失败**） |
| 5xxx | 系统 | 5000 未捕获异常 |

统一响应体：`{"code": 0, "message": "ok", "data": {...}}`

**错误分层原则**：SSE 一旦开始，HTTP 状态码就不能改。所以参数、会话归属、知识库就绪这类「本地能 100% 判定」的错误**在流开始前同步校验**，直接返回 4xx + JSON；Embedding / 大模型这类外部依赖的失败**放进流内 `error` 事件**，并自动降级为「检索原文展示」，保证用户永远拿得到东西。

---

## 五、技术选型

| 层 | 选型 | 理由 |
|---|---|---|
| Web 框架 | FastAPI | 异步 + 依赖注入做权限 + 自带 OpenAPI |
| 模板 | Jinja2 服务端渲染 | 管理后台不需要前端工程化，SSR 最快落地 |
| 数据库 | SQLite（WAL 模式） | 单机演示足够；WAL 让「SSE 长连接读」与「管理员写」不互相阻塞 |
| 向量存储 | `kb_chunks.embedding` (BLOB) + numpy 余弦 | 几十份文档量级，省掉 FAISS/pgvector 的运维与重建成本；删文档即删行 |
| 检索 | BM25(jieba) + 向量双路 → **RRF 融合** | 中文短查询里关键词命中和语义命中各有盲区，融合比单路稳 |
| 鉴权 | JWT（PyJWT）+ HttpOnly Cookie | 无状态；SSR 页面与 JSON API 共用一套 |
| 密码 | PBKDF2-HMAC-SHA256 + 随机盐 | 标准库实现，不引第三方 |
| 大模型 | OpenAI 兼容协议，供应商可换 | 换供应商只改 `.env`，业务代码零改动 |
| PDF 解析 | pypdf | 纯 Python，免装 poppler |

**没引入的东西（刻意）**：Redis、Celery、微服务、独立向量库、前端框架、ORM。这个体量引入它们只会增加演示失败概率。

---

## 六、目录结构

```
智家速修-AI售后工单2（第三阶段）/
├── app/
│   ├── main.py       # 全部路由：页面 SSR + 管理动作 + JSON API + SSE
│   ├── config.py     # 环境变量、供应商预设、默认提示词
│   ├── db.py         # 建表、种子数据、全部 CRUD
│   ├── auth.py       # 密码哈希、JWT 签发校验、角色依赖
│   ├── rag.py        # 切片、Embedding、向量库、BM25、RRF 融合
│   └── llm.py        # LLM 流式调用、降级答案、模拟流式
├── templates/        # base / login / chat / admin_base / kb / prompts / audit
├── static/style.css  # SaaS 风格样式
├── data/报销制度.md   # 种子语料（可替换成任意制度文档）
├── requirements.txt  .env.example  .gitignore
└── README.md
```

---

## 七、演进的四个阶段（本仓库的上下文）

| 阶段 | 内容 | 状态 |
|---|---|---|
| 1 | RAG 命令行原型：切片 / 向量化 / FAISS+BM25 混合检索 / 可换供应商 LLM | ✅ 已完成（独立项目） |
| 2 | `POST /chat` SSE 接口设计：事件时序、错误分层、引用结构 | ✅ 已完成（设计文档） |
| 3 | **管理后台**：JWT 鉴权、知识库管理、提示词模板、对话审计 | ✅ 本仓库 |
| 4 | 工单业务闭环：报修 → 初诊 → 派单 → 上门 → 完成 → 回访 | ⬜ 规划中 |

---

## 八、已知限制

1. **演示级鉴权**：JWT 存在 HttpOnly Cookie，但没有 Refresh Token、没有强制改密、没有登录失败锁定。
2. **SQLite 单机**：并发写入靠 WAL + busy_timeout 兜底，不适合多实例部署；上生产需换 PostgreSQL。
3. **向量全量载内存**：切片数超过 10 万会明显拖慢启动，届时换 pgvector。
4. **审计标记是人工的**：没有做"违规话术自动识别"，`flagged` 完全由管理员点击产生。
5. **无 OCR**：只接受 md/pdf 文本，扫描件 PDF 提取不出内容（会在上传时明确报错）。
6. **提示词无灰度和回滚审计**：切换生效是即时的，只记录 `updated_by/updated_at`。

---

## 九、License

MIT
