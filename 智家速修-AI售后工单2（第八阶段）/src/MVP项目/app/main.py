# -*- coding: utf-8 -*-
"""FastAPI 应用入口：路由注册、SSR 页面、REST/SSE 接口、统一错误处理。

页面（服务端渲染，SaaS 风格）：
  GET  /login           登录页
  GET  /                员工问答页（打字机 + 引用来源）
  GET  /admin/kb        制度知识库管理页（上传/删除/重新向量化）
  GET  /admin/prompts   提示词模板管理页（编辑立即生效，支持回滚历史版本）
  GET  /admin/audit     对话审计页（按员工/日期/已标记筛选，标记疑似违规）

接口：
  POST /api/chat        SSE 流式问答（核心）
  其余见 README 的接口清单表
"""

import json
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from . import auth, db, llm
from .agent import loop as agent_loop          # ★阶段5：Agent 执行循环
from .config import Config
from .rag import parse_upload, rag_service
from .ratelimit import SlidingWindowLimiter    # ★阶段7：按用户限流
from .security import detect_injection, mask_pii   # ★阶段7：注入检测 + PII 脱敏

# 问答限流器：默认 60 次/分钟/用户（.env 的 CHAT_RATE_LIMIT_PER_MIN 可调）
chat_limiter = SlidingWindowLimiter(Config.CHAT_RATE_LIMIT_PER_MIN)

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent.parent / "templates"))


# ----------------------------------------------------------------------------
# 启动与生命周期
# ----------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动流程：建表 → 种子数据 → 加载内存索引 → 首次运行自动入库种子语料。"""
    db.init_db()
    db.seed()
    rag_service.refresh()
    docs, _chunks = db.count_docs_and_chunks()
    if docs == 0 and Config.SEED_DOC.exists():
        # 首次启动把“报销制度.md”自动入库，保证开箱即可演示问答
        admin = db.get_user_by_username("admin")
        text = Config.SEED_DOC.read_text(encoding="utf-8")
        rag_service.add_document(Config.SEED_DOC.name, text, admin["id"])
        print(f"[初始化] 已自动入库种子语料：{Config.SEED_DOC.name}")
    print(f"[启动] 知识库：{rag_service.stats()}；大模型：{llm.llm_mode()}")
    yield


app = FastAPI(title="报销助手智能体系统 · 第三阶段", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(Path(__file__).resolve().parent.parent / "static")), name="static")


# ----------------------------------------------------------------------------
# 统一错误处理：所有接口错误都返回 {code, message}，且 message 为中文
# ----------------------------------------------------------------------------

@app.exception_handler(HTTPException)
async def http_exception_handler(_req: Request, exc: HTTPException):
    """业务层抛出的 HTTPException(detail={"code","message"}) 统一转成 JSON。"""
    if isinstance(exc.detail, dict) and "code" in exc.detail:
        return JSONResponse(status_code=exc.status_code, content=exc.detail)
    return JSONResponse(status_code=exc.status_code, content={"code": 5000, "message": str(exc.detail)})


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(_req: Request, exc: RequestValidationError):
    """参数校验失败（422）统一转成 1003 + 中文提示。"""
    return JSONResponse(status_code=422, content={"code": 1003, "message": "请求参数格式不正确：" + str(exc.errors()[:1])})


def api_error(code: int, message: str, status: int) -> JSONResponse:
    """同步接口的前置校验错误：SSE 流还没开始，直接返回标准 JSON。"""
    return JSONResponse(status_code=status, content={"code": code, "message": message})


def redirect(path: str, msg: str) -> RedirectResponse:
    """带中文提示的重定向。

    msg 必须先 quote：Location 头里直接放中文/空格不规范，
    curl 等客户端会拒收或截断，浏览器虽然宽容但会显示原始编码。
    """
    return RedirectResponse(f"{path}?msg={quote(msg)}", status_code=303)


