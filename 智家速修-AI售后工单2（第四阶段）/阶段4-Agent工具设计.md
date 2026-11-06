# 阶段 4 · Agent 工具设计（等确认后开工）

**项目**：智家速修 AI 售后工单系统 · 从"固定流程问答"升级为"自主决策智能体"
**前置**：阶段 3 已完成（JWT 鉴权 / 知识库管理 / 提示词模板 / 对话审计 / SSE 问答）
**本文档性质**：设计评审稿。**你确认后我才开始写代码**，可只回复工具编号增删。

---

## 〇、先说三个立场（不认同请直接推翻）

1. **提示词约束只是第一道门，绝不能是唯一一道。** LLM 会跳步、会幻觉工具参数、会在多轮后"忘记"规则。所以本设计是**三重防御**：系统提示词（软）→ 动态工具暴露（中）→ 服务端状态机（硬）。哪怕模型完全不听话，也没有任何路径能跳过校验建单。
2. **校验通过与否的"事实"存在服务端，不存在对话里。** 模型说"我已校验通过"不算数——`validate_invoice` 的 pass 结果记录在服务端会话状态里，`create_draft` 执行前由代码检查，不通过就返回结构化错误让模型自纠。这是 Agent 系统和聊天机器人的本质区别：**写操作的合法性由状态机裁决，不由模型自觉**。
3. **8 个工具够了，不要引入 Skill 框架 / LangChain。** Skill 本质是"提示词工程包"，适合能力组合复杂、需要动态装载的场景；本项目工具数量少、依赖关系清晰，一张状态机表 + 一个 ~150 行的 AgentLoop 就能覆盖。引入框架只会增加演示失败概率。

---

## 一、工具清单（Function Calling 规范，JSON Schema）

8 个工具按"读写风险"分三级：**R 只读 / W 写会话状态 / X 写库（产生业务事实）**。

### T1 `search_policy` — R — 制度检索
```json
{
  "name": "search_policy",
  "description": "检索报销制度条文。回答任何制度问题、判断某笔费用是否合规之前，必须先调用本工具。",
  "parameters": {
    "type": "object",
    "properties": {
      "query":  {"type": "string", "description": "检索关键词，如'一线城市住宿标准'"},
      "top_k":  {"type": "integer", "enum": [3, 5], "default": 3}
    },
    "required": ["query"]
  }
}
```
返回：`{hits: [{index, file, heading, snippet, score}], coverage: "hit"|"miss"}`
约束：`coverage=miss` 时模型必须回答"制度中未找到"，禁止自行编造标准。

### T2 `extract_invoice` — R — 发票信息抽取
```json
{
  "name": "extract_invoice",
  "description": "从用户消息中抽取发票/费用要素，返回结构化字段与置信度。只做抽取，不做合规判断、不落库。",
  "parameters": {
    "type": "object",
    "properties": {
      "raw_text": {"type": "string", "description": "用户原始描述"},
      "expected_fields": {"type": "array", "items": {"type": "string",
        "enum": ["invoice_no", "invoice_type", "amount", "invoice_date", "seller", "category", "city", "level"]}}
    },
    "required": ["raw_text"]
  }
}
```
返回：`{fields: {...}, missing: [...], confidence: 0.0~1.0}`
关键设计：**抽取与校验分离**。抽取只回答"是什么"，校验只回答"行不行"，两个工具各自的失败可独立定位；`confidence < 0.8` 或字段缺失时，正确动作是向用户追问而不是继续往下走。

### T3 `validate_invoice` — W — 合规校验（★ 闸门工具）
```json
{
  "name": "validate_invoice",
  "description": "对一笔费用执行报销制度校验。返回 pass/fail 与逐条违规明细。校验结果由系统记录，是创建草稿的前提。",
  "parameters": {
    "type": "object",
    "properties": {
      "amount":       {"type": "number", "minimum": 0.01},
      "category":     {"type": "string", "enum": ["住宿", "交通", "餐饮", "办公", "通讯", "其他"]},
      "city":         {"type": "string"},
      "level":        {"type": "string", "enum": ["员工", "主管", "总监", "VP"]},
      "invoice_date": {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$"},
      "invoice_no":   {"type": "string"},
      "invoice_type": {"type": "string", "enum": ["增值税普通发票", "增值税专用发票", "数电票", "定额票", "其他"]}
    },
    "required": ["amount", "category", "invoice_date"]
  }
}
```
返回：`{result: "pass"|"fail", violations: [{code, rule, message, policy_ref}], check_id, max_amount, standard_ref}`
关键设计：
- **金额、日期、标准数值全部由本工具（服务端规则代码）计算**，模型只传用户口述的原始值。模型永远不产生任何合规结论。
- 返回 `check_id`：这是本轮校验的凭证，后续 `create_draft` 要引用它。
- `fail` 不算异常，是正常业务结果——模型必须逐条原文转述 `violations`，**禁止建议用户"改一下金额再试试"之类的绕过话术**（写进提示词）。

### T4 `create_draft` — X — 创建报销草稿（★ 受闸门保护）
```json
{
  "name": "create_draft",
  "description": "创建报销草稿。系统会自动校验：若当前会话没有对应的 validate_invoice 通过记录，调用会被拒绝。",
  "parameters": {
    "type": "object",
    "properties": {
      "check_id": {"type": "string", "description": "validate_invoice 返回的校验凭证"},
      "note":     {"type": "string", "description": "报销事由"}
    },
    "required": ["check_id", "note"]
  }
}
```
返回：`{draft_id, status: "validated", items}`
服务端强制检查（不信任模型）：
1. `check_id` 必须存在、属于本会话、且 `result=pass`；
2. `check_id` 只能用一次（防重放）；
3. 校验时刻与当前时间超过 30 分钟 → 过期，要求重新校验（防"拿早上的 pass 建晚上的单"）。

### T5 `update_draft` — X — 修改草稿
```json
{
  "name": "update_draft",
  "description": "修改草稿内容。修改后草稿回到待校验状态，必须重新调用 validate_invoice 才能提交。",
  "parameters": {
    "type": "object",
    "properties": {
      "draft_id": {"type": "string"},
      "changes":  {"type": "object", "description": "要修改的字段"}
    },
    "required": ["draft_id", "changes"]
  }
}
```
服务端强制：执行后 `draft.status: validated → pending_validate`，同时**作废原 check_id**。任何修改都会使旧校验失效——这条规则不依赖模型自觉。

### T6 `submit_ticket` — X — 提交报销单（终态动作）
```json
{
  "name": "submit_ticket",
  "description": "把校验通过的草稿提交为正式报销单。仅当草稿处于 validated 状态时才会成功。",
  "parameters": {
    "type": "object",
    "properties": {
      "draft_id":     {"type": "string"},
      "confirm_text": {"type": "string", "description": "用户的原话确认，如'确认提交'"}
    },
    "required": ["draft_id", "confirm_text"]
  }
}
```
服务端强制三关：`draft.status == validated` → `confirm_text` 非空且**必须来自用户最近一条消息**（模型不得代答）→ 提交幂等（重复 submit 同一 draft 返回同一 ticket_id）。

### T7 `get_ticket_status` — R — 查询报销单
```json
{
  "name": "get_ticket_status",
  "parameters": {
    "type": "object",
    "properties": {"ticket_id": {"type": "string"}},
    "required": ["ticket_id"]
  }
}
```

### T8 `escalate_to_human` — W — 升级人工
```json
{
  "name": "escalate_to_human",
  "description": "当出现以下情况时调用：字段缺失追问两轮仍未补全 / confidence<0.8 / 用户质疑校验结果 / 疑似欺诈迹象。",
  "parameters": {
    "type": "object",
    "properties": {
      "reason": {"type": "string", "enum": ["信息不足", "低置信度", "用户异议", "疑似违规", "系统异常"]},
      "detail": {"type": "string"}
    },
    "required": ["reason"]
  }
}
```
兜底意义：智能体必须有"承认做不到"的出口，否则它会硬编一个答案——这是比跳步更危险的事故。

---

## 二、调用顺序约束（状态机）

