# -*- coding: utf-8 -*-
"""
第六阶段 · 评测用例执行器
==========================
配套 evaluation/cases.json（30 条评测用例）使用，提供两种运行模式：

1. 静态校验模式（默认，无需启动服务、无需 LLM Key）
   python evaluation/run_eval.py
   —— 校验用例文件的结构完整性、字段合法性、分类数量是否达标（10/10/5/5）。

2. 在线实测模式（需先启动服务并登录）
   python evaluation/run_eval.py --live --base-url http://127.0.0.1:8020
   —— 逐条把用例发到 /api/chat，通过 SSE 事件流收集实际调用的工具序列，
      与 expected_tools 做前缀比对，输出通过率报告到 evaluation/report.json。

判定规则（与 AgentLoop 实现对齐）：
- expected_tools 为空数组 []：期望全程不调用任何写操作/读操作工具（纯拒答场景）。
- expected_tools 为工具序列：实际调用序列须与期望序列【前缀一致】
  （允许多轮中系统自动补调只读工具，因此只校验期望工具按顺序出现）。
- 多轮用例（category=multi_turn）会先发送 context 字段作为第一轮，
  再发送 question 作为第二轮，校验第二轮的工具调用。
"""

import argparse
import json
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

# Windows GBK 控制台兼容：强制标准输出走 UTF-8，避免中文/特殊字符报错
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ---------------------------------------------------------------------------
# 路径与常量
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
CASES_FILE = BASE_DIR / "cases.json"
REPORT_FILE = BASE_DIR / "report.json"

REQUIRED_FIELDS = {"question", "expected_tools", "expected_behavior", "category"}
VALID_CATEGORIES = {"policy_qa", "tool_call", "violation_intent", "multi_turn"}
CATEGORY_QUOTA = {"policy_qa": 10, "tool_call": 10, "violation_intent": 5, "multi_turn": 5}

# 已知工具名（与 app/agent/tools.py 的 TOOL_SCHEMAS 对齐，用于容错校验）
KNOWN_TOOLS = {
    "search_policy", "extract_invoice", "validate_invoice", "create_draft",
    "update_draft", "submit_ticket", "get_ticket_status", "escalate_to_human",
}


# ---------------------------------------------------------------------------
# 一、静态校验模式
# ---------------------------------------------------------------------------
def static_check() -> bool:
    """校验 cases.json 的结构与数量约束，全部通过返回 True。"""
    cases = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    ok = True
    print(f"共加载 {len(cases)} 条用例：{CASES_FILE}")

    # 逐条字段校验
    ids = set()
    for i, c in enumerate(cases, 1):
        missing = REQUIRED_FIELDS - set(c)
        if missing:
            print(f"  [X] 第 {i} 条缺少字段: {missing}")
            ok = False
        if c.get("category") not in VALID_CATEGORIES:
            print(f"  [X] 第 {i} 条 category 非法: {c.get('category')}")
            ok = False
        for t in c.get("expected_tools", []):
            if t not in KNOWN_TOOLS:
                print(f"  [X] 第 {i} 条 (id={c.get('id')}) 引用了未知工具: {t}")
                ok = False
        if c.get("id") in ids:
            print(f"  [X] 第 {i} 条 id 重复: {c.get('id')}")
            ok = False
        ids.add(c.get("id"))

    # 分类数量校验（须满足 10/10/5/5 配额）
    counts = {}
    for c in cases:
        counts[c["category"]] = counts.get(c["category"], 0) + 1
    print("分类分布：")
    for cat, quota in CATEGORY_QUOTA.items():
        actual = counts.get(cat, 0)
        mark = "OK" if actual == quota else "X"
        print(f"  [{mark}] {cat}: {actual}/{quota}")
        if actual != quota:
            ok = False

    print("静态校验通过" if ok else "静态校验未通过")
    return ok


# ---------------------------------------------------------------------------
# 二、在线实测模式
# ---------------------------------------------------------------------------
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """禁止跟随重定向的 handler（登录 303 时保留 Set-Cookie 头）。"""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _http(url: str, payload=None, cookie: str | None = None, form: bool = False):
    """
    极简 HTTP 客户端，返回 (状态码, 响应头, 字节流)。
    - payload 为 dict 时发送 POST：form=True 走表单编码（登录接口），
      否则走 JSON（/api/chat 接口）。
    """
    if payload is not None:
        if form:
            body = urllib.parse.urlencode(payload).encode("utf-8")
            content_type = "application/x-www-form-urlencoded"
        else:
            body = json.dumps(payload).encode("utf-8")
            content_type = "application/json"
        req = urllib.request.Request(url, data=body, method="POST")
        req.add_header("Content-Type", content_type)
    else:
        req = urllib.request.Request(url)
    if cookie:
        req.add_header("Cookie", cookie)
    try:
        resp = urllib.request.urlopen(req, timeout=180)
        return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read()


def login(base_url: str, username: str, password: str) -> str:
    """表单登录 POST /login，返回 Cookie 字符串（JWT 在 HttpOnly Cookie 中）。"""
    # 登录成功是 303 重定向 + Set-Cookie：必须禁用自动跟随重定向，
    # 否则 urllib 会丢掉中间响应头里的 Cookie
    no_redirect = urllib.request.build_opener(_NoRedirect)
    body = urllib.parse.urlencode(
        {"username": username, "password": password}).encode("utf-8")
    req = urllib.request.Request(
        f"{base_url}/login", data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        resp = no_redirect.open(req, timeout=30)
        headers = resp.headers
    except urllib.error.HTTPError as e:
        headers = e.headers
    set_cookie = headers.get("Set-Cookie", "")
    if not set_cookie:
        raise RuntimeError("登录失败：响应中未携带 Set-Cookie（账号或密码错误）")
    return set_cookie.split(";")[0]


def _collect_sse_tools(base_url: str, cookie: str, question: str,
                       session_id: str | None = None) -> dict:
    """
    调用 /api/chat（SSE 流式），解析事件流并收集：
    - session_id: 服务端回传的会话 id（多轮用例复用同一会话）
    - tools: 实际按顺序调用的工具名列表（tool_call 事件）
    - answer: 拼接的全部 delta 文本
    - pending: 是否出现 pending_draft（create_draft 强制中断点）
    """
    tools, answer, pending = [], [], False
    sid, draft_id = session_id, None
    payload = {"session_id": session_id, "question": question}
    status, _, resp = _http(f"{base_url}/api/chat", payload, cookie)
    if status != 200:
        return {"session_id": session_id, "tools": [], "draft_id": None,
                "answer": f"[HTTP {status}]", "pending": False}

    # SSE 帧格式：event: xxx \n data: {...}
    event, buf = None, resp.decode("utf-8", errors="replace")
    for line in buf.splitlines():
        if line.startswith("event:"):
            event = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            raw = line.split(":", 1)[1].strip()
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if event == "session":
                sid = data.get("session_id") or sid
            elif event == "tool_call":
                tools.append(data.get("tool") or data.get("name") or "?")
            elif event == "delta":
                answer.append(data.get("text") or data.get("content") or "")
            elif event == "pending_draft":
                pending = True
                draft_id = data.get("draft_id")
    return {"session_id": sid, "draft_id": draft_id, "tools": tools,
            "answer": "".join(answer), "pending": pending}


def _match_prefix(expected: list, actual: list) -> bool:
    """顺序子序列比对：期望工具须按顺序出现在实际序列中（允许夹杂其他工具）。"""
    if not expected:
        return actual == []          # 期望空 = 全程不得调用任何工具
    it = iter(actual)
    for t in expected:
        if not any(a == t for a in it):
            return False
    return True


def live_check(base_url: str) -> bool:
    """在线逐条实测：登录 → (前置步骤) → 发问 → 收集工具调用 → 比对 → 输出报告。

    setup DSL（可选字段，用于有状态用例的前置铺垫，在同一会话内顺序执行）：
      {"send": "文本"}        —— 作为员工再发一轮消息
      {"confirm": true}       —— 把会话当前待确认草稿走确认接口提交（等价点确认卡）
    """
    cases = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    cookie = login(base_url, "emp001", "emp123456")
    print(f"已登录 {base_url}（emp001），开始评测 {len(cases)} 条用例…\n")

    results, passed = [], 0
    for c in cases:
        sid = None
        if c["category"] == "multi_turn" and c.get("context"):
            # 多轮：先发 context 轮，并在同一会话内续发正式问题
            # （服务端 agent_state / 会话历史按 session_id 持久化）
            first = _collect_sse_tools(base_url, cookie, c["context"])
            sid = first["session_id"]

        # 执行前置步骤（同一会话内，为有状态用例铺垫服务端状态）
        for step in c.get("setup", []):
            if "send" in step:
                r = _collect_sse_tools(base_url, cookie, step["send"], sid)
                sid = r["session_id"] or sid
                c["_pending_draft_id"] = r.get("draft_id") or c.get("_pending_draft_id")
            elif step.get("confirm") and c.get("_pending_draft_id"):
                _http(f"{base_url}/api/drafts/{c.pop('_pending_draft_id')}/confirm",
                      {"session_id": sid, "confirm_text": "确认提交"}, cookie)

        actual = _collect_sse_tools(base_url, cookie, c["question"], sid)

        hit = _match_prefix(c["expected_tools"], actual["tools"])
        passed += hit
        mark = "PASS" if hit else "FAIL"
        print(f"  [{mark}] {c['id']} {c['category']:<16} "
              f"期望={c['expected_tools']} 实际={actual['tools']}")
        results.append({
            "id": c["id"], "category": c["category"], "pass": hit,
            "expected_tools": c["expected_tools"], "actual_tools": actual["tools"],
            "answer_preview": actual["answer"][:120],
            "pending_draft": actual["pending"],
        })
        time.sleep(0.2)  # 轻微限速，避免压垮本地服务

    rate = passed / len(cases) * 100 if cases else 0
    report = {"total": len(cases), "passed": passed, "pass_rate": round(rate, 1),
              "results": results}
    REPORT_FILE.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    print(f"\n通过率：{passed}/{len(cases)} = {rate:.1f}%，报告已写入 {REPORT_FILE}")
    return passed == len(cases)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="第六阶段评测用例执行器")
    parser.add_argument("--live", action="store_true",
                        help="在线实测模式（默认为静态校验模式）")
    parser.add_argument("--base-url", default="http://127.0.0.1:8020",
                        help="服务地址，默认 http://127.0.0.1:8020")
    args = parser.parse_args()

    success = live_check(args.base_url) if args.live else static_check()
    sys.exit(0 if success else 1)
