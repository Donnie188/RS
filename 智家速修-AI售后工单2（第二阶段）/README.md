# 智家速修 · AI 售后工单系统（第二版）

阶段 1 最小原型：**单文件 Python + RAG 检索增强问答**，不涉及 Web。

在第一版（微信小程序 Demo，AI 能力为随机模拟）的基础上，把「故障初诊」这一环做成**真实可运行**的：
读本地知识库 Markdown → 切片 → 向量化入 FAISS → BM25 + 向量混合检索 Top3 → 拼提示词 → 调大模型生成带引用的回答。

---

## 一、目录结构

```
智家速修-AI售后工单2/
├── ai_aftersales_rag.py      ← 全部逻辑都在这一个脚本里（约 1000 行，含详细中文注释）
├── data/
│   └── 售后知识库.md          ← RAG 语料（11 个章节：时效/初诊/5 类设备故障/派单/收费/升级）
├── index/                    ← 向量索引缓存（自动生成，已 gitignore，删了会自动重建）
├── requirements.txt
├── .env.example              ← 配置模板，复制为 .env 后填 Key
├── .gitignore
└── README.md
```

## 二、快速开始

```bash
# 1. 安装依赖（Python 3.9+，推荐 3.11）
pip install -r requirements.txt

# 2. 配置大模型（可选）
copy .env.example .env      # 然后编辑 .env，填入 LLM_API_KEY
#    不配也能跑：脚本会自动进入"本地降级模式"

# 3. 交互式问答
python ai_aftersales_rag.py

# 其他用法
python ai_aftersales_rag.py -q "智能门锁指纹识别无响应怎么办"   # 单条提问
python ai_aftersales_rag.py --rebuild                          # 强制重建索引
python ai_aftersales_rag.py --offline                          # 强制离线降级
python ai_aftersales_rag.py --topk 5 --chunk-size 400          # 调参
```

### 两种运行模式

| 模式 | 触发条件 | Embedding | 答案生成 | 适合场景 |
|---|---|---|---|---|
| **完整模式** | 配置了 `LLM_API_KEY` | 调用远端向量接口 | 调用远端大模型 | 真实效果验证 |
| **降级模式** | 无 Key 或加 `--offline` | 本地哈希向量（词袋） | 直接拼接召回原文 | **作业演示、无网环境** |

> 降级模式是刻意设计的：任何外部依赖都要有兜底路径。两条模式的检索链路完全一致，
> 区别只在"向量怎么来"和"答案怎么生成"。

## 三、运行效果示例

```
[索引] 文档切片完成：12 个切片，平均 275 字/片
[索引] 向量化完成：12 × 1024 维（本地哈希向量（降级模式 / dim=1024））
[索引] 已写入并持久化到：index/faiss.index

输入问题 > 智能门锁指纹识别无响应怎么办

召回片段（混合检索 Top3）
[1] 融合得分 0.0328 | 向量第1名(0.2606) | 关键词第1名(6.3069)
    来源：《三、智能门锁常见故障与处理》
[2] 融合得分 0.0320 | 向量第2名(0.1033) | 关键词第3名(1.4661)
    来源：《十、收费标准与保修政策》
[3] 融合得分 0.0318 | 向量第4名(0.0813) | 关键词第2名(1.7847)
    来源：《七、智能窗帘常见故障与处理》
```

每条召回都打印**向量通道排名**与**关键词通道排名**，方便直接观察两路召回的差异，判断该调权重还是调切片。

## 四、核心链路原理

```
Markdown 知识库
   ↓ ① 按标题切分（保留章节归属，便于标注出处）
   ↓ ② 按 500 字二次切片（优先在句号处断开，相邻片重叠 80 字）
Chunk 列表
   ↓ ③ Embedding（可换供应商；无 Key 时用本地哈希向量）
向量矩阵 (n × dim)，L2 归一化
   ↓ ④ FAISS IndexFlatIP（归一化后内积 == 余弦相似度）
向量索引 + BM25 倒排索引
   ↓ ⑤ 用户提问 → 双路各召回 Top9
   ↓ ⑥ RRF 倒数排名融合 → 取 Top3
   ↓ ⑦ 拼进提示词（强制引用编号、禁止编造、四段式输出）
   ↓ ⑧ llm_chat() 调用大模型
带 [1][2][3] 引用的回答
```

### 三个关键设计决策

**1. 为什么必须做 BM25 + 向量混合？**
纯向量检索擅长语义泛化（"打不开门"≈"无法开锁"），但对**型号、编号、专有名词**这类必须字面命中的查询容易召回错型号。BM25 对词频敏感，正好补这个短板。二者缺一，Top3 命中率都会明显下降。

**2. 为什么用 RRF 融合，而不是分数加权？**
BM25 得分是无上界的词频统计值（示例里 6.3），余弦相似度固定在 [-1, 1]，量纲完全不同，归一化加权需要反复调参且不稳定。RRF 只关心"排第几"：

```
score(d) = Σ 1 / (k + rank_i(d))        k = 60（经验值）
```

无需归一化、无需调参，是工业界混合检索的默认做法。同一文档在两路都出现时得分累加 —— 这就是"互相印证会排更前"。

