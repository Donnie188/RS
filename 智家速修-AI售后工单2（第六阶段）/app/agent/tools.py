# -*- coding: utf-8 -*-
"""阶段 5 · Agent 工具层：Function Calling 规范 + 工具实现 + 状态机硬校验。

本模块是阶段 4 设计的落地。三层职责清晰分开：

1. TOOL_SCHEMAS —— 给模型看的"菜单"（JSON Schema，Function Calling 规范）。
   模型只能调用菜单上存在的工具，参数不符合 Schema 会在 API 层被拒。

2. 工具实现函数（TOOL_IMPLS 注册表）—— 工具真正干活的 Python 函数。
   只读工具（R）直接执行；写工具（X）在执行前必须通过 _guard_* 系列硬校验。

3. 状态机硬校验（_guard_*）—— 三重防御的第三道（最后一道）。
   这里**不信任任何来自模型的信息**：check_id 必须能在库里查到且属于本会话、
   未消费、未过期；confirm 文本必须逐字出现在用户最近一条消息里。
   校验不过 → 工具不执行 → 返回结构化错误让模型自纠，同时写拦截日志。

 ★CORE（阶段 6+ 可整体复用）：TOOL_SCHEMAS / TOOL_IMPLS / _guard_* 全部与
 Web 框架无关，只依赖 db 和 config，迁到任何宿主里都能直接用。
"""

import json
import re
import secrets
from datetime import datetime, timedelta

from .. import db
from ..rag import RagService

# 校验凭证有效期（分钟）。超过这个时间的 pass 凭证作废，
# 防止"上午校验通过、下午拿着凭证建一张内容已经不同的单"。
CHECK_TTL_MINUTES = 30

# 规则表：按 职级 × 城市级别 给出差旅住宿上限（元/晚）。
# 真实项目里这张表应来自制度文档的结构化配置；教学项目硬编码即可。
STAY_LIMITS = {
    ("一线城市", "员工"): 500, ("一线城市", "主管"): 600,
    ("一线城市", "总监"): 800, ("一线城市", "VP"): 1000,
    ("二线城市", "员工"): 350, ("二线城市", "主管"): 450,
    ("二线城市", "总监"): 600, ("二线城市", "VP"): 800,
}
FIRST_TIER_CITIES = {"北京", "上海", "广州", "深圳"}

# 报销有效期：发票日期距今天超过 90 天不得报销（演示规则）。
MAX_INVOICE_AGE_DAYS = 90


# ============================================================================
# 一、工具菜单：Function Calling 的 JSON Schema（模型只看得到这一层）
# ============================================================================

