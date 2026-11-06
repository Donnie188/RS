# -*- coding: utf-8 -*-
"""阶段 5 · Agent 执行循环（Harness）。

框架选型说明（为什么自研 Harness 而不用 LangChain/LangGraph）：

  "Harness" 不是某个具体库的名字，而是一种模式：**一个把"模型决策、工具执行、
  结果回填"串成循环的宿主程序**。OpenAI / DeepSeek / 通义等所有 OpenAI 兼容 API
  都原生支持 Function Calling——模型返回 tool_calls，宿主执行后把结果以
  role="tool" 追加回 messages 再喂回去，如此往复直到模型给出最终回答。

  本项目工具只有 6 个、依赖关系由一张状态表决定，自研 Harness 约 200 行就能
  覆盖全部需求，且每一行都可控、可讲清楚。引入 LangGraph 需要额外引入状态图
  抽象、checkpoint 存储、 hundreds 兆依赖——收益为零，演示风险陡增。
  ★这就是阶段 1 定下的架构纪律的延续：够用即可，不为简历驱动开发。

循环骨架（用户要求的四拍）：

      ┌────────────────────────────────────────────┐
      │  1. 模型决策（带 tools 菜单调用 LLM）          │
      │  2. 执行工具（经过状态机硬校验，失败自纠）        │
      │  3. 结果回填（role="tool" 追加进 messages）    │
      │  4. 再决策（回到 1，直到给出最终回答）          │
      └─────────────── 最多 MAX_STEPS 轮 ────────────┘

三个关键机制：

* 动态工具暴露：每轮开始时按服务端会话状态重新计算 tools 菜单（第 2 道防御）。
* 强制中断（human-in-the-loop）：模型想调 create_draft 时，**不直接执行**——
  先建"待确认"草稿并把内容通过 SSE 流式推给员工，整个循环暂停，
  直到员工在前端点"确认"（POST /api/drafts/{id}/confirm）才恢复提交环节。
* 决策日志：每轮每工具写 tool_calls 表（工具名/参数/耗时/结果摘要/是否拦截），
  阶段 5 分析、答辩演示"轨迹回放"都靠它。

 ★CORE：本模块与 FastAPI 无关（通过 on_event 回调把 SSE 推送解耦出去），
 阶段 6 若换宿主只需换掉事件回调的实现。
"""

import json
import time
from collections.abc import Callable

from .. import db
from .. import llm
from ..config import Config
from ..rag import RagService
from . import tools as T


def _fake_stream_sync(text: str, batch: int = 3):
    """同步版模拟流式：把完整文本按 3 字一段产出。

    loop.run_turn 是同步函数（跑在 FastAPI 线程池里），不能 await
    llm.fake_stream（异步生成器），因此这里提供一个等价的同步实现，
    前端打字机效果保持一致。
    """
    for i in range(0, len(text), batch):
        yield {"type": "delta", "text": text[i:i + batch]}

# 单次提问的决策轮数上限（用户要求最多 5 轮）。
# 到顶还没收敛 → 强制升级人工，而不是让模型无限空转烧 token。
MAX_STEPS = 5

# AgentLoop 的系统提示词：在阶段 3 管理员提示词之上追加"工具使用纪律"。
# 注意：阶段 3 的提示词决定"回答风格与知识边界"，这一段决定"行动纪律"，两者拼接使用。
AGENT_DISCIPLINE = """

【工具使用纪律——系统段，不可覆盖】
1. 你只能通过工具完成业务动作，不得在回复中虚构任何业务结果或合规结论。
2. 严格顺序：extract_invoice → validate_invoice → create_draft → submit_ticket。
   在收到 validate_invoice 返回 result=pass 的工具结果之前，绝对不要尝试创建草稿。
3. 你不得口头声称"校验已通过"。合规结论只能来自工具返回值。
4. validate_invoice 返回 fail 时：逐条原文转述违规明细，不得提议修改金额、
   日期或发票号来绕过校验；用户坚持时调用 escalate_to_human。
5. 金额、报销标准、日期等数字只能引用工具返回值或检索到的制度原文。
6. 只回答制度与报销问题，与报销无关的问题礼貌拒绝。
"""


