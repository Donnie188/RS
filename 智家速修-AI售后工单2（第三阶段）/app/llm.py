# -*- coding: utf-8 -*-
"""LLM 调用模块：供应商可配置（阶段 1 核心要求的延续）+ 流式输出 + 降级兜底。

要点：
- 统一走 OpenAI 兼容协议（/chat/completions），换供应商只改 .env；
- stream_chat() 是异步生成器，逐块 yield 文本，供 SSE 接口转发给前端；
- 未配置 Key 或调用失败时，调用方走 build_degraded_answer() 降级：
  直接把检索命中的原文结构化展示，保证用户"永远拿得到东西"。
"""

import asyncio
from typing import Any, AsyncGenerator

from .config import Config

try:
    from openai import AsyncOpenAI
except ImportError:  # 未装 openai 时也能启动服务（只是走降级）
    AsyncOpenAI = None


def llm_mode() -> str:
    """当前大模型工作模式，health 接口和页面头部展示用。"""
    if Config.llm_ready() and AsyncOpenAI is not None:
        return f"{Config.LLM_PROVIDER} / {Config.LLM_MODEL}"
    return "降级模式（未配置 LLM_API_KEY，回答由检索原文拼接）"


async def stream_chat(messages: list[dict], system_prompt: str) -> AsyncGenerator[dict, None]:
    """流式调用大模型。

    messages —— [{"role": "user"/"assistant", "content": "..."}]（不含 system），
                system 单独传是因为它来自数据库的提示词模板，每次请求都可能不同；
    产出     —— 逐块 yield {"type":"delta","text":...}，最后 yield {"type":"usage", ...}。
    异常     —— 网络失败/超时直接抛出，由调用方决定是否降级（上层有 try 包裹）。
    """
    if not Config.llm_ready() or AsyncOpenAI is None:
        raise RuntimeError("未配置 LLM_API_KEY，无法调用远端大模型")

    client = AsyncOpenAI(api_key=Config.LLM_API_KEY, base_url=Config.LLM_BASE_URL, timeout=Config.LLM_TIMEOUT)
    full_messages = [{"role": "system", "content": system_prompt}] + messages
    usage: dict[str, int] = {}

    stream = await client.chat.completions.create(
        model=Config.LLM_MODEL,
        messages=full_messages,
        temperature=Config.LLM_TEMPERATURE,
        max_tokens=Config.LLM_MAX_TOKENS,
        stream=True,
    )
    async for chunk in stream:
        if not chunk.choices:
            # 部分供应商最后一个 chunk 只有 usage 没有 choices
            if getattr(chunk, "usage", None):
                usage = {
                    "prompt_tokens": getattr(chunk.usage, "prompt_tokens", 0) or 0,
                    "completion_tokens": getattr(chunk.usage, "completion_tokens", 0) or 0,
                }
            continue
        delta = chunk.choices[0].delta
        text = getattr(delta, "content", None)
        if text:
            yield {"type": "delta", "text": text}
    yield {"type": "usage", "usage": usage or {"prompt_tokens": 0, "completion_tokens": 0}}


async def fake_stream(text: str, interval: float = 0.03, batch: int = 3) -> AsyncGenerator[dict, None]:
    """把一段完整文本模拟成流式输出（降级模式/无 Key 演示时使用）。

    注意：这不是真实 token 流，只是让前端打字机效果一致。
    真实模式走 stream_chat()。
    """
    for i in range(0, len(text), batch):
        yield {"type": "delta", "text": text[i:i + batch]}
        await asyncio.sleep(interval)
    yield {"type": "usage", "usage": {"prompt_tokens": 0, "completion_tokens": 0}}


def build_degraded_answer(question: str, hits: list[dict]) -> str:
    """降级回答：未配置 Key 或模型调用失败时，把检索原文结构化展示出来。

    护栏：如果一条都没命中，明确说"制度中未找到"，不编造 ——
    这正是提示词模板要求模型遵守的行为，降级路径也必须同样遵守。
    """
    if not hits:
        return "制度中未找到相关规定。建议咨询财务部门或管理员补充制度文档。"

    lines = [
        "【降级模式提示】当前未接入大模型（或调用失败），以下为制度库中检索到的原文片段，"
        "未经模型加工，请人工阅读判断。",
        "",
        f"共命中 {len(hits)} 条相关制度片段：",
    ]
    for h in hits:
        lines.append("")
        lines.append(f"[{h['index']}] 来源：{h['file']}《{h['heading']}》")
        snippet = h["snippet"]
        lines.append(snippet + ("…" if len(snippet) >= 200 else ""))
    lines.append("")
    lines.append("引用来源：" + "、".join(f"[{h['index']}] {h['file']}《{h['heading']}》" for h in hits))
    return "\n".join(lines)