TOOL_SCHEMAS: dict[str, dict] = {
    "search_policy": {
        "type": "function",
        "function": {
            "name": "search_policy",
            "description": "检索报销制度条文。回答任何制度问题、判断费用是否合规前必须先调用本工具。"
                           "未命中时必须如实告知'制度中未找到'，禁止编造标准。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索关键词，如'一线城市住宿标准'"},
                    "top_k": {"type": "integer", "enum": [3, 5], "default": 3},
                },
                "required": ["query"],
            },
        },
    },
    "extract_invoice": {
        "type": "function",
        "function": {
            "name": "extract_invoice",
            "description": "从用户描述中抽取报销要素（金额/类别/城市/职级/发票日期等），"
                           "返回结构化字段与缺失项。只做抽取，不做合规判断。"
                           "存在 missing 字段时应先向用户追问补全。",
            "parameters": {
                "type": "object",
                "properties": {
                    "raw_text": {"type": "string", "description": "用户原始描述"},
                },
                "required": ["raw_text"],
            },
        },
    },
    "validate_invoice": {
        "type": "function",
        "function": {
            "name": "validate_invoice",
            "description": "对一笔费用执行报销制度校验，返回 pass/fail 与逐条违规明细。"
                           "结果由系统记录并签发凭证（check_id），是创建草稿的唯一前提。"
                           "金额、日期等数字由本工具计算，你不得自行给出合规结论。",
            "parameters": {
                "type": "object",
                "properties": {
                    "amount":       {"type": "number", "minimum": 0.01, "description": "报销总金额（元）"},
                    "category":     {"type": "string", "enum": ["住宿", "交通", "餐饮", "办公", "通讯", "其他"]},
                    "city":         {"type": "string", "description": "发生城市"},
                    "level":        {"type": "string", "enum": ["员工", "主管", "总监", "VP"], "default": "员工"},
                    "nights":       {"type": "integer", "minimum": 1, "description": "住宿晚数（住宿类必填）"},
                    "invoice_date": {"type": "string", "pattern": "^\\d{4}-\\d{2}-\\d{2}$"},
                    "has_invoice":  {"type": "boolean", "description": "是否持有合规发票"},
                },
                "required": ["amount", "category"],
            },
        },
    },
    "create_draft": {
        "type": "function",
        "function": {
            "name": "create_draft",
            "description": "创建报销草稿。必须传入 validate_invoice 通过后返回的 check_id。"
                           "系统会先验证凭证，无有效凭证时调用会被拒绝。"
                           "创建成功后流程会暂停，等待员工确认后才能提交。",
            "parameters": {
                "type": "object",
                "properties": {
                    "check_id": {"type": "string", "description": "校验凭证 ID"},
                    "note":     {"type": "string", "description": "报销事由（一句话）"},
                },
                "required": ["check_id", "note"],
            },
        },
    },
    "submit_ticket": {
        "type": "function",
        "function": {
            "name": "submit_ticket",
            "description": "把员工已确认的草稿提交为正式报销单。仅当草稿处于 validated 状态时才会成功。",
            "parameters": {
                "type": "object",
                "properties": {
                    "draft_id": {"type": "string"},
                },
                "required": ["draft_id"],
            },
        },
    },
    "get_ticket_status": {
        "type": "function",
        "function": {
            "name": "get_ticket_status",
            "description": "查询本会话已提交报销工单的当前状态（只读，不改变任何流程状态）。"
                           "不传 ticket_id 时默认查本会话最近一笔已提交工单。",
            "parameters": {
                "type": "object",
                "properties": {
                    "ticket_id": {"type": "string",
                                  "description": "报销工单号（TK 开头），可省略"},
                },
                "required": [],
            },
        },
    },
    "escalate_to_human": {
        "type": "function",
        "function": {
            "name": "escalate_to_human",
            "description": "升级人工处理。出现以下情况必须调用：信息追问两轮仍补不全、"
                           "用户质疑校验结果、疑似欺诈、系统异常。不要硬编答案。",
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {"type": "string",
                               "enum": ["信息不足", "低置信度", "用户异议", "疑似违规", "系统异常"]},
                    "detail": {"type": "string", "description": "补充说明"},
                },
                "required": ["reason"],
            },
        },
    },
}


# ============================================================================
# 二、动态工具暴露（三重防御的第 2 道）
# ============================================================================

# 会话状态 → 本轮允许模型看见的工具清单。
# 核心思想：没到的阶段，工具根本不在模型面前——它想调也调不了。
STATE_TOOLS: dict[str, list[str]] = {
    "idle":         ["search_policy", "extract_invoice", "validate_invoice", "escalate_to_human"],
    "validated":    ["search_policy", "validate_invoice", "create_draft",
                     "escalate_to_human"],                      # 有有效 pass 凭证 → 允许建草稿
    "draft_pending": ["search_policy", "escalate_to_human"],    # 等确认中：冻结建单类工具
    "draft_confirmed": ["search_policy", "submit_ticket", "escalate_to_human"],
    "submitted":    ["search_policy", "get_ticket_status", "escalate_to_human"],
}


def allowed_tools(session_id: str, state: str) -> list[dict]:
    """按会话状态返回本轮的 tools 参数（schema 列表）。

    状态名会被事实核对后归一化：状态声称"有有效凭证"（validated），但库里
    实际查不到（例如过期了、被消费了），自动降级回 idle——
    状态必须和数据库事实一致，否则状态本身就是漏洞。
    """
    if state == "validated":
        row = db.query_one(
            "SELECT check_id FROM validation_records WHERE session_id=? AND result='pass'"
            " AND consumed=0 AND expires_at > ? ORDER BY created_at DESC LIMIT 1",
            (session_id, db.now_str()))
        if row is None:
            state = "idle"
    return [TOOL_SCHEMAS[name] for name in STATE_TOOLS.get(state, STATE_TOOLS["idle"])]


# ============================================================================
# 三、工具实现（真正干活的函数）+ 状态机硬校验（第 3 道防御）
# ============================================================================

def _money(v) -> float:
    """金额转 float 并保留两位。模型传来的数字永远不做信任假设。"""
    return round(float(v), 2)


