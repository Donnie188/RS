# -*- coding: utf-8 -*-
"""数据库层：SQLite 的建表、连接管理与全部 CRUD。

设计说明：
- 沿用阶段 2 接口设计中的表结构，并扩展阶段 3 新增的三组表：
  users（登录鉴权）、kb_documents/kb_chunks（知识库）、prompt_templates（提示词模板）；
- messages 表在阶段 2 字段基础上增加 flagged / flag_reason 两列，支撑"审计页标记疑似违规"；
- 向量不存文件索引（阶段 1 的 FAISS），而是以 BLOB 直接存在 kb_chunks.embedding 列里：
  文档量小（几十份以内），全量载入内存用 numpy 做余弦即可，删除文档不需要重建索引文件；
- sqlite3 用单连接 + 锁，FastAPI 的同步路由跑在线程池里，对这个写入量完全够用。
"""

import json
import sqlite3
import threading
from datetime import datetime, timedelta

from .config import Config

_conn: sqlite3.Connection | None = None
_lock = threading.Lock()


# ----------------------------------------------------------------------------
# 基础工具
# ----------------------------------------------------------------------------

def now_str() -> str:
    """统一使用本地时间（东八区）的 ISO8601 字符串，避免时区换算混乱。"""
    return datetime.now().isoformat(timespec="seconds")


def get_conn() -> sqlite3.Connection:
    """获取全局唯一连接。row_factory=Row 让查询结果可以按列名取值。

    关于两个 PRAGMA（都是踩过坑才加的）：
    - journal_mode=WAL：SQLite 默认的 rollback journal 会让"读事务"阻塞"写事务"。
      SSE 问答期间，生成器长时间持有读连接，此时管理员上传文档就会直接报
      "database is locked"。WAL 让读写互不阻塞，是单机多线程服务的最低要求。
    - busy_timeout=15000：万一真撞上写写冲突（例如同时上传两份文档），
      让 SQLite 自己等 15 秒重试，而不是立刻抛异常给用户。
    """
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(
            str(Config.DB_PATH),
            check_same_thread=False,
            timeout=15,          # 等价于 busy_timeout，单位秒
        )
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode = WAL")   # 读写并发（生成 .db-wal / .db-shm 两个伴生文件）
        _conn.execute("PRAGMA synchronous = NORMAL") # WAL 下的推荐值，兼顾安全与速度
        _conn.execute("PRAGMA foreign_keys = ON")    # 开启外键约束（删除文档级联删切片）
    return _conn


def execute(sql: str, params: tuple = ()) -> sqlite3.Cursor:
    """写操作的统一入口：加锁 → 执行 → 提交。"""
    with _lock:
        cur = get_conn().execute(sql, params)
        get_conn().commit()
        return cur