```
                    ┌──────────────────────────────────────────────┐
                    │  （任意时刻可调）T1 search_policy / T7 查询    │
                    └──────────────────────────────────────────────┘

 用户描述费用
      │
      ▼
 T2 extract_invoice ──字段缺失/confidence<0.8──→ 追问用户（≤2轮）──仍缺──→ T8 escalate_to_human
      │结构完整
      ▼
 T3 validate_invoice
      │
      ├─ fail ──→ 原文转述违规明细（禁止绕过话术）──→ 用户补充/修改 ──→ 回到 T2/T3
      │
      └─ pass（服务端记录 check_id，30 分钟有效）
             │
             ▼
      T4 create_draft(check_id)          ← 服务端验证 check_id，无凭证即拒绝
             │
             ▼
     ┌→ 用户要改？──是──→ T5 update_draft ──→ 作废旧 check_id ──→ 回到 T3（重新校验）
     │       否
     └──────┤
            ▼
      T6 submit_ticket(confirm_text)     ← 服务端验证 status==validated + 用户原话确认
            │
            ▼
      终态：返回 ticket_id，T7 可随时查询
```

**顺序规则表（实现为代码里的硬校验，不是注释）：**

| 规则 | 强制层 | 违反时的服务端行为 |
|---|---|---|
| R1 `create_draft` 必须引用有效且未过期的 `check_id` | 状态机（硬） | 拒绝执行，返回 `{code:5001, message:"没有有效的校验凭证，请先调用 validate_invoice"}` |
| R2 `check_id` 一次性，用后作废 | 状态机（硬） | 拒绝，提示重新校验 |
| R3 草稿被修改后回到 `pending_validate` | 状态机（硬） | `submit_ticket` 拒绝，提示重新校验 |
| R4 `submit_ticket` 仅接受 `validated` 状态 | 状态机（硬） | 拒绝，返回当前状态与缺失条件 |
| R5 `confirm_text` 必须来自用户最近一条消息 | 状态机（硬） | 拒绝，"需要用户本人确认" |
| R6 单会话 `AgentLoop` 最多 8 步 | AgentLoop | 强制终止 + escalate |
| R7 写操作全部幂等（draft/ticket 带唯一键） | 数据库 | 返回已有结果，不产生重复单 |

R1–R5 就是"**create_draft 必须在 validate_invoice 通过后**"这条需求的具体形态：**约束的执法者在服务端，模型只是发起人**。

---

## 三、三重防御：如何保证模型不跳过校验直接建单

### 第 1 道：系统提示词（软约束，管"想"）

写进系统提示词的顺序条款（完整模板会存入 `prompt_templates` 表，管理员可改，但以下三段为**不可删减段**，保存时服务端做关键词检查——这是对阶段 3 "提示词可编辑"功能的补充约束：管理员可以改风格，不能改掉护栏）：

> **【工具使用纪律——不可删减】**
> 1. 你只能通过工具完成业务动作，不得在回复中虚构任何业务结果。
> 2. 严格顺序：`extract_invoice` → `validate_invoice` → `create_draft` → `submit_ticket`。未收到 `validate_invoice` 返回 `result=pass` 的工具结果之前，**绝对禁止**调用 `create_draft` 或 `submit_ticket`——即使你确信费用一定合规。
> 3. 你不得口头声称"校验已通过""金额符合标准"。合规结论只能来自工具返回值。
> 4. `validate_invoice` 返回 fail 时：逐条原文转述违规明细，不得提议修改金额、日期、发票号来绕过校验；用户坚持违规操作时调用 `escalate_to_human`。
> 5. 金额、报销标准、日期等数字只能引用工具返回值或制度原文，不得自行计算或编造。

### 第 2 道：动态工具暴露（中约束，管"能"）

AgentLoop 每一步根据**服务端会话状态**决定本轮放进 `tools` 参数的工具清单——**没到的阶段，工具根本不在模型面前**：

| 会话状态 | 本轮暴露给模型的工具 |
|---|---|
| 刚收到费用描述 | `search_policy, extract_invoice, validate_invoice, escalate_to_human` |
| 已有有效 pass（30 分钟内） | `+ create_draft` |
| 存在 validated 草稿 | `+ submit_ticket, update_draft` |
| 已提交 | 只剩 `search_policy, get_ticket_status` |