class AgentEvent:
    """AgentLoop 对外产出的事件（宿主负责把这些变成 SSE 推给前端）。

    kind 取值：
      tool_call    —— 某工具开始执行（前端显示过程卡片）
      tool_result  —— 工具执行完成（摘要）
      pending_draft —— ★中断：草稿已建好，等员工确认（前端弹确认卡）
      delta        —— 最终回答的流式文本片段
      done         —— 本轮结束（附带摘要信息）
    """
    def __init__(self, kind: str, data: dict):
        self.kind = kind
        self.data = data


# 事件回调类型：宿主（main.py 的 SSE 生成器）提供一个 sink 把事件推给前端
EventSink = Callable[[AgentEvent], None]


def run_turn(rag: RagService, session_id: str, question: str,
             history: list[dict], emit: EventSink) -> dict:
    """执行一次完整提问的 Agent 循环。返回本轮摘要（给 done 事件与审计用）。

    参数：
      rag      —— 阶段 3 的检索服务（工具直接复用，零改动）
      question —— 本轮用户输入
      history  —— 最近 6 轮对话（阶段 3 已有的 get_history）
      emit     —— 事件回调：把 AgentEvent 推到 SSE 流
    """
    # ---- 0. 组装上下文：系统提示词 = 管理员模板（阶段3存库）+ 工具纪律段 ----
    prompt_row = db.get_active_prompt()
    system_prompt = (prompt_row["content"] if prompt_row else Config.DEFAULT_PROMPT) + AGENT_DISCIPLINE

    messages: list[dict] = [{"role": "system", "content": system_prompt}]
    for m in history:
        messages.append({"role": m["role"], "content": m["content"]})
    messages.append({"role": "user", "content": question})

    state = db.get_agent_state(session_id)
    step = 0
    final_text = ""
    degraded = False
    model_name = Config.LLM_MODEL

    while step < MAX_STEPS:
        step += 1

        # ---- 1. 模型决策：动态工具暴露（第 2 道防御）----
        #       每轮重新计算菜单：没到的阶段，工具不在模型面前。
        schema_list = T.allowed_tools(session_id, state)

        if Config.llm_ready():
            # ---- 真实模式：调用大模型做决策（非流式，工具调用需要完整结构）----
            try:
                decision = llm.decide(messages, schema_list, system_prompt)
            except Exception as exc:
                # 模型层失败：降级到规则决策器，保证流程可继续（演示不中断）
                degraded = True
                model_name = "local-fallback"
                emit(AgentEvent("tool_result", {"note": f"模型调用失败，切换本地决策器：{exc}"}))
                decision = _rule_decide(session_id, question, messages, schema_list)
        else:
            # ---- 降级模式：无 Key 用规则决策器（确定性路由，可离线演示）----
            degraded = True
            model_name = "local-router"
            decision = _rule_decide(session_id, question, messages, schema_list)

        # ---- 2. 分支：模型给出最终回答 → 结束 ----
        if decision["type"] == "answer":
            final_text = decision["content"]
            if degraded:
                # 降级模式的"回答"由决策器给出，按句模拟流式推送（打字机效果一致）
                for chunk in _fake_stream_sync(final_text):
                    if chunk["type"] == "delta":
                        emit(AgentEvent("delta", {"text": chunk["text"]}))
            else:
                emit(AgentEvent("delta", {"text": final_text}))
            break

        # ---- 3. 分支：模型要求调工具 → 逐个执行 ----
        for call in decision["tool_calls"]:
            name = call["name"]
            args = call.get("arguments", {}) or {}
            emit(AgentEvent("tool_call", {"step": step, "tool": name, "args": args}))

            t0 = time.time()
            try:
                # ★强制中断点：create_draft 先建"待确认"草稿并暂停循环。
                # 工具实现本身会把状态置为 draft_pending 并消费 check_id，
                # 这里只负责把草稿内容流式推给员工，然后终止本轮循环——
                # 提交动作必须等员工点"确认"（POST /api/drafts/{id}/confirm）。
                result = T.TOOL_IMPLS[name](rag, session_id, args)
                latency = int((time.time() - t0) * 1000)
                digest = json.dumps(result, ensure_ascii=False)
                db.log_tool_call(session_id, step, name, args, digest, False, latency)
                emit(AgentEvent("tool_result", {"step": step, "tool": name, "ok": True}))

                # 工具结果回填（第 3 拍）：以 role=tool 追加，模型下一轮能看到
                messages.append({"role": "tool", "tool_call_id": call.get("id", name),
                                 "content": digest})
                messages.append({"role": "assistant", "content":
                                 f"[调用 {name} 完成]"})

                # create_draft 成功 → 触发中断，等待人工确认
                if name == "create_draft":
                    emit(AgentEvent("pending_draft", result))
                    return _finish(session_id, step, final_text, degraded, model_name,
                                   interrupted=True)

                # 状态推进：validate pass 后会话进入 validated（解锁 create_draft）
                if name == "validate_invoice" and result.get("result") == "pass":
                    state = "validated"
                    db.set_agent_state(session_id, state)
                if name == "escalate_to_human":
                    state = "escalated"
                    final_text = result.get("message", "已转人工处理")
                    emit(AgentEvent("delta", {"text": final_text}))
                    return _finish(session_id, step, final_text,
                                   degraded, model_name, escalated=True)

            except PermissionError as exc:
                # ---- 状态机拦截（第 3 道防御生效）：拒绝执行并回填错误让模型自纠 ----
                latency = int((time.time() - t0) * 1000)
                db.log_tool_call(session_id, step, name, args, str(exc), True, latency)
                emit(AgentEvent("tool_result", {"step": step, "tool": name, "ok": False,
                                                "message": str(exc)}))
                messages.append({"role": "tool", "tool_call_id": call.get("id", name),
                                 "content": json.dumps({"error": str(exc)}, ensure_ascii=False)})
                # 被拦截后不再让模型继续本轮（避免死循环重试），直接把错误说明给用户
                final_text = f"⚠️ {exc}"
                emit(AgentEvent("delta", {"text": final_text}))
                return _finish(session_id, step, final_text, degraded, model_name)

            except KeyError:
                # 模型调用了一个当前菜单里不存在的工具（跳步尝试的另一种形态）
                db.log_tool_call(session_id, step, name, args, "工具不在当前合法清单", True, 0)
                final_text = "⚠️ 该操作在当前流程状态下不可用，请先完成前置步骤。"
                emit(AgentEvent("delta", {"text": final_text}))
                return _finish(session_id, step, final_text, degraded, model_name)

    else:
        # 步数耗尽：强制升级人工，绝不无限循环
        result = T.TOOL_IMPLS["escalate_to_human"](rag, session_id,
                                                   {"reason": "系统异常", "detail": "决策轮数达到上限"})
        final_text = result["message"]
        emit(AgentEvent("delta", {"text": final_text}))
        return _finish(session_id, MAX_STEPS, final_text, degraded, model_name)

    return _finish(session_id, step, final_text, degraded, model_name)