def _tool_search_policy(rag: RagService, session_id: str, args: dict) -> dict:
    """T1 制度检索：直接复用阶段 3 的混合检索（RRF 融合），零改动复用。

    RagService.search() 固定返回 TOP_K 条（阶段 3 配置），top_k 参数仅作
    语义兼容；需要更大召回时扩展 rag 层即可，工具协议不用变。
    """
    hits = rag.search(args["query"])
    return {"hits": hits, "coverage": "hit" if hits else "miss"}


# 会话级抽取缓存：员工分多轮补全要素时（第 1 轮说城市、第 2 轮补金额），
# 把已抽到的字段记下来合并。教学项目放进程内存即可（重启即清，重新走流程）；
# 生产环境应持久化到 sessions 表或 Redis。create_draft 成功后清空。
_EXTRACT_CACHE: dict[str, dict] = {}


def _tool_extract_invoice(rag: RagService, session_id: str, args: dict) -> dict:
    """T2 要素抽取：正则 + 关键词的确定性抽取（阶段 5 不引入 NER 模型）。

    抽取与校验分离的意义：本工具只回答"用户说了什么"，
    "行不行"完全交给 validate_invoice。两个失败互不干扰。
    """
    text = args.get("raw_text", "")
    fields: dict = {}

    # 金额：匹配 "700元" / "700 元" / "¥700" / "700块"
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:元|块|¥|￥)", text)
    if m:
        fields["amount"] = _money(m.group(1))

    # 天数/晚数：匹配 "3天" "住2晚"
    m = re.search(r"(\d+)\s*晚", text) or re.search(r"(\d+)\s*天", text)
    if m:
        fields["nights"] = max(1, int(m.group(1)) - 0 if "晚" in text else 1)

    # 城市：优先精确命中一线城市名单，再匹配"去/在/到 X"句式
    for city in FIRST_TIER_CITIES:
        if city in text:
            fields["city"] = city
            break
    if "city" not in fields:
        m = re.search(r"[去在到](北京|上海|广州|深圳|\w{2,4}?市)", text)
        if m:
            city = m.group(1).removesuffix("市")
            fields["city"] = city

    # 职级
    for lv in ("VP", "总监", "主管", "员工"):
        if lv in text:
            fields["level"] = lv
            break
    fields.setdefault("level", "员工")

    # 费用类别（关键词映射）
    for cat, kws in {"住宿": ["住宿", "酒店", "宾馆", "房费"],
                     "交通": ["高铁", "火车", "机票", "打车", "出租", "交通"],
                     "餐饮": ["餐", "饭", "宴请"],
                     "办公": ["办公", "采购", "打印"],
                     "通讯": ["话费", "通讯"]}.items():
        if any(k in text for k in kws):
            fields["category"] = cat
            break

    # 发票日期：YYYY-MM-DD 或 "去年/上个月" 等口语（口语日期只能标记，不能猜）
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if m:
        fields["invoice_date"] = f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    fields["has_invoice"] = ("发票" in text) or ("has_invoice" in fields)

    # 必填项缺失清单：把"该问什么"交给数据而不是模型发挥
    required_by_cat = {"住宿": ["amount", "nights", "city", "invoice_date"],
                       "交通": ["amount", "invoice_date"],
                       "餐饮": ["amount", "invoice_date"]}
    need = required_by_cat.get(fields.get("category", ""), ["amount", "category"])
    missing = [k for k in need if k not in fields]

    # ---- 与上一轮已抽到的字段合并（多轮补全的核心）----
    # 合并策略：本轮显式说出的字段覆盖缓存（用户改口以最新为准），
    # 本轮没提的字段沿用缓存（第 1 轮说了"去北京"、第 2 轮只补金额，城市仍在）。
    cached = _EXTRACT_CACHE.get(session_id, {})
    merged = {**cached, **{k: v for k, v in fields.items() if v not in (None, "")}}
    fields = merged

    # 重新计算缺失项（合并后缺失应逐轮减少）
    required_by_cat = {"住宿": ["amount", "nights", "city", "invoice_date"],
                       "交通": ["amount", "invoice_date"],
                       "餐饮": ["amount", "invoice_date"]}
    need = required_by_cat.get(fields.get("category", ""), ["amount", "category"])
    missing = [k for k in need if k not in fields]

    confidence = round(1 - 0.25 * len(missing), 2)
    if missing:
        _EXTRACT_CACHE[session_id] = fields      # 没齐：记住，等下一轮补
    else:
        _EXTRACT_CACHE.pop(session_id, None)     # 齐了：清缓存，流程向下走
    return {"fields": fields, "missing": missing, "confidence": confidence}