**3. 为什么提示词要写"禁止编造"和"强制引用"？**
售后场景答错要付成本（派错工程师、报错价）。这三条护栏比换更强的模型更有效：
- 每条结论必须标 `[1][2][3]`，对应召回编号；
- 片段里没有的，必须明说"知识库未覆盖，建议转人工"；
- 费用、时效、保修期数字必须与原文完全一致，不得估算。

## 五、★ 核心模块与阶段 2 复用去向

脚本里每个模块都用 `★CORE-n` / `★ONCE-n` 标注。完整清单在文件头部注释中，摘要如下：

| 标记 | 模块 | 阶段 1 实现 | 阶段 2 去向 |
|---|---|---|---|
| ★CORE-1 | 配置中心 `Settings` | dataclass + .env | `app/core/config.py` |
| ★CORE-2 | 文档加载与切片 | 500 字 + 标题归属 | `services/knowledge/splitter.py` |
| ★CORE-3 | Embedding 客户端 | OpenAI 兼容协议 | `services/llm/base.py` + `factory.py` |
| ★CORE-4 | 向量库 FAISS | 单机文件索引 | **替换为 Postgres + pgvector**（接口不变） |
| ★CORE-5 | BM25 关键词检索 | rank_bm25 + jieba | 与 pgvector 检索并行 |
| ★CORE-6 | 混合检索融合 RRF | 倒序排名融合 | `services/retrieval_service.py` |
| ★CORE-7 | 提示词组装 | 模板 + 引用编号 | `agent/prompts/*.md` |
| ★CORE-8 | **LLM 调用函数** | 独立函数 `llm_chat()` | `services/llm/`，被 AgentLoop 调用 |
| ★CORE-9 | RAG 编排 Pipeline | `ask()` | `services/` + `agent/loop.py` |
| ★ONCE-1 | 命令行交互循环 | `while input()` | 替换为 HTTP 接口，删除 |

**可复用率约 80%**：只有 CLI 主循环（★ONCE-1）和 FAISS 具体实现（★CORE-B）会变，其余原样搬进 FastAPI 项目。

## 六、LLM 供应商可替换是怎么做到的

1. 所有供应商统一走 **OpenAI 兼容协议**（国产大模型基本都支持），因此一套 SDK 通吃；
2. `PROVIDER_PRESETS` 内置 deepseek / moonshot / dashscope / zhipu / openai / local 六家预设；
3. 换供应商 = 改 `.env` 里的 `LLM_PROVIDER`，**业务代码零改动**；
4. Embedding 单独一套配置（`EMBED_*`），因为很多对话供应商不提供向量接口（如 DeepSeek）；
5. 对外只暴露 `llm_chat(messages, settings)` 一个函数，单一职责，便于 mock 与测试。

```python
# 唯一的对话入口，任何业务模块都只调它
def llm_chat(messages, settings, temperature=None, max_tokens=None) -> dict:
    client = OpenAI(api_key=settings.llm_api_key, base_url=settings.llm_base_url)
    response = client.chat.completions.create(model=settings.llm_model, messages=messages, ...)
    return {"content": ..., "model": ..., "usage": {...}}
```

## 七、阶段演进路线

| 阶段 | 目标 | 相对本版的增量 |
|---|---|---|
| **阶段 1（本版）** | 验证检索 + 生成链路 | 单脚本、FAISS、命令行 |
| 阶段 2 | 变成服务 | FastAPI + Postgres(pgvector) + JWT 双角色（员工/管理员）+ 知识库管理端 |
| 阶段 3 | 加入智能体 | AgentLoop + 工具调用：建工单、故障初诊、判是否需上门、推荐备件清单 |
| 阶段 4 | 接业务系统 | 工单表、工程师表、派单算法（距离 40% + 技能 40% + 负载 20%）、满意度预测 |

阶段 3 的智能派单算法规则已写进知识库第八章，可直接作为提示词依据让模型解释派单结果。

## 八、常见问题

**Q：没有 API Key 能演示吗？**
能。脚本会自动进入降级模式，完整展示"切片 → 向量化 → 混合检索 → 召回排名"全过程，答案由召回原文拼接。作业答辩建议先用这个模式跑通，再演示配了 Key 的完整模式。

**Q：Windows 下报 `could not open ... faiss.index`？**
FAISS 底层是 C++ fstream，打不开含中文的绝对路径。脚本里已用 `_in_dir()` 临时切换工作目录 + 纯英文文件名绕开，无需手动处理。

**Q：改了知识库文档但检索结果没变？**
索引指纹（文档 SHA1 + 向量配置）未变时会复用旧索引。改文档会自动重建；也可用 `--rebuild` 强制重建。

**Q：为什么切片平均只有 275 字，不是 500 字？**
500 是**上限**不是目标值。切片逻辑先按 Markdown 标题切（保证一个切片只讲一个主题），章节本身不足 500 字时保持完整 —— 强行把不同章节拼到 500 字会污染语义，反而降低检索精度。

**Q：Top3 里混进了不太相关的章节（如问指纹却召回收费标准）？**
这是降级模式本地哈希向量的局限（只能匹配字面）。配置真实 Embedding 后语义区分度会显著提升。也可以：调大 `--topk` 后再加一层重排、或在提示词里要求模型忽略无关片段。