def confirm_draft_and_submit(rag: RagService, session_id: str, draft_id: str,
                             confirm_text: str) -> dict:
    """员工在确认卡上点"确认"后的服务端动作（不走模型）。

    理由：提交是终态业务动作，让模型参与反而引入不确定性。
    硬校验：草稿属于本会话 + 状态必须是 validated（确认接口先流转）+
    confirm 文本非空。确认成功后立即执行 submit（复用工具实现，享受幂等保护）。
    """
    draft = db.get_draft(draft_id)
    if draft is None or draft["session_id"] != session_id:
        raise PermissionError("草稿不存在或不属于当前会话。")
    if draft["status"] == "pending_confirm":
        # 确认动作：pending_confirm → validated（check_id 已在建草稿时消费）
        db.set_draft_status(draft_id, "validated")
        db.set_agent_state(session_id, "draft_confirmed")
    elif draft["status"] != "validated":
        raise PermissionError(f"草稿状态为 {draft['status']}，无法确认。")

    if not confirm_text.strip():
        raise PermissionError("缺少确认文本。")

    return T.TOOL_IMPLS["submit_ticket"](rag, session_id, {"draft_id": draft_id})


def cancel_draft(session_id: str, draft_id: str) -> dict:
    """员工取消草稿：状态置 cancelled，会话回到 idle（可重新走完整流程）。"""
    draft = db.get_draft(draft_id)
    if draft is None or draft["session_id"] != session_id:
        raise PermissionError("草稿不存在或不属于当前会话。")
    if draft["status"] != "pending_confirm":
        raise PermissionError(f"草稿状态为 {draft['status']}，只有待确认草稿能取消。")
    db.set_draft_status(draft_id, "cancelled")
    db.set_agent_state(session_id, "idle")
    return {"draft_id": draft_id, "status": "cancelled"}