def _tool_validate_invoice(rag: RagService, session_id: str, args: dict) -> dict:
    """T3 合规校验（★闸门）：唯一的合规结论产出点，全部由规则代码计算。

    模型在本工具里没有任何裁量权——它只负责把用户口述的原始数字传进来。
    无论 pass/fail 都落一条 validation_records（审计回放需要完整轨迹）。
    """
    amount = _money(args["amount"])
    category = args["category"]
    city = args.get("city", "")
    level = args.get("level", "员工")
    violations: list[dict] = []
    standard_ref = ""

    # ---- 规则 1：无发票不得报销 ----
    if not args.get("has_invoice", False):
        violations.append({"code": "NO_INVOICE", "rule": "发票与凭证要求",
                           "message": "未提供合规发票，按制度不能报销，请补开发票或提供替代凭证。"})

    # ---- 规则 2：发票时效（超过 90 天不得报销）----
    inv_date = args.get("invoice_date")
    if inv_date:
        try:
            age = (datetime.now() - datetime.strptime(inv_date, "%Y-%m-%d")).days
            if age > MAX_INVOICE_AGE_DAYS:
                violations.append({"code": "EXPIRED_INVOICE", "rule": "报销时效",
                                   "message": f"发票日期 {inv_date} 距今 {age} 天，"
                                              f"超过 {MAX_INVOICE_AGE_DAYS} 天报销时效。"})
        except ValueError:
            violations.append({"code": "BAD_DATE", "rule": "数据校验",
                               "message": f"发票日期格式不合法：{inv_date}"})
    else:
        violations.append({"code": "NO_DATE", "rule": "数据校验",
                           "message": "缺少发票日期，无法核验时效，请补充。"})

    # ---- 规则 3：住宿标准（职级 × 城市级别 分档，金额只能来自规则表）----
    if category == "住宿":
        tier = "一线城市" if city in FIRST_TIER_CITIES else "二线城市"
        limit = STAY_LIMITS.get((tier, level))
        nights = int(args.get("nights") or 1)
        if limit is None:
            violations.append({"code": "NO_STANDARD", "rule": "住宿标准",
                               "message": f"未找到 {tier}×{level} 的住宿标准，转人工核定。"})
        else:
            per_night = amount / nights
            standard_ref = f"{tier}·{level} 住宿标准 {limit} 元/晚"
            if per_night > limit:
                violations.append({"code": "OVER_STANDARD", "rule": "住宿标准",
                                   "message": f"折合 {per_night:.0f} 元/晚，超出 {standard_ref}"
                                              f"（超 {per_night - limit:.0f} 元/晚）。"})
    elif category == "餐饮":
        standard_ref = "餐饮凭发票据实报销（单日上限 100 元）"
        if amount > 100 * (int(args.get("nights") or 1) + 1):
            violations.append({"code": "OVER_MEAL", "rule": "餐饮标准",
                               "message": f"餐饮金额 {amount} 元超出合理范围（{standard_ref}）。"})

    result = "fail" if violations else "pass"

    # ---- 签发凭证：pass 才有价值，但 fail 也留痕（审计要看到完整决策链）----
    check_id = "chk_" + secrets.token_hex(6)
    expires_at = (datetime.now() + timedelta(minutes=CHECK_TTL_MINUTES)).isoformat(timespec="seconds")
    db.create_validation(check_id, session_id, result,
                         args, violations, standard_ref or None, expires_at)

    return {"result": result, "check_id": check_id if result == "pass" else None,
            "violations": violations, "standard_ref": standard_ref,
            "expires_at": expires_at if result == "pass" else None,
            "amount": amount}