def query(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    """读操作：返回 Row 列表。"""
    with _lock:
        return get_conn().execute(sql, params).fetchall()


def query_one(sql: str, params: tuple = ()) -> sqlite3.Row | None:
    with _lock:
        return get_conn().execute(sql, params).fetchone()


# ----------------------------------------------------------------------------
# 建表
# ----------------------------------------------------------------------------

DDL = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,             -- 登录名
    password_hash TEXT NOT NULL,                    -- pbkdf2 单向哈希，绝不存明文
    role          TEXT NOT NULL CHECK(role IN ('employee','admin')),
    display_name  TEXT NOT NULL,                    -- 界面显示名
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kb_documents (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    filename     TEXT NOT NULL,                     -- 上传时的原始文件名
    source_type  TEXT NOT NULL DEFAULT 'md',       -- md / pdf
    char_count   INTEGER NOT NULL DEFAULT 0,        -- 提取出的正文字数
    chunk_count  INTEGER NOT NULL DEFAULT 0,        -- 切片数量
    uploaded_by  INTEGER NOT NULL REFERENCES users(id),
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kb_chunks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id      INTEGER NOT NULL REFERENCES kb_documents(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,                   -- 文档内的序号，从 0 开始
    heading     TEXT NOT NULL,                      -- 所属章节标题（引用来源用）
    text        TEXT NOT NULL,                      -- 切片正文
    embedding   BLOB,                               -- L2 归一化后的 float32 向量
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON kb_chunks(doc_id);

CREATE TABLE IF NOT EXISTS prompt_templates (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,                       -- 模板名（自动按版本号生成，也可手填）
    content    TEXT NOT NULL,                       -- 系统提示词正文
    is_active  INTEGER NOT NULL DEFAULT 0,          -- 1 = 当前生效（全表最多一条）
    updated_by INTEGER NOT NULL REFERENCES users(id),
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id            TEXT PRIMARY KEY,                 -- 'sess_' + 16 位十六进制
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    title         TEXT NOT NULL DEFAULT '新对话',
    message_count INTEGER NOT NULL DEFAULT 0,
    agent_state   TEXT NOT NULL DEFAULT 'idle',   -- ★阶段5：会话状态（驱动动态工具暴露）
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id, updated_at DESC);

CREATE TABLE IF NOT EXISTS messages (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id       TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    role             TEXT NOT NULL CHECK(role IN ('user','assistant')),
    content          TEXT NOT NULL,
    citations        TEXT,                          -- JSON 数组：引用的制度条文来源
    model            TEXT,                          -- 实际使用的模型名；降级时为 local-fallback
    degraded         INTEGER NOT NULL DEFAULT 0,   -- 1 = 降级输出（未走大模型）
    prompt_tokens    INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    latency_ms      INTEGER NOT NULL DEFAULT 0,     -- 端到端耗时（含检索）
    flagged          INTEGER NOT NULL DEFAULT 0,    -- ★阶段3新增：1 = 疑似违规
    flag_reason      TEXT,                          -- ★阶段3新增：标记原因（管理员填写）
    created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);
CREATE INDEX IF NOT EXISTS idx_messages_flag ON messages(flagged);

-- ============================================================================
-- ★阶段5新增：Agent 执行循环的三张支撑表
-- ============================================================================

-- 校验凭证表：validate_invoice 通过后签发的 check_id。
-- 这是"create_draft 必须在 validate 通过后调用"这条顺序约束的执法依据：
-- create_draft 执行前，服务端只认这张表里的有效凭证，不信任模型的任何口头声明。
CREATE TABLE IF NOT EXISTS validation_records (
    check_id    TEXT PRIMARY KEY,                  -- 'chk_' + 随机串，模型必须原样引用
    session_id  TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    result      TEXT NOT NULL CHECK(result IN ('pass','fail')),
    payload     TEXT NOT NULL,                     -- 校验入参（金额/类别/日期等）JSON
    violations  TEXT,                              -- fail 时的违规明细 JSON 数组
    standard_ref TEXT,                             -- pass 时命中的制度依据（如"一线城市住宿 600 元/晚"）
    expires_at  TEXT NOT NULL,                     -- 凭证有效期（30 分钟），过期需重新校验
    consumed    INTEGER NOT NULL DEFAULT 0,        -- 1 = 已被 create_draft 使用（一次性，防重放）
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_validation_session ON validation_records(session_id, created_at);

-- 草稿表：报销草稿的状态机载体。
-- 状态流转（全部由服务端代码驱动，模型只能发起请求）：
--   pending_confirm（等待员工确认，AgentLoop 中断点）
--   → validated（确认通过，凭证转移绑定）
--   → submitted（已提交为正式单）
--   → cancelled（员工取消）
CREATE TABLE IF NOT EXISTS drafts (
    draft_id    TEXT PRIMARY KEY,                  -- 'dft_' + 随机串
    session_id  TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    status      TEXT NOT NULL DEFAULT 'pending_confirm'
                CHECK(status IN ('pending_confirm','validated','submitted','cancelled')),
    items       TEXT NOT NULL,                     -- 草稿内容 JSON（费用明细 + 校验结论 + 制度依据）
    check_id    TEXT REFERENCES validation_records(check_id),  -- 来源凭证（建草稿后即消费作废）
    ticket_id   TEXT,                              -- 提交成功后回填的正式单号
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_drafts_session ON drafts(session_id, created_at);

-- 工具调用日志表：AgentLoop 每一步决策与执行都记一条，供阶段 5 分析（轨迹回放/成功率/耗时分布）。
-- blocked=1 表示这次调用被服务端状态机拦截（如无凭证建草稿）——这是"跳步尝试"的直接证据。
CREATE TABLE IF NOT EXISTS tool_calls (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id     TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    step           INTEGER NOT NULL,               -- 第几轮决策（1~5）
    tool           TEXT NOT NULL,                  -- 工具名
    args           TEXT,                           -- 模型给的参数 JSON
    result_digest  TEXT,                           -- 结果摘要（截断存储，完整结果不落库）
    blocked        INTEGER NOT NULL DEFAULT 0,     -- 1 = 被状态机拦截
    latency_ms     INTEGER NOT NULL DEFAULT 0,     -- 工具执行耗时
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_toolcalls_session ON tool_calls(session_id, id);
"""


def init_db() -> None:
    """应用启动时调用：建表（幂等）。

    阶段 5 兼容性迁移：sessions 表补 agent_state 列（老库升级时 ALTER 会抛
    duplicate column，捕获即可）。新库直接由 DDL 建出完整结构。
    """
    with _lock:
        get_conn().executescript(DDL)
        try:
            get_conn().execute(
                "ALTER TABLE sessions ADD COLUMN agent_state TEXT NOT NULL DEFAULT 'idle'")
        except sqlite3.OperationalError:
            pass  # 列已存在（新库或已升级），忽略
        get_conn().commit()


# ----------------------------------------------------------------------------
# users
# ----------------------------------------------------------------------------

def get_user_by_username(username: str) -> sqlite3.Row | None:
    return query_one("SELECT * FROM users WHERE username = ?", (username,))


def get_user(user_id: int) -> sqlite3.Row | None:
    return query_one("SELECT id, username, role, display_name, created_at FROM users WHERE id = ?", (user_id,))


def create_user(username: str, password_hash: str, role: str, display_name: str) -> int:
    cur = execute(
        "INSERT INTO users(username, password_hash, role, display_name, created_at) VALUES (?,?,?,?,?)",
        (username, password_hash, role, display_name, now_str()),
    )
    return int(cur.lastrowid)


def list_users() -> list[sqlite3.Row]:
    return query("SELECT id, username, role, display_name FROM users ORDER BY id")


# ----------------------------------------------------------------------------
# kb_documents / kb_chunks
# ----------------------------------------------------------------------------

def create_document(filename: str, source_type: str, char_count: int, uploaded_by: int) -> int:
    cur = execute(
        "INSERT INTO kb_documents(filename, source_type, char_count, uploaded_by, created_at) VALUES (?,?,?,?,?)",
        (filename, source_type, char_count, uploaded_by, now_str()),
    )
    return int(cur.lastrowid)


def set_chunk_count(doc_id: int, count: int) -> None:
    execute("UPDATE kb_documents SET chunk_count = ? WHERE id = ?", (count, doc_id))


def list_documents() -> list[sqlite3.Row]:
    return query(
        "SELECT d.*, u.display_name AS uploader FROM kb_documents d "
        "LEFT JOIN users u ON u.id = d.uploaded_by ORDER BY d.id DESC"
    )


def get_document(doc_id: int) -> sqlite3.Row | None:
    return query_one("SELECT * FROM kb_documents WHERE id = ?", (doc_id,))


def delete_document(doc_id: int) -> None:
    # 外键级联会同时删除该文档的所有切片
    execute("DELETE FROM kb_documents WHERE id = ?", (doc_id,))


def insert_chunks(rows: list[tuple[int, int, str, str, bytes | None]]) -> None:
    """批量写入切片（含向量 BLOB）。rows = [(doc_id, chunk_index, heading, text, embedding)]"""
    with _lock:
        conn = get_conn()
        conn.executemany(
            "INSERT INTO kb_chunks(doc_id, chunk_index, heading, text, embedding, created_at) "
            "VALUES (?,?,?,?,?,?)",
            [(r[0], r[1], r[2], r[3], r[4], now_str()) for r in rows],
        )
        conn.commit()


def load_all_chunks() -> list[sqlite3.Row]:
    """加载全部切片（含向量），供内存索引使用。"""
    return query(
        "SELECT c.id, c.doc_id, c.chunk_index, c.heading, c.text, c.embedding, d.filename "
        "FROM kb_chunks c JOIN kb_documents d ON d.id = c.doc_id ORDER BY c.id"
    )


def update_chunk_embedding(chunk_id: int, blob: bytes) -> None:
    execute("UPDATE kb_chunks SET embedding = ? WHERE id = ?", (blob, chunk_id))


def count_docs_and_chunks() -> tuple[int, int]:
    row = query_one(
        "SELECT (SELECT COUNT(*) FROM kb_documents) AS docs, (SELECT COUNT(*) FROM kb_chunks) AS chunks"
    )
    return int(row["docs"]), int(row["chunks"])


# ----------------------------------------------------------------------------
# prompt_templates
# ----------------------------------------------------------------------------

def get_active_prompt() -> sqlite3.Row | None:
    """当前生效的系统提示词。问答接口每次请求都会读它 —— 改完立即生效的关键就在这里。"""
    return query_one("SELECT * FROM prompt_templates WHERE is_active = 1 ORDER BY id DESC")


def list_prompts() -> list[sqlite3.Row]:
    return query(
        "SELECT p.*, u.display_name AS editor FROM prompt_templates p "
        "LEFT JOIN users u ON u.id = p.updated_by ORDER BY p.id DESC"
    )


def save_prompt(name: str, content: str, updated_by: int) -> int:
    """保存新版本并置为生效：先把旧版本全部下线，再插入。保留历史版本可随时回滚。"""
    with _lock:
        conn = get_conn()
        conn.execute("UPDATE prompt_templates SET is_active = 0 WHERE is_active = 1")
        cur = conn.execute(
            "INSERT INTO prompt_templates(name, content, is_active, updated_by, updated_at) "
            "VALUES (?,?,1,?,?)",
            (name, content, updated_by, now_str()),
        )
        conn.commit()
        return int(cur.lastrowid)


def activate_prompt(prompt_id: int, updated_by: int) -> None:
    """把某个历史版本重新置为生效（回滚操作）。"""
    with _lock:
        conn = get_conn()
        conn.execute("UPDATE prompt_templates SET is_active = 0 WHERE is_active = 1")
        conn.execute(
            "UPDATE prompt_templates SET is_active = 1, updated_by = ?, updated_at = ? WHERE id = ?",
            (updated_by, now_str(), prompt_id),
        )
        conn.commit()


# ----------------------------------------------------------------------------
# sessions / messages
# ----------------------------------------------------------------------------

def create_session(user_id: int, title: str = "新对话") -> str:
    import secrets
    sid = "sess_" + secrets.token_hex(8)
    execute(
        "INSERT INTO sessions(id, user_id, title, created_at, updated_at) VALUES (?,?,?,?,?)",
        (sid, user_id, title[:50], now_str(), now_str()),
    )
    return sid


def get_session(sid: str) -> sqlite3.Row | None:
    return query_one("SELECT * FROM sessions WHERE id = ?", (sid,))


def list_sessions_by_user(user_id: int) -> list[sqlite3.Row]:
    return query("SELECT * FROM sessions WHERE user_id = ? ORDER BY updated_at DESC", (user_id,))


def touch_session(sid: str) -> None:
    execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (now_str(), sid))


def increase_message_count(sid: str) -> None:
    execute("UPDATE sessions SET message_count = message_count + 1 WHERE id = ?", (sid,))


def insert_message(
    session_id: str,
    role: str,
    content: str,
    citations: list | None = None,
    model: str = "",
    degraded: int = 0,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    latency_ms: int = 0,
) -> int:
    cur = execute(
        "INSERT INTO messages(session_id, role, content, citations, model, degraded, "
        "prompt_tokens, completion_tokens, latency_ms, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (
            session_id, role, content,
            json.dumps(citations, ensure_ascii=False) if citations else None,
            model, degraded, prompt_tokens, completion_tokens, latency_ms, now_str(),
        ),
    )
    increase_message_count(session_id)
    touch_session(session_id)
    return int(cur.lastrowid)


def get_history(session_id: str, rounds: int, char_limit: int) -> list[dict]:
    """取最近 N 轮（2N 条）历史，按时间正序返回，只带 content 不带引用。

    截断规则（阶段 2 设计中定下的坑规避）：
    - 引用片段不进上下文，纯浪费 token；
    - 单条超过 char_limit 时截断，防止极端长答案把窗口撑爆。
    """
    rows = query(
        "SELECT role, content FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ?",
        (session_id, rounds * 2),
    )
    history = [
        {"role": r["role"], "content": r["content"][:char_limit]}
        for r in reversed(rows)
    ]
    return history


def get_messages(session_id: str) -> list[dict]:
    """某个会话的全部消息（前端刷新页面时恢复对话用）。"""
    rows = query("SELECT * FROM messages WHERE session_id = ? ORDER BY id", (session_id,))
    result = []
    for r in rows:
        result.append({
            "role": r["role"],
            "content": r["content"],
            "citations": json.loads(r["citations"]) if r["citations"] else [],
            "flagged": bool(r["flagged"]),
            "degraded": bool(r["degraded"]),
            "created_at": r["created_at"],
        })
    return result


def get_message(message_id: int) -> sqlite3.Row | None:
    return query_one("SELECT * FROM messages WHERE id = ?", (message_id,))


# ----------------------------------------------------------------------------
# 对话审计（阶段 3 核心新增）
# ----------------------------------------------------------------------------

def list_audit(user_id: int | None, start: str | None, end: str | None,
               flagged_only: bool) -> list[dict]:
    """按员工 / 起止日期 / 是否已标记 筛选对话记录，返回给审计页渲染。

    - 日期按"创建日期前缀"匹配（ISO8601 字符串可直接用字符串比较）；
    - 关联出员工姓名、会话标题，方便管理员看上下文。
    """
    sql = (
        "SELECT m.*, u.display_name AS employee, s.title AS session_title "
        "FROM messages m JOIN sessions s ON s.id = m.session_id "
        "JOIN users u ON u.id = s.user_id WHERE 1=1"
    )
    params: list = []
    if user_id:
        sql += " AND s.user_id = ?"
        params.append(user_id)
    if start:
        sql += " AND m.created_at >= ?"
        params.append(start)
    if end:
        # 结束日期取当天 23:59:59，保证闭区间
        sql += " AND m.created_at <= ?"
        params.append(end + "T23:59:59")
    if flagged_only:
        sql += " AND m.flagged = 1"
    sql += " ORDER BY m.id DESC LIMIT 500"
    rows = query(sql, tuple(params))

    result = []
    for r in rows:
        result.append({
            "id": r["id"],
            "session_id": r["session_id"],
            "session_title": r["session_title"],
            "employee": r["employee"],
            "role": r["role"],
            "content": r["content"],
            "citations": json.loads(r["citations"]) if r["citations"] else [],
            "flagged": bool(r["flagged"]),
            "flag_reason": r["flag_reason"],
            "degraded": bool(r["degraded"]),
            "created_at": r["created_at"],
        })
    return result


def set_flag(message_id: int, reason: str, by_user: int) -> None:
    """标记疑似违规。留痕：标记原因 + 操作人记入 flag_reason 文本。"""
    reason_text = f"{reason}（标记人ID:{by_user}）"
    execute("UPDATE messages SET flagged = 1, flag_reason = ? WHERE id = ?", (reason_text, message_id))


def clear_flag(message_id: int) -> None:
    execute("UPDATE messages SET flagged = 0, flag_reason = NULL WHERE id = ?", (message_id,))


# ----------------------------------------------------------------------------
# 种子数据（首次启动自动执行，保证开箱即可演示）
# ----------------------------------------------------------------------------

def seed() -> None:
    """首次启动时写入：默认账号 + 默认提示词 + 示例对话（审计页演示用）。幂等。"""
    from . import auth  # 延迟导入避免循环依赖

    if get_user_by_username("admin"):
        return  # 已初始化过，跳过

    # ---- 账号：1 管理员 + 2 员工（密码见 README，生产环境务必修改）----
    admin_id = create_user("admin", auth.hash_password("admin123"), "admin", "系统管理员")
    emp1 = create_user("emp001", auth.hash_password("emp123456"), "employee", "张三")
    emp2 = create_user("emp002", auth.hash_password("emp123456"), "employee", "李四")

    # ---- 默认系统提示词（作业要求的模板，写入数据库后不再写死在代码里）----
    save_prompt("默认模板 v1", Config.DEFAULT_PROMPT, admin_id)

    # ---- 示例对话：让审计页一打开就有数据可筛 ----
    sid1 = create_session(emp1, "上海出差住宿标准")
    insert_message(sid1, "user", "我去上海出差，住宿报销标准是多少？")
    insert_message(
        sid1, "assistant",
        "根据制度，一线城市普通员工住宿标准为 500 元/晚，主管级 600 元/晚。\n\n"
        "引用来源：[1] 报销制度.md《二、差旅住宿与交通标准》",
        citations=[{"index": 1, "file": "报销制度.md", "heading": "二、差旅住宿与交通标准",
                    "snippet": "一线城市（北京、上海、广州、深圳）：普通员工 500 元/晚；主管级 600 元/晚。",
                    "score": 0.031, "vector_rank": 1, "bm25_rank": 1}],
        model="seed-sample", latency_ms=1200,
    )

    sid2 = create_session(emp2, "发票丢失怎么报销")
    insert_message(sid2, "user", "我的出租车发票丢了，还能报销吗？")
    msg_id = insert_message(
        sid2, "assistant",
        "制度中未找到发票丢失情况的处理办法，建议补充情况说明并联系财务确认。\n\n"
        "引用来源：[1] 报销制度.md《三、发票与凭证要求》",
        citations=[{"index": 1, "file": "报销制度.md", "heading": "三、发票与凭证要求",
                    "snippet": "所有报销必须提供合规发票原件……", "score": 0.028}],
        model="seed-sample", latency_ms=980,
    )
    # 预置一条"已标记疑似违规"，演示审计页的筛选与取消标记
    set_flag(msg_id, "示例：该问题涉及票据缺失，疑似违规报销，需人工核查", admin_id)


# ----------------------------------------------------------------------------
# ★阶段5新增：Agent 支撑表（validation_records / drafts / tool_calls / agent_state）
# ----------------------------------------------------------------------------

# ---- 会话状态（驱动"动态工具暴露"：状态决定本轮模型能看见哪些工具） ----

def get_agent_state(session_id: str) -> str:
    """读取会话当前状态。取不到时按 idle 处理（兼容老记录）。"""
    row = query_one("SELECT agent_state FROM sessions WHERE id = ?", (session_id,))
    return (row["agent_state"] if row and row["agent_state"] else "idle")


def set_agent_state(session_id: str, state: str) -> None:
    """写入会话状态。状态由服务端代码独占管理，模型无权直接修改。"""
    execute("UPDATE sessions SET agent_state = ?, updated_at = ? WHERE id = ?",
            (state, now_str(), session_id))


# ---- 校验凭证（check_id 的签发 / 查询 / 消费） ----

def create_validation(check_id: str, session_id: str, result: str, payload: dict,
                      violations: list | None, standard_ref: str | None,
                      expires_at: str) -> None:
    """validate_invoice 执行后落一条凭证记录（无论 pass/fail 都留痕，便于审计回放）。"""
    execute(
        "INSERT INTO validation_records"
        "(check_id, session_id, result, payload, violations, standard_ref, expires_at, consumed, created_at)"
        " VALUES (?,?,?,?,?,?,?,0,?)",
        (check_id, session_id, result, json.dumps(payload, ensure_ascii=False),
         json.dumps(violations or [], ensure_ascii=False), standard_ref, expires_at, now_str()),
    )


def get_valid_check(session_id: str, check_id: str) -> sqlite3.Row | None:
    """取一条"当前可用"的校验凭证。必须同时满足四个条件（缺一即拒）：

    1. 属于本会话（防止拿别的会话的凭证跨会话建单）；
    2. result = pass（fail 的凭证毫无价值）；
    3. 未消费（consumed=0，check_id 一次性，防重放）;
    4. 未过期（expires_at > 当前时间，防"拿上午的 pass 建下午的单"）。
    任何一条不满足都返回 None，由调用方给出具体原因。
    """
    row = query_one("SELECT * FROM validation_records WHERE check_id = ?", (check_id,))
    if row is None or row["session_id"] != session_id:
        return None
    if row["result"] != "pass" or row["consumed"]:
        return None
    if row["expires_at"] <= now_str():
        return None
    return row


def consume_check(check_id: str) -> None:
    """作废凭证：create_draft 成功后调用，保证同一份校验只能建一张草稿。"""
    execute("UPDATE validation_records SET consumed = 1 WHERE check_id = ?", (check_id,))


# ---- 草稿（状态机的载体，全部流转在服务端函数里完成） ----

def create_draft(draft_id: str, session_id: str, items: dict, check_id: str) -> None:
    """创建草稿，初始状态就是 pending_confirm——阶段5 的 create_draft 强制中断点。"""
    execute(
        "INSERT INTO drafts(draft_id, session_id, status, items, check_id, created_at, updated_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (draft_id, session_id, "pending_confirm", json.dumps(items, ensure_ascii=False),
         check_id, now_str(), now_str()),
    )


def get_draft(draft_id: str) -> sqlite3.Row | None:
    return query_one("SELECT * FROM drafts WHERE draft_id = ?", (draft_id,))


def set_draft_status(draft_id: str, status: str, ticket_id: str | None = None) -> None:
    """草稿状态流转。校验合法流转由调用方（状态机）负责，这里只管写。"""
    execute("UPDATE drafts SET status = ?, ticket_id = COALESCE(?, ticket_id), updated_at = ?"
            " WHERE draft_id = ?", (status, ticket_id, now_str(), draft_id))


def get_pending_draft(session_id: str) -> sqlite3.Row | None:
    """取本会话最近一张待确认草稿。确认/取消接口用它定位操作对象。"""
    return query_one(
        "SELECT * FROM drafts WHERE session_id = ? AND status = 'pending_confirm'"
        " ORDER BY created_at DESC LIMIT 1", (session_id,))


# ---- 工具调用日志（阶段5 每轮决策记录，供后续轨迹分析与成功率统计） ----

def log_tool_call(session_id: str, step: int, tool: str, args: dict | None,
                  result_digest: str, blocked: bool, latency_ms: int) -> None:
    """AgentLoop 每次工具执行（或被拦截）都记一条。

    result_digest 只存摘要（前 200 字符），完整结果留在对话上下文里，
    避免日志表被大 JSON 撑爆——日志的用途是"分析调用模式"，不是数据备份。
    """
    execute(
        "INSERT INTO tool_calls(session_id, step, tool, args, result_digest, blocked, latency_ms, created_at)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (session_id, step, tool,
         json.dumps(args, ensure_ascii=False) if args else None,
         (result_digest or "")[:200], 1 if blocked else 0, latency_ms, now_str()),
    )


def list_tool_calls(session_id: str) -> list[dict]:
    """按时间序取某会话的全部工具调用记录（轨迹回放用）。"""
    rows = query("SELECT * FROM tool_calls WHERE session_id = ? ORDER BY id", (session_id,))
    out = []
    for r in rows:
        out.append({
            "step": r["step"], "tool": r["tool"], "args": r["args"],
            "result_digest": r["result_digest"], "blocked": bool(r["blocked"]),
            "latency_ms": r["latency_ms"], "created_at": r["created_at"],
        })
    return out