def _rule_decide(session_id: str, question: str, messages: list[dict],
                 schema_list: list[dict]) -> dict:
    """降级模式决策器：无大模型 Key 时的确定性路由。

    用关键词规则模拟"模型决策"，产出与真实 Function Calling 完全相同的结构
    （{"type":"answer"} 或 {"type":"tool_calls", ...}），因此下游循环代码
    一行不用改。注释明确标注：**这是演示用模拟决策，生产环境必须接真模型**。

    意图判定优先级（第六阶段评测驱动的修正版）：
      0. 回填感知：本轮刚执行完的工具，其结果直接决定下一步（不再重复意图判定）
      1. 超标/争议咨询 → 查制度（不管句子里有没有金额，都是在问规则）
      2. 升级人工 / 违规请求拒绝 / 提交确认类 / 查进度 / 建单 / 发起报销
      3. 制度问答 → search_policy；兜底也查制度（答非所问时至少给引用）

    关键修正（对应评测用例 P-02~P-07 / T-02 / T-07 等）：
      - "提交报销申请要多长时间"是制度问句，不是提交动作——问句关键词优先；
      - extract/validate 不再在决策器内部"偷偷执行"，而是产出真正的工具调用，
        让 tool_call/tool_result 事件可观测（与真实 FC 行为对齐）；
      - 校验通过后不再无条件建单：只有员工明确要求"建单/发起报销"才建。
    """
    allowed = {s["function"]["name"] for s in schema_list}
    state = db.get_agent_state(session_id)

    def _search() -> dict:
        return {"type": "tool_calls", "tool_calls": [
            {"id": "a1", "name": "search_policy", "arguments": {"query": question, "top_k": 3}}]}

    def _missing_ask(missing: list) -> dict:
        q = "、".join({"amount": "报销金额", "nights": "住宿晚数", "city": "城市",
                       "invoice_date": "发票日期", "category": "费用类别"}.get(k, k)
                      for k in missing)
        return {"type": "answer", "content":
                f"好的，我可以帮您发起报销。还差几项信息：{q}。"
                f"例如可以说'住宿 3 晚共 1500 元，发票日期 2026-10-20'。"}

    # ---- 0. 回填感知：上一轮工具已经执行过 → 基于结果做"再决策" ----
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    last: dict = {}
    if tool_msgs:
        try:
            parsed = json.loads(tool_msgs[-1]["content"])
            if isinstance(parsed, dict):
                last = parsed
        except (json.JSONDecodeError, TypeError):
            last = {}

    if "hits" in last:
        # search_policy 已执行 → 降级拼接制度原文作答（与阶段 3 降级口径一致）
        return {"type": "answer",
                "content": llm.build_degraded_answer(question, last["hits"])}

    if "fields" in last and "missing" in last:
        # extract_invoice 已执行：没齐就追问，齐了就产出【显式】校验调用
        if last["missing"]:
            return _missing_ask(last["missing"])
        return {"type": "tool_calls", "tool_calls": [
            {"id": "b2", "name": "validate_invoice", "arguments": last["fields"]}]}

    if "violations" in last and "result" in last:
        # validate_invoice 已执行
        if last["result"] == "fail":
            lines = "；".join(v["message"] for v in last["violations"])
            return {"type": "answer", "content":
                    f"❌ 校验未通过：{lines}\n\n本次校验编号 {last.get('check_id', '')} 已留痕，"
                    f"补充合规信息后可重新发起。"}
        # pass：按员工原始诉求决定是否自动建单——明确要求"建"才建，
        # 只是"查一查/能不能报"的只答复校验结论（第六阶段评测修正点）
        first_user = next((m["content"] for m in messages if m.get("role") == "user"),
                          question)
        if "create_draft" in allowed and any(
                k in first_user for k in ("建", "发起报销", "直接报", "帮我报销")):
            check = db.get_latest_valid_check(session_id)
            if check:
                return {"type": "tool_calls", "tool_calls": [
                    {"id": "b3", "name": "create_draft",
                     "arguments": {"check_id": check["check_id"],
                                   "note": "住宿报销" if "住宿" in first_user else "费用报销"}}]}
        return {"type": "answer", "content":
                "✅ 发票校验通过，符合报销标准。如需创建报销单，请回复"
                "'帮我把报销单草稿建出来'，我会在建单后暂停流程等待您确认。"}

    if "ticket_id" in last and "status" in last:
        # get_ticket_status / submit_ticket 已执行 → 直接播报结果
        return {"type": "answer", "content":
                f"工单 {last['ticket_id']} 当前状态：{last['status']}。"
                f"报销审核进度也可以在财务系统'我的报销'模块查询。"}

    if last.get("message") and "escalated" in state:
        # escalate_to_human 已执行 → 转述转人工结果
        return {"type": "answer", "content": last["message"]}

    # ---- 1. 意图关键词组 ----
    asking_policy = any(k in question for k in (
        "标准", "多少", "多长时间", "多久", "规定", "制度", "能不能报", "可以报吗",
        "怎么报", "怎么", "哪些", "什么", "需要什么", "审批", "吗", "期限"))
    has_amount = any(k in question for k in ("元", "块", "¥", "￥"))

    # 1.1 超标/争议咨询：句子里带不带金额都是在问规则 → 查制度
    if any(k in question for k in ("超标", "超了")) and any(
            k in question for k in ("怎么办", "怎么处理", "怎么算", "承担", "掏钱", "吗")):
        return _search()

    # 1.2 升级人工：Agent 的"承认做不到"出口
    if "escalate_to_human" in allowed and any(
            k in question for k in ("转人工", "人工客服", "解决不了", "投诉")):
        return {"type": "tool_calls", "tool_calls": [
            {"id": "e1", "name": "escalate_to_human",
             "arguments": {"reason": "用户异议", "detail": question[:100]}}]}

    # 1.3 违规/作弊请求：明确拒绝并给出合规指引，不调任何工具、不编造规则
    if any(k in question for k in ("改成", "改一下", "篡改", "伪造", "PS", "修图",
                                   "重复报", "再报一次", "编一个", "虚假", "私了")):
        return {"type": "answer", "content":
                "❌ 这个忙我不能帮：伪造或篡改票据、重复报销、虚假报销属于严重违规，"
                "一经查实将追回全部款项，视情节给予警告直至解除劳动合同，"
                "涉嫌违法的会移交司法机关。\n\n"
                "合规做法：发票问题联系开票方重新开具；无票支出需提供替代凭证并附书面"
                "情况说明，经部门负责人与财务双重审批。"}

    # 1.4 查进度（优先于提交类：'刚才提交的那个工单到哪一步了'不是提交动作）
    if "get_ticket_status" in allowed and any(
            k in question for k in ("到哪一步", "进度", "工单状态", "查一下工单",
                                    "看看上次", "上次提交", "提交的那个")):
        return {"type": "tool_calls", "tool_calls": [
            {"id": "s1", "name": "get_ticket_status", "arguments": {}}]}

    # 1.5 提交/确认类
    if state == "submitted" and any(k in question for k in ("确认", "提交", "成功")):
        # 提交是终态动作，由确认接口直接完成（不经模型）；这里只做状态播报
        return {"type": "answer", "content":
                "您的报销单已通过确认卡确认并提交完成。提交动作由确认接口直接执行，"
                "无需再次提交；可在财务系统'我的报销'模块查询审核进度。"}
    if any(k in question for k in ("提交", "打给我", "打款", "到账")) and not asking_policy:
        if "submit_ticket" in allowed:
            draft = db.get_pending_draft(session_id)
            if draft:
                return {"type": "tool_calls", "tool_calls": [
                    {"id": "c1", "name": "submit_ticket",
                     "arguments": {"draft_id": draft["draft_id"]}}]}
        return {"type": "answer", "content":
                "提交需要走完整流程：校验发票 → 创建报销单草稿 → 您在确认卡上确认 → "
                "系统提交付款。目前还没有可提交的报销单，请先提供发票信息。"}

    # 1.6 建单意图：必须先有有效校验凭证（服务端状态机 R1-R7 兜底）
    if any(k in question for k in ("建草稿", "建单", "创建报销", "草稿建", "建出来",
                                   "建一个报销", "把报销单草稿", "草稿建出来")):
        check = db.get_latest_valid_check(session_id) if "create_draft" in allowed else None
        if check:
            return {"type": "tool_calls", "tool_calls": [
                {"id": "b4", "name": "create_draft",
                 "arguments": {"check_id": check["check_id"],
                               "note": "住宿报销" if "住宿" in question else "费用报销"}}]}
        return {"type": "answer", "content":
                "创建报销单前必须先通过发票校验（防止无凭证建单）。请提供发票信息"
                "（金额、城市、入住晚数、发票日期），我先帮您校验，通过后再建单。"}

    # 1.7 发起报销：明确的报销动作短语，或【发票语境下的】金额 → 抽取要素。
    # 注意："金额超过 20000 元需要谁审批"这类制度问句虽然带金额，
    # 但没有发票语境词，不能误判为发起报销（第六阶段评测 P-05 修正点）。
    invoice_context = any(k in question for k in (
        "发票", "住宿", "花了", "金额是", "费用是", "晚", "报销这笔", "打车", "餐费"))
    if "extract_invoice" in allowed and (
            invoice_context and has_amount or any(
                k in question for k in ("帮我报销", "我要报销", "发起报销", "报一笔",
                                        "继续校验", "补上", "补充"))):
        return {"type": "tool_calls", "tool_calls": [
            {"id": "b1", "name": "extract_invoice", "arguments": {"raw_text": question}}]}

    # ---- 2. 制度问答 / 兜底：查制度（有引用总比硬编强） ----
    return _search()


def _finish(session_id: str, steps: int, text: str, degraded: bool, model_name: str,
            interrupted: bool = False, escalated: bool = False) -> dict:
    """循环结束时的统一摘要（供 done 事件与调用方落库）。"""
    return {"steps": steps, "answer": text, "degraded": degraded,
            "model": model_name, "interrupted": interrupted, "escalated": escalated}