def _tool_create_draft(rag: RagService, session_id: str, args: dict) -> dict:
    """T4 创建草稿（★受闸门保护 + 强制中断点）。

    硬校验三连（任一不过直接拒绝，工具不执行）：
      R1 check_id 必须是本会话、pass、未消费、未过期的有效凭证；
      R2 通过后立即消费凭证（一次性）；
      R3 草稿初始状态即 pending_confirm——流程在此中断，等员工确认。

    模型拿到的 description 里已写明"无凭证会被拒"，但真正的保证是这里的代码。
    """
    check_id = args.get("check_id", "")
    row = db.get_valid_check(session_id, check_id)
    if row is None:
        # ★拦截：把"为什么不行"说清楚，让模型（和用户）知道下一步该干什么
        raise PermissionError(
            "没有有效的校验凭证（check_id 不存在、不属于本会话、已使用或已过期）。"
            "请先调用 validate_invoice 并取得新的 check_id。")

    payload = json.loads(row["payload"])
    draft_id = "dft_" + secrets.token_hex(6)
    items = {
        "note": args.get("note", "差旅费用报销"),
        "amount": payload.get("amount"),
        "category": payload.get("category"),
        "city": payload.get("city", ""),
        "level": payload.get("level", "员工"),
        "nights": payload.get("nights"),
        "invoice_date": payload.get("invoice_date"),
        "standard_ref": row["standard_ref"],
    }
    db.create_draft(draft_id, session_id, items, check_id)
    db.consume_check(check_id)                       # R2：凭证一次性
    db.set_agent_state(session_id, "draft_pending")
    _EXTRACT_CACHE.pop(session_id, None)             # 流程闭环，清掉要素缓存
    return {"draft_id": draft_id, "status": "pending_confirm", "items": items,
            "message": "草稿已创建，已暂停流程等待员工确认。请把草稿内容展示给员工并等待确认。"}


def _tool_submit_ticket(rag: RagService, session_id: str, args: dict) -> dict:
    """T6 提交报销单。执行前提（全部服务端判定）：

    R4 草稿存在、属于本会话、状态 = validated（即员工已经确认过）；
    R7 幂等：已 submitted 的草稿重复提交返回原 ticket_id，不产生重复单。
    """
    draft = db.get_draft(args.get("draft_id", ""))
    sid = session_id
    if draft is None or draft["session_id"] != sid:
        raise PermissionError("草稿不存在或不属于当前会话。")
    if draft["status"] == "submitted":
        # R7 幂等：直接返回已有单号
        return {"ticket_id": draft["ticket_id"], "status": "submitted", "idempotent": True}
    if draft["status"] != "validated":
        raise PermissionError(
            f"草稿当前状态为 {draft['status']}，只有员工确认后的草稿（validated）才能提交。")
    ticket_id = "TK" + datetime.now().strftime("%Y%m%d") + secrets.token_hex(3).upper()
    db.set_draft_status(draft["draft_id"], "submitted", ticket_id)
    db.set_agent_state(sid, "submitted")
    return {"ticket_id": ticket_id, "status": "submitted", "idempotent": False}


def _tool_get_ticket_status(rag: RagService, session_id: str, args: dict) -> dict:
    """T7 工单状态查询（只读）：按工单号查本会话报销单的当前状态。

    权限约束：只允许查本会话的工单（session_id 过滤），杜绝横向越权查询。
    ticket_id 缺省时查本会话最近一笔已提交工单（贴近真实口语"上次那个单"）。
    """
    ticket_id = (args.get("ticket_id") or "").strip()
    if ticket_id:
        row = db.query_one(
            "SELECT draft_id, ticket_id, status, items, updated_at FROM drafts"
            " WHERE ticket_id = ? AND session_id = ?",
            (ticket_id, session_id))
    else:
        row = db.query_one(
            "SELECT draft_id, ticket_id, status, items, updated_at FROM drafts"
            " WHERE session_id = ? AND ticket_id IS NOT NULL"
            " ORDER BY updated_at DESC LIMIT 1", (session_id,))
    if row is None:
        return {"error": "未找到对应的报销工单，请确认工单号是否正确。"}
    return {"ticket_id": row["ticket_id"], "status": row["status"],
            "draft_id": row["draft_id"], "updated_at": row["updated_at"],
            "message": "状态查询完成。"}


def _tool_escalate_to_human(rag: RagService, session_id: str, args: dict) -> dict:
    """T8 升级人工：Agent 的"承认做不到"出口。写审计、改状态，避免硬编答案。"""
    sid = session_id
    db.set_agent_state(sid, "escalated")
    return {"message": f"已转人工处理（{args.get('reason')}），人工客服会尽快跟进。",
            "detail": args.get("detail", "")}


# 工具名 → 实现函数。loop.py 只跟这张表打交道。
TOOL_IMPLS = {
    "search_policy":      _tool_search_policy,
    "extract_invoice":    _tool_extract_invoice,
    "validate_invoice":   _tool_validate_invoice,
    "create_draft":       _tool_create_draft,
    "submit_ticket":      _tool_submit_ticket,
    "get_ticket_status":  _tool_get_ticket_status,
    "escalate_to_human":  _tool_escalate_to_human,
}
