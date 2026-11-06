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
from .config import Config
from .rag import parse_upload, rag_service

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
        yield sse("status", {"stage": "retrieving", "text": "正在检索制度…"})

        # （2）取历史（先取再落库，当前问题就不会混进历史）
        history = db.get_history(session_id, Config.HISTORY_ROUNDS, Config.HISTORY_CHAR_LIMIT)
        db.insert_message(session_id, "user", question)

        # （3）混合检索（阻塞操作放线程池，不卡事件循环）
        try:
            hits = await run_in_threadpool(rag_service.search, question)
        except Exception as exc:
            yield sse("error", {"code": 3003, "message": f"检索失败：{exc}"})
            yield sse("done", {"message_id": 0, "degraded": True, "latency_ms": 0})
            return
        yield sse("citations", {"items": hits})
        yield sse("status", {"stage": "generating", "text": "正在生成回答…"})

        # （4）组装 messages：system=库里生效的提示词（改完立即生效的关键）+ 6 轮历史 + 本轮条文
        prompt_row = db.get_active_prompt()
        system_prompt = prompt_row["content"] if prompt_row else Config.DEFAULT_PROMPT
        references = "\n\n".join(
            f"[{h['index']}] 来源：{h['file']}《{h['heading']}》\n{h['snippet']}" for h in hits
        ) or "（未检索到相关制度条文）"
        messages = [{"role": m["role"], "content": m["content"]} for m in history]
        messages.append({"role": "user", "content": f"《制度条文》\n{references}\n\n《员工问题》\n{question}"})

        # （5）流式生成：真实大模型 → 降级兜底
        t0 = time.time()
        parts: list[str] = []
        degraded = False
        model_name = Config.LLM_MODEL
        usage = {"prompt_tokens": 0, "completion_tokens": 0}
        if Config.llm_ready():
            try:
                async for chunk in llm.stream_chat(messages, system_prompt):
                    if chunk["type"] == "delta":
                        parts.append(chunk["text"])
                        yield sse("delta", {"text": chunk["text"]})
                    elif chunk["type"] == "usage":
                        usage = chunk["usage"]
            except Exception as exc:
                yield sse("error", {"code": 4001, "message": f"大模型调用失败，已切换降级模式：{exc}"})
                degraded = True
        if degraded or not parts:
            # 降级：检索原文拼接 + 模拟流式（保证前端打字机效果一致）
            degraded = True
            model_name = "local-fallback"
            answer = llm.build_degraded_answer(question, hits)
            parts = [answer]
            async for chunk in llm.fake_stream(answer):
                if chunk["type"] == "delta":
                    yield sse("delta", {"text": chunk["text"]})

        # （6）落库 + 结束事件
        content = "".join(parts)
        latency_ms = int((time.time() - t0) * 1000)
        message_id = db.insert_message(
            session_id, "assistant", content,
            citations=hits, model=model_name, degraded=1 if degraded else 0,
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
            latency_ms=latency_ms,
        )
        yield sse("done", {
            "message_id": message_id, "model": model_name, "degraded": degraded,
            "usage": usage, "latency_ms": latency_ms,
        })

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