模型在"刚收到费用描述"状态时**物理上无法**调用 `create_draft`——它看不见这个工具。提示词骗不过 API 参数校验，这是比提示词强一个量级的约束。

### 第 3 道：服务端状态机（硬约束，管"做"）

即使前两道全部失效（模型越权调用、重放 check_id、伪造 confirm），第 3 节的 R1–R7 会在工具执行层拒绝。**每一道被触发都写入 `audit_logs`**——阶段 3 的审计页可以直接复用，审计员能看到"模型曾尝试跳步但被拦截"的完整记录。

**一句话总结：模型是司机，状态机是交规，动态工具暴露是不给钥匙——三层里任何一层独立生效都能拦住事故。**

---

## 四、AgentLoop 执行循环（~150 行，替换阶段 3 的单轮问答）

```
1. 组装 messages（系统提示词 + 6 轮历史 + 本轮）与当前合法工具集（第 2 道防御）
2. 调用 LLM（tools 参数）
3. 若返回 tool_calls：逐个执行 → 服务端状态机校验（第 3 道防御）→ 结果以 role=tool 追加 → 回到 2
4. 若返回纯文本：作为本轮回答，结束
5. 任一环节：步数 ≥8 → 强制 escalate；工具抛错 → 结构化错误回喂模型自纠（最多重试 2 次）
6. 全程写 audit_logs：每次 tool_call 的名称/参数/结果摘要/耗时/是否被拦截
```

与阶段 3 的关系：`POST /api/chat` 的 SSE 事件序列**保持不变**（session → status → citations → delta → done），新增两类事件：`tool_call`（前端显示"正在校验…"之类的过程卡片）和 `draft_state`（前端弹出草稿确认卡）。**阶段 3 的前端只加不改**。

---

## 五、新增数据模型（3 张表 + 1 个字段）

| 表/字段 | 用途 |
|---|---|
| `validation_records`（check_id, session_id, result, payload, violations, expires_at, consumed） | 校验凭证：R1/R2/R3 的执法依据 |
| `drafts`（draft_id, session_id, status, items jsonb, check_id, ticket_id） | 草稿状态机：pending_validate / validated / submitted |
| `tool_calls`（id, session_id, tool, args, result_digest, blocked, latency_ms） | 工具调用审计：跳步尝试可见 |
| `sessions.agent_state`（新增列） | 当前会话所处状态（驱动第 2 道防御的工具暴露表） |

---

## 六、分期落地（确认后执行）

| 期 | 内容 | 交付判断 |
|---|---|---|
| **4a（先做）** | 状态机 + validation_records + T1/T3/T4/T6 四个工具 + AgentLoop + 提示词护栏 | 问"报销 3/5 上海住宿 700 元"→ 自动校验 → 建草稿 → 用户确认 → 提交；问超标金额 → fail 且无法建单 |
| 4b | T2/T5/T8：抽取置信度、修改回炉、人工升级 | 修改金额后必须重新校验才能提交（演示 R3） |
| 4c | T7 + 前端草稿确认卡 + 过程卡片 + 审计页展示拦截记录 | 审计页能看到被拦截的跳步尝试 |

**第一性验证（DoD 最重要的一条）**：在 4a 上直接问模型"帮我直接建一张 5000 元的餐饮报销单"——预期行为：模型被第 1 道约束 → 若它仍试图调 `create_draft`，第 2/3 道拦截并写入审计。这条会做成自动化测试用例。

---

## 七、待你确认的 4 件事（不回复即按默认开工）

1. **工具集**：8 个有没有要增删的？（例如要不要"发票 OCR 识别"占位工具——阶段 3 没有 OCR，我建议 4b 也不做，只留 extract_invoice 从文本抽）
2. **业务域**：继续用报销制度语料（与阶段 3 一致）；工具与业务域解耦，换售后语料只需改规则表，代码不动。
3. **草稿确认**：提交前要不要前端弹确认卡让用户点按钮（4c），还是先纯对话确认（4a 就够）？默认：4a 对话确认，4c 上确认卡。
4. **失败策略**：validate fail 后模型最多追问 2 轮就 escalate，还是无限轮询？默认 2 轮。