def sse(event: str, data: dict) -> str:
    """SSE 帧：`event: xxx\\ndata: {...}\\n\\n`。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# ----------------------------------------------------------------------------
# 页面鉴权辅助（页面用重定向，接口用 JSON —— 两套行为刻意分开）
# ----------------------------------------------------------------------------

def page_user(request: Request) -> dict | None:
    return auth.get_user_from_request(request)


# ----------------------------------------------------------------------------
# 登录 / 登出
# ----------------------------------------------------------------------------

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if page_user(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login")
def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
    user = db.get_user_by_username(username)
    if user is None or not auth.verify_password(password, user["password_hash"]):
        return templates.TemplateResponse(
            request, "login.html",
            {"error": "用户名或密码错误"},
            status_code=401,
        )
    # 签发 JWT 并写入 HttpOnly Cookie（防 XSS 窃取）
    token = auth.create_token(user["id"], user["role"])
    resp = RedirectResponse("/", status_code=303)
    resp.set_cookie(Config.COOKIE_NAME, token, httponly=True, samesite="lax")
    return resp


@app.get("/logout")
def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(Config.COOKIE_NAME)
    return resp


# ----------------------------------------------------------------------------
# 员工问答页
# ----------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def chat_page(request: Request):
    user = page_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    stats = rag_service.stats()
    prompt = db.get_active_prompt()
    return templates.TemplateResponse(request, "chat.html", {
        "user": user,
        "llm_mode": llm.llm_mode(),
        "kb_docs": stats["documents"],
        "kb_chunks": stats["chunks"],
        "prompt_name": prompt["name"] if prompt else "（未配置）",
    })


# ----------------------------------------------------------------------------
# 管理后台页面（管理员专用）
# ----------------------------------------------------------------------------

def admin_guard(request: Request) -> dict | RedirectResponse | HTMLResponse:
    """管理页三态守卫：未登录→登录页；非管理员→403 页；通过→返回用户。"""
    user = page_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if user["role"] != "admin":
        return HTMLResponse("<h3>403 无权访问：该功能仅管理员可用</h3><p><a href='/'>返回问答</a></p>",
                            status_code=403)
    return user


@app.get("/admin/kb", response_class=HTMLResponse)
def kb_page(request: Request):
    result = admin_guard(request)
    if not isinstance(result, dict):
        return result
    return templates.TemplateResponse(request, "kb.html", {
        "user": result,
        "docs": db.list_documents(),
        "msg": request.query_params.get("msg", ""),
        "embed_mode": rag_service.stats()["embed_mode"],
    })


@app.get("/admin/prompts", response_class=HTMLResponse)
def prompts_page(request: Request):
    result = admin_guard(request)
    if not isinstance(result, dict):
        return result
    return templates.TemplateResponse(request, "prompts.html", {
        "user": result,
        "active": db.get_active_prompt(),
        "prompts": db.list_prompts(),
        "msg": request.query_params.get("msg", ""),
    })


@app.get("/admin/audit", response_class=HTMLResponse)
def audit_page(request: Request):
    result = admin_guard(request)
    if not isinstance(result, dict):
        return result
    params = request.query_params
    user_id = int(params.get("user_id") or 0)
    start = params.get("start") or None
    end = params.get("end") or None
    flagged_only = params.get("flagged") == "1"
    return templates.TemplateResponse(request, "audit.html", {
        "user": result,
        "rows": db.list_audit(user_id, start, end, flagged_only),
        "users": db.list_users(),
        "filters": {"user_id": user_id, "start": start or "", "end": end or "",
                     "flagged": "1" if flagged_only else ""},
        "msg": params.get("msg", ""),
    })


# ----------------------------------------------------------------------------
# 知识库管理接口（管理员）
# ----------------------------------------------------------------------------

@app.post("/admin/kb/upload")
async def kb_upload(request: Request, file: UploadFile = File(...),
                    user: dict = Depends(auth.require_admin_api)):
    """上传制度文档：校验后缀/大小 → 解析 → 切片 → 向量化 → 入库。"""
    filename = file.filename or "未命名"
    suffix = Path(filename).suffix.lower()
    if suffix not in Config.UPLOAD_SUFFIXES:
        return redirect("/admin/kb", "上传失败：仅支持 .md / .pdf 文件")
    raw = await file.read()
    if len(raw) > Config.UPLOAD_MAX_BYTES:
        return redirect("/admin/kb", "上传失败：文件超过 10MB 上限")
    try:
        text, _ = parse_upload(filename, raw)
        if not text.strip():
            raise ValueError("未能从文件中提取到文本内容")
        info = await run_in_threadpool(rag_service.add_document, filename, text, user["id"])
    except ValueError as exc:
        return redirect("/admin/kb", f"上传失败：{exc}")
    return redirect("/admin/kb", f"上传成功：{filename}（{info['chunks']} 个切片已向量化入库）")


@app.post("/admin/kb/{doc_id}/delete")
def kb_delete(doc_id: int, user: dict = Depends(auth.require_admin_api)):
    doc = db.get_document(doc_id)
    name = doc["filename"] if doc else f"id={doc_id}"
    rag_service.delete_document(doc_id)
    return redirect("/admin/kb", f"已删除文档：{name}")


@app.post("/admin/kb/revectorize")
async def kb_revectorize(user: dict = Depends(auth.require_admin_api)):
    """全部文档重新向量化（更换 Embedding 供应商/维度后用）。"""
    try:
        count = await run_in_threadpool(rag_service.revectorize_all)
        return redirect("/admin/kb", f"重新向量化完成，共 {count} 个切片")
    except Exception as exc:
        return redirect("/admin/kb", f"重新向量化失败：{exc}")


# ----------------------------------------------------------------------------
# 提示词模板接口（管理员）—— 修改后立即生效：问答接口每次都读数据库里的生效模板
# ----------------------------------------------------------------------------

@app.post("/admin/prompts/save")
def prompt_save(request: Request, content: str = Form(...), name: str = Form(""),
                user: dict = Depends(auth.require_admin_api)):
    content = content.strip()
    if not content:
        return redirect("/admin/prompts", "提示词内容不能为空")
    if not name:
        total = len(db.list_prompts()) + 1
        name = f"模板 v{total}"
    db.save_prompt(name, content, user["id"])
    return redirect("/admin/prompts", f"已保存并生效：{name}")


@app.post("/admin/prompts/{prompt_id}/activate")
def prompt_activate(prompt_id: int, user: dict = Depends(auth.require_admin_api)):
    db.activate_prompt(prompt_id, user["id"])
    return redirect("/admin/prompts", f"已切换生效模板（id={prompt_id}）")


# ----------------------------------------------------------------------------
# 对话审计接口（管理员）
# ----------------------------------------------------------------------------

@app.post("/admin/audit/{message_id}/flag")
def audit_flag(request: Request, message_id: int, reason: str = Form(""),
               user: dict = Depends(auth.require_admin_api)):
    reason = reason.strip() or "疑似违规报销，需人工核查"
    db.set_flag(message_id, reason, user["id"])
    # 重定向回审计页并保留当前筛选条件（表单里带 hidden 字段）
    back = request.query_params.get("back") or "/admin/audit"
    sep = "&" if "?" in back else "?"
    return RedirectResponse(f"{back}{sep}msg={quote('已标记为疑似违规')}", status_code=303)


@app.post("/admin/audit/{message_id}/unflag")
def audit_unflag(request: Request, message_id: int,
                 user: dict = Depends(auth.require_admin_api)):
    db.clear_flag(message_id)
    back = request.query_params.get("back") or "/admin/audit"
    sep = "&" if "?" in back else "?"
    return RedirectResponse(f"{back}{sep}msg={quote('已取消标记')}", status_code=303)


# ----------------------------------------------------------------------------
# 问答 API
# ----------------------------------------------------------------------------

@app.get("/api/health")
def health():
    stats = rag_service.stats()
    return {"code": 0, "message": "ok", "data": {
        "llm_mode": llm.llm_mode(),
        "embed_mode": stats["embed_mode"],
        "documents": stats["documents"],
        "chunks": stats["chunks"],
    }}


class ChatRequest(BaseModel):
    session_id: Optional[str] = None   # 为空表示新会话，首个事件会回传新建的 id
    question: str


@app.post("/api/sessions")
def create_session(user: dict = Depends(auth.require_api_user)):
    sid = db.create_session(user["id"])
    return {"code": 0, "message": "ok", "data": {"session_id": sid}}


@app.get("/api/sessions")
def my_sessions(user: dict = Depends(auth.require_api_user)):
    rows = db.list_sessions_by_user(user["id"])
    return {"code": 0, "message": "ok", "data": {"sessions": [
        {"id": r["id"], "title": r["title"], "updated_at": r["updated_at"]} for r in rows
    ]}}


@app.get("/api/sessions/{sid}/messages")
def session_messages(sid: str, user: dict = Depends(auth.require_api_user)):
    session = db.get_session(sid)
    if session is None:
        return api_error(2001, "会话不存在或已被删除", 404)
    if session["user_id"] != user["id"]:
        return api_error(2002, "会话不属于当前用户", 403)
    return {"code": 0, "message": "ok", "data": {"messages": db.get_messages(sid)}}


@app.post("/api/chat")
async def chat(payload: ChatRequest, user: dict = Depends(auth.require_api_user)):
    """SSE 流式问答。错误分两层（阶段 2 设计的核心约束）：

    第一层（本函数体内、流开始之前）——本地能 100% 判定的问题：
        参数为空/超长、会话不存在/不属于本人、知识库为空。
        SSE 一旦开始 HTTP 状态码就不能改了，所以这类错误必须前置，直接返回 JSON。

    第二层（生成器内部）——外部依赖的不确定性：
        Embedding、大模型调用失败。此时只能通过流内 error 事件告知前端，
        并自动降级为"检索原文展示"，保证用户永远拿得到东西。
    """
    question = payload.question.strip()
    if not question:
        return api_error(1001, "问题不能为空", 400)
    if len(question) > Config.MAX_QUESTION_LEN:
        return api_error(1002, f"问题长度超限（最多 {Config.MAX_QUESTION_LEN} 字）", 400)
    if not rag_service.ready:
        return api_error(3001, "知识库为空，请联系管理员上传制度文档", 503)

    # ---- ★阶段7 加固 1：按用户限流（LLM 配额保护，超出返回 429）----
    if not chat_limiter.hit(user["id"]):
        return api_error(
            1003,
            f"提问太频繁了，请稍后再试（每人每分钟最多 {Config.CHAT_RATE_LIMIT_PER_MIN} 次）",
            429,
        )

    # ---- ★阶段7 加固 2：提示词注入前置拦截（代码硬闸门，优先于一切提示词约束）----
    # 命中注入特征的问题不进 RAG、不进 LLM，直接流内返回固定拒绝话术并留痕，
    # 保证"用户说忽略之前的指令"时行为是确定性拒绝，而不是寄希望于模型自觉。
    injection_hit = detect_injection(question)
    if injection_hit:
        def _refuse_stream():
            # 复用 SSE 协议输出：session → status → delta → done，前端零改动
            session_id = payload.session_id or db.create_session(user["id"], "注入拦截")
            yield sse("session", {"session_id": session_id})
            yield sse("status", {"stage": "blocked", "text": "已按安全策略拦截"})
            yield sse("delta", {"text": Config.INJECTION_REFUSAL})
            yield sse("done", {"message_id": 0, "degraded": False,
                               "latency_ms": 0, "blocked": injection_hit})
        return StreamingResponse(_refuse_stream(), media_type="text/event-stream")

    # ---- 会话校验（不合法直接 4xx，不带进流里） ----
    session = db.get_session(payload.session_id) if payload.session_id else None
    if session is None:
        session_id = db.create_session(user["id"], question[:20])
        is_new_session = True
    else:
        if session["user_id"] != user["id"]:
            return api_error(2002, "会话不属于当前用户", 403)
        session_id = session["id"]
        is_new_session = False

    async def event_stream():
        # （1）会话事件：新会话时把 id 回传给前端保存
        if is_new_session:
            yield sse("session", {"session_id": session_id})
        yield sse("status", {"stage": "retrieving", "text": "正在思考…"})

        # （2）取历史（先取再落库，当前问题就不会混进历史）
        history = db.get_history(session_id, Config.HISTORY_ROUNDS, Config.HISTORY_CHAR_LIMIT)
        db.insert_message(session_id, "user", question)

        # （3）★阶段5：把固定流程换成 Agent 执行循环。
        # AgentLoop 是同步函数（内部可能有多次模型/工具调用），放线程池执行；
        # 它通过 emit 回调把过程事件（工具调用/草稿待确认/文本片段）推到
        # asyncio.Queue，本生成器从队列取出来转成 SSE 事件流。
        # 这样既不阻塞事件循环，又保持"边执行边推送"的实时性。
        import asyncio

        q: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()
        SENTINEL = object()          # 结束哨兵：worker 线程跑完时投递

        def emit(ev):
            """AgentEvent 回调：从工作线程安全地投递到事件循环队列。"""
            loop.call_soon_threadsafe(q.put_nowait, ev)

        # 线程通信：result_box 容器接 summary，队列只传过程事件与结束哨兵
        result_box: dict = {}

        def worker():
            try:
                result_box["summary"] = agent_loop.run_turn(
                    rag_service, session_id, question, history, emit)
            except Exception as exc:                     # 顶层兜底：不让线程异常静默
                result_box["error"] = str(exc)
            finally:
                loop.call_soon_threadsafe(q.put_nowait, SENTINEL)

        import threading
        threading.Thread(target=worker, daemon=True).start()

        # （4）消费队列：AgentLoop 的每个过程事件实时转成 SSE 帧
        parts: list[str] = []
        pending_draft = None
        while True:
            item = await q.get()
            if item is SENTINEL:
                break
            kind, data = item.kind, item.data
            if kind == "delta":
                parts.append(data["text"])
                yield sse("delta", data)
            elif kind == "tool_call":
                yield sse("tool_call", data)
            elif kind == "tool_result":
                yield sse("tool_result", data)
            elif kind == "pending_draft":
                # ★强制中断：草稿内容流式发给员工，循环已暂停
                pending_draft = data
                yield sse("pending_draft", data)

        # （5）收尾：中断模式不落 assistant 消息（确认/取消后再记），正常模式落库
        summary = result_box.get("summary")
        if result_box.get("error"):
            yield sse("error", {"code": 5000, "message": f"Agent 执行异常：{result_box['error']}"})
            yield sse("done", {"message_id": 0, "degraded": True, "latency_ms": 0})
            return

        if summary and summary.get("answer") and not summary.get("interrupted"):
            message_id = db.insert_message(
                session_id, "assistant", summary["answer"],
                citations=None, model=summary.get("model", Config.LLM_MODEL),
                degraded=1 if summary.get("degraded") else 0, latency_ms=0,
            )
        else:
            message_id = 0

        yield sse("done", {
            "message_id": message_id,
            "model": summary.get("model", Config.LLM_MODEL) if summary else Config.LLM_MODEL,
            "degraded": bool(summary.get("degraded")) if summary else True,
            "steps": summary.get("steps", 0) if summary else 0,
            "interrupted": bool(summary.get("interrupted")) if summary else False,
            "latency_ms": 0,
        })

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ============================================================================
# ★阶段5新增：草稿确认 / 取消（human-in-the-loop 的另一半）
# ============================================================================

@app.post("/api/drafts/{draft_id}/confirm")
async def draft_confirm(draft_id: str, payload: dict, user: dict = Depends(auth.require_api_user)):
    """员工在确认卡上点"确认"：服务端校验后直接提交，不再经过模型。

    请求体：{"confirm_text": "确认提交"} —— 必须与用户意图一致（前端按钮固定传）。
    返回 ticket_id；失败返回 4xx + 中文错误。
    """
    session_id = payload.get("session_id", "")
    session = db.get_session(session_id)
    if session is None or session["user_id"] != user["id"]:
        return api_error(2002, "会话不存在或不属于当前用户", 403)
    confirm_text = (payload.get("confirm_text") or "").strip()
    if not confirm_text:
        return api_error(1003, "缺少确认文本", 400)
    try:
        result = await run_in_threadpool(
            agent_loop.confirm_draft_and_submit, rag_service, session_id, draft_id, confirm_text)
    except PermissionError as exc:
        return api_error(5002, str(exc), 403)
    # 落一条 assistant 消息，把提交结果写进对话（审计页可见）
    db.insert_message(session_id, "assistant",
                      f"✅ 报销单已提交，单号 {result['ticket_id']}。"
                      f"{'（重复确认，幂等返回原单号）' if result.get('idempotent') else ''}")
    return {"code": 0, "message": "ok", "data": result}


@app.post("/api/drafts/{draft_id}/cancel")
async def draft_cancel(draft_id: str, payload: dict, user: dict = Depends(auth.require_api_user)):
    """员工取消草稿：状态置 cancelled，会话回到 idle，可重新发起。"""
    session_id = payload.get("session_id", "")
    session = db.get_session(session_id)
    if session is None or session["user_id"] != user["id"]:
        return api_error(2002, "会话不存在或不属于当前用户", 403)
    try:
        result = await run_in_threadpool(agent_loop.cancel_draft, session_id, draft_id)
    except PermissionError as exc:
        return api_error(5003, str(exc), 403)
    db.insert_message(session_id, "assistant", "已取消该报销草稿。如需重新发起，请再次描述报销内容。")
    return {"code": 0, "message": "ok", "data": result}


@app.get("/api/sessions/{sid}/toolcalls")
def session_toolcalls(sid: str, user: dict = Depends(auth.require_api_user)):
    """某会话的 Agent 轨迹回放：每轮决策的工具名/参数/耗时/是否被拦截。"""
    session = db.get_session(sid)
    if session is None:
        return api_error(2001, "会话不存在或已被删除", 404)
    if session["user_id"] != user["id"] and user["role"] != "admin":
        return api_error(2002, "会话不属于当前用户", 403)
    return {"code": 0, "message": "ok", "data": {"tool_calls": db.list_tool_calls(sid)}}
