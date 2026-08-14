# 思考模型（deepseek-v4-flash 等带 reasoning_content）的真流式支持
#
# 背景：langchain-openai 的 _stream 用 openai SDK 解析 SSE。思考模型的流式响应
# 里，思考阶段每 chunk 的 delta.content 是 null、只有 reasoning_content —— langchain
# 收集完 merge 出空 content → 抛 "No generation chunks were returned"。
#
# 方案：覆盖 _stream/_astream，绕开 SDK，自己用 httpx 读原始 SSE。
#   - content（正文）→ 正常 AIMessageChunk，图的消息流照常工作
#   - reasoning_content（思考）→ 放在 additional_kwargs["reasoning_content"]，
#     merge_chat_generation_chunks 会拼接它，前端可单独展示"思考过程"
#   - tool_calls → 复用 langchain 的 tool_call_chunk，工具调用不受影响
import asyncio
import json

import httpx
from langchain_core.messages import AIMessageChunk
from langchain_core.messages.tool import tool_call_chunk
from langchain_core.outputs import ChatGenerationChunk
from langchain_openai import ChatOpenAI


class ReasoningChatModel(ChatOpenAI):
    """处理思考模型真流式的 ChatOpenAI 子类。

    覆盖 _stream/_astream：raw SSE 解析，正文→content，思考→additional_kwargs。
    """

    def _sse_request(self, payload: dict):
        """发原始流式请求，逐 chunk yield 解析后的 dict。"""
        api_key = getattr(self, "openai_api_key", None)
        if hasattr(api_key, "get_secret_value"):
            api_key = api_key.get_secret_value()
        # base_url 可能不带 /v1（.env 的 OPENAI_BASE_URL=https://api.ccfuck.me）——
        # 必须补 /v1，否则请求根路径会拿到网页/重定向而非 SSE
        base = str(self.openai_api_base).rstrip("/")
        if not base.endswith("/v1"):
            base += "/v1"
        url = base + "/chat/completions"
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        # 上游瞬断/503 自动重试（学习日记问题 5 的健壮性；中转站偶尔抖）。
        # 注意：必须是生成器里迭代 + 重试——惰性生成器不能靠 return 前的 try/except 捕获迭代期异常。
        max_tries = max(1, getattr(self, "max_retries", 3) + 1)
        last_err = None
        for _try in range(max_tries):
            try:
                for chunk in self._sse_connect(url, headers, payload):
                    yield chunk
                return  # 正常流式结束
            except httpx.HTTPError as e:
                last_err = e
                if _try < max_tries - 1:
                    import time as _time
                    _time.sleep(1.0 * (_try + 1))  # 1s/2s/3s 退避
        raise last_err  # 全部重试失败 → 让上层捕获报错

    def _sse_connect(self, url, headers, payload):
        """单次 SSE 连接，逐 chunk yield。"""
        with httpx.stream(
            "POST",
            url,
            json=payload,
            headers=headers,
            timeout=getattr(self, "request_timeout", 60),
        ) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if not line or not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    return
                try:
                    yield json.loads(data)
                except json.JSONDecodeError:
                    continue

    def _stream(self, messages, stop=None, run_manager=None, **kwargs):
        """同步流式：raw SSE 解析，yield ChatGenerationChunk。"""
        kwargs["stream"] = True  # _get_request_payload 需要 stream=true 才返回 SSE
        payload = self._get_request_payload(messages, stop=stop, **kwargs)
        reasoning_buf: list[str] = []
        saw_content = False
        saw_reasoning = False
        try:
            for chunk in self._sse_request(payload):
                choices = chunk.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}

                # 思考 token：合并成单包发（一次性完整推理），避免前端逐 token 视觉"重复/堆积"。
                # merge 后 additional_kwargs 仍拿到完整推理，前端只显示一次。
                reasoning = delta.get("reasoning_content")
                if reasoning:
                    reasoning_buf.append(reasoning)
                    saw_reasoning = True

                # 工具调用：复用 langchain 的 tool_call_chunk
                if delta.get("tool_calls"):
                    tcs = [
                        tool_call_chunk(
                            name=tc["function"].get("name"),
                            args=tc["function"].get("arguments"),
                            id=tc.get("id"),
                            index=tc.get("index"),
                        )
                        for tc in delta["tool_calls"]
                    ]
                    msg = AIMessageChunk(content="", tool_call_chunks=tcs)
                    yield ChatGenerationChunk(message=msg)

                # 正文 token：content + 带完整 reasoning（保证 merge 后拿得到完整思考）
                content = delta.get("content")
                if content:
                    saw_content = True
                    msg = AIMessageChunk(
                        content=content,
                        additional_kwargs={"reasoning_content": "".join(reasoning_buf)},
                    )
                    if run_manager:
                        run_manager.on_llm_new_token(content, chunk=msg)
                    yield ChatGenerationChunk(message=msg)
        except httpx.HTTPError:
            # 让上层（run_deep_agent 的 except）捕获并报错
            raise

        # 兜底：若只有思考没有正文（极端），补一个空 content chunk 避免空 merge 报错
        if reasoning_buf and not saw_content:
            msg = AIMessageChunk(
                content="",
                additional_kwargs={"reasoning_content": "".join(reasoning_buf)},
            )
            yield ChatGenerationChunk(message=msg)

    async def _astream(self, *args, **kwargs):
        # SSE 是逐行网络读，同步解析足够；用 to_thread 避免阻塞事件循环
        for c in await asyncio.to_thread(lambda: list(self._stream(*args, **kwargs))):
            yield c