from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncGenerator
from dataclasses import replace
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, ResultContentType, filter
from astrbot.api.message_components import Plain
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register


@register(
    "astrbot_plugin_streamchunk",
    "MUKAPP",
    "Smart chunk sender for platforms without native streaming support.",
    "0.1.0",
)
class StreamChunkPlugin(Star):
    ORIGINAL_STREAMING_SUPPORT_KEY = "_streamchunk_original_support_streaming_message"
    PLATFORM_META_PATCHED_KEY = "_streamchunk_platform_meta_patched"
    TAG_PATTERN = re.compile(r"\[(SHORT|LONG)\]", re.IGNORECASE)
    PROMPT_SENTINEL = "[STREAMCHUNK_LENGTH_TAG_RULE]"
    PROMPT_TEMPLATE = (
        "[STREAMCHUNK_LENGTH_TAG_RULE]\n"
        "你必须在回复开始前输出一个系统标签：[SHORT] 或 [LONG]。\n"
        "【[SHORT] 使用条件】你打算回复的字数在120字以下时使用，会被系统分为多条消息发送，更像人们的聊天风格。例如日常闲聊时使用 [SHORT]。\n"
        "【[LONG] 使用条件】你打算回复的字数在120字以上时使用，只会发送一条消息，避免消息太多刷屏。例如你的回复是一篇短文、文章、代码、包含多个列表项或者是长段落时使用 [LONG]。\n"
        "标签必须放在最前面，不要附带任何解释。"
    )
    TAG_DETECT_MAX_CHARS = 64

    def __init__(self, context: Context, config: dict[str, Any] | None = None):
        super().__init__(context)
        self._config = config or {}
        self._load_settings()

    def _load_settings(self) -> None:
        self.enabled = self._as_bool(self._config.get("enabled", True), True)
        self.only_unsupported_platform = self._as_bool(
            self._config.get("only_unsupported_platform", True),
            True,
        )
        self.local_stream_processing = self._as_bool(
            self._config.get("local_stream_processing", True),
            True,
        )
        self.only_model_result = self._as_bool(
            self._config.get("only_model_result", True),
            True,
        )
        self.inject_prompt = self._as_bool(self._config.get("inject_prompt", True), True)

        self.default_mode_no_tag = str(
            self._config.get("default_mode_no_tag", "long"),
        ).lower()
        if self.default_mode_no_tag not in {"short", "long", "auto"}:
            self.default_mode_no_tag = "long"

        self.auto_short_max_chars = self._as_int(
            self._config.get("auto_short_max_chars", 120),
            120,
            min_value=1,
        )
        self.min_chunk_chars = self._as_int(
            self._config.get("min_chunk_chars", 6),
            6,
            min_value=1,
        )
        self.max_chunk_chars = self._as_int(
            self._config.get("max_chunk_chars", 160),
            160,
            min_value=self.min_chunk_chars,
        )
        self.segment_interval_seconds = self._as_float(
            self._config.get("segment_interval_seconds", 0.8),
            0.8,
            min_value=0.0,
        )

        split_punctuations = str(
            self._config.get("split_punctuations", r"[。？！!?；;…\n]"),
        )
        try:
            self.split_pattern = re.compile(split_punctuations)
        except re.error as e:
            logger.error(f"streamchunk: 分段标点正则表达式编译失败: {e}，将回退到默认设置")
            self.split_pattern = re.compile(r"[。？！!?；;…\n]")

        drop_punctuations = str(
            self._config.get("drop_punctuations", r"[。.]"),
        )
        try:
            self.drop_pattern = re.compile(drop_punctuations)
        except re.error as e:
            logger.error(f"streamchunk: 丢弃分段符号正则表达式编译失败: {e}，将回退到默认设置")
            self.drop_pattern = re.compile(r"[。.]")

    @staticmethod
    def _as_bool(value: Any, default: bool) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"1", "true", "yes", "on"}:
                return True
            if lowered in {"0", "false", "no", "off"}:
                return False
        return default

    @staticmethod
    def _as_int(value: Any, default: int, min_value: int | None = None) -> int:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            parsed = default
        if min_value is not None:
            parsed = max(min_value, parsed)
        return parsed

    @staticmethod
    def _as_float(value: Any, default: float, min_value: float | None = None) -> float:
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            parsed = default
        if min_value is not None:
            parsed = max(min_value, parsed)
        return parsed

    @staticmethod
    def _is_platform_streaming_supported(event: AstrMessageEvent) -> bool:
        original_support = event.get_extra(
            StreamChunkPlugin.ORIGINAL_STREAMING_SUPPORT_KEY,
            None,
        )
        if original_support is not None:
            return bool(original_support)
        return bool(getattr(event.platform_meta, "support_streaming_message", True))

    def _force_event_streaming_pipeline(self, event: AstrMessageEvent) -> None:
        if event.get_extra(self.PLATFORM_META_PATCHED_KEY, False):
            return

        original_support = bool(
            getattr(event.platform_meta, "support_streaming_message", True),
        )
        event.set_extra(self.ORIGINAL_STREAMING_SUPPORT_KEY, original_support)

        try:
            patched_meta = replace(event.platform_meta, support_streaming_message=True)
        except Exception as e:
            logger.warning(f"streamchunk: 平台元数据改写失败，将仅依赖 enable_streaming: {e}")
            return

        event.platform_meta = patched_meta
        # keep compatibility with event.platform alias
        event.platform = patched_meta
        event.set_extra(self.PLATFORM_META_PATCHED_KEY, True)

    def _should_handle_event(self, event: AstrMessageEvent) -> bool:
        if not self.enabled:
            return False
        if self.only_unsupported_platform and self._is_platform_streaming_supported(event):
            return False
        return True

    @filter.on_waiting_llm_request(priority=100)
    async def on_waiting_llm_request(self, event: AstrMessageEvent):
        """Patch local stream handling before LLM execution pipeline starts."""
        if not self._should_handle_event(event):
            return

        is_stream_unsupported = not self._is_platform_streaming_supported(event)

        if (
            self.local_stream_processing
            and is_stream_unsupported
            and not event.get_extra("_streamchunk_stream_patched", False)
        ):
            # Keep model-side streaming enabled and force pipeline streaming branch.
            event.set_extra("enable_streaming", True)
            self._force_event_streaming_pipeline(event)

            async def patched_send_streaming(
                generator: AsyncGenerator[MessageChain, None],
                use_fallback: bool = False,
            ) -> None:
                _ = use_fallback
                logger.info("streamchunk: 已接管 send_streaming，开始本地消费流式输出。")
                await self._process_stream_locally(event, generator)

            event.send_streaming = patched_send_streaming  # type: ignore[method-assign]
            event.set_extra("_streamchunk_stream_patched", True)

    @filter.on_llm_request(priority=100)
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """Inject [SHORT]/[LONG] rule ahead of LLM request."""
        if not self._should_handle_event(event):
            return

        if not self.inject_prompt:
            return

        system_prompt = req.system_prompt or ""
        if self.PROMPT_SENTINEL in system_prompt:
            return

        if system_prompt and not system_prompt.endswith("\n"):
            system_prompt += "\n"
        system_prompt += f"{self.PROMPT_TEMPLATE}\n"
        req.system_prompt = system_prompt

    def _extract_mode(self, text: str) -> tuple[str | None, str]:
        match = self.TAG_PATTERN.search(text)
        if not match:
            return None, text
        mode = match.group(1).lower()
        cleaned = text[:match.start()] + text[match.end():]
        if match.start() == 0:
            cleaned = cleaned.lstrip()
        return mode, cleaned

    def _detect_mode_from_prefix(self, text: str) -> tuple[str | None, str, bool]:
        """Detect mode from stream prefix.

        Returns:
            mode: "short"/"long"/"auto"/None
            remaining: text after removing tag if present
            need_more: True when tag parsing is still uncertain
        """
        mode, cleaned = self._extract_mode(text)
        if mode is not None:
            return mode, cleaned, False

        stripped = text.lstrip()
        if stripped.startswith("<think>") and "</think>" not in text:
            return None, "", True

        if "</think>" in text:
            after_think = text.split("</think>", 1)[1]
            mode, cleaned_after = self._extract_mode(after_think)
            if mode is not None:
                cleaned_total = text[:text.rindex("</think>") + 8] + cleaned_after
                return mode, cleaned_total, False
            if len(after_think) < self.TAG_DETECT_MAX_CHARS:
                return None, "", True
            return self.default_mode_no_tag, text, False

        if len(text) < self.TAG_DETECT_MAX_CHARS:
            return None, "", True

        return self.default_mode_no_tag, text, False

    def _default_mode(self, text: str) -> str:
        if self.default_mode_no_tag in {"short", "long"}:
            return self.default_mode_no_tag
        return "short" if len(text) <= self.auto_short_max_chars else "long"

    def _split_short_text(self, text: str) -> list[str]:
        if not text.strip():
            return []

        chunks: list[str] = []
        buf: list[str] = []

        for char in text:
            buf.append(char)
            size = len(buf)
            should_cut = False

            if size >= self.min_chunk_chars and self.split_pattern.search(char):
                should_cut = True
            elif size >= self.max_chunk_chars:
                should_cut = True

            if should_cut:
                segment = "".join(buf).strip()
                if segment:
                    if self.drop_pattern.search(segment[-1]):
                        segment = segment[:-1].strip()
                    if segment:
                        chunks.append(segment)
                buf.clear()

        if buf:
            segment = "".join(buf).strip()
            if segment:
                if self.drop_pattern.search(segment[-1]):
                    segment = segment[:-1].strip()
                if segment:
                    chunks.append(segment)

        return chunks or [text.strip()]

    def _split_short_text_incremental(
        self,
        text: str,
        final: bool = False,
    ) -> tuple[list[str], str]:
        chunks: list[str] = []
        last_cut = 0

        for idx, char in enumerate(text, start=1):
            seg_len = idx - last_cut
            should_cut = False
            if seg_len >= self.min_chunk_chars and self.split_pattern.search(char):
                should_cut = True
            elif seg_len >= self.max_chunk_chars:
                should_cut = True

            if should_cut:
                part = text[last_cut:idx].strip()
                if part:
                    if self.drop_pattern.search(part[-1]):
                        part = part[:-1].strip()
                    if part:
                        chunks.append(part)
                last_cut = idx

        remain = text[last_cut:]
        if final and remain.strip():
            chunks.append(remain.strip())
            remain = ""

        return chunks, remain

    async def _send_chunks(self, event: AstrMessageEvent, chunks: list[str]) -> None:
        for idx, chunk in enumerate(chunks):
            await event.send(MessageChain([Plain(chunk)]))
            if idx < len(chunks) - 1 and self.segment_interval_seconds > 0:
                await asyncio.sleep(self.segment_interval_seconds)

    async def _send_long_text_if_present(
        self,
        event: AstrMessageEvent,
        long_buffer: str,
    ) -> str:
        text = long_buffer.strip()
        if text:
            await event.send(MessageChain([Plain(text)]))
            return ""
        return long_buffer

    async def _flush_text_buffer(
        self,
        event: AstrMessageEvent,
        mode: str,
        text_buffer: str,
        final: bool,
    ) -> str:
        if mode == "short":
            chunks, remain = self._split_short_text_incremental(text_buffer, final=final)
            await self._send_chunks(event, chunks)
            return remain
        if final:
            return await self._send_long_text_if_present(event, text_buffer)
        return text_buffer

    async def _process_stream_locally(
        self,
        event: AstrMessageEvent,
        generator: AsyncGenerator[MessageChain, None],
    ) -> None:
        mode: str | None = None
        prefix_buffer = ""
        text_buffer = ""

        async for chain in generator:
            if not isinstance(chain, MessageChain):
                continue

            if chain.type == "break":
                continue

            for comp in chain.chain:
                if not isinstance(comp, Plain):
                    if mode is None and prefix_buffer.strip():
                        mode = self.default_mode_no_tag
                        if mode == "auto":
                            mode = "short" if len(prefix_buffer) <= self.auto_short_max_chars else "long"
                        text_buffer += prefix_buffer
                        prefix_buffer = ""

                    if mode == "auto":
                        mode = "short" if len(text_buffer) <= self.auto_short_max_chars else "long"

                    if mode is not None:
                        text_buffer = await self._flush_text_buffer(
                            event,
                            mode,
                            text_buffer,
                            final=True,
                        )

                    await event.send(MessageChain([comp]))
                    continue

                incoming = comp.text
                if not incoming:
                    continue

                logger.debug(f"streamchunk: 当前流式获取到片段 -> '{incoming}'")

                if mode is None:
                    prefix_buffer += incoming
                    mode_candidate, remaining, need_more = self._detect_mode_from_prefix(
                        prefix_buffer,
                    )
                    if need_more:
                        continue
                    mode = mode_candidate
                    if mode is not None:
                        logger.info(f"streamchunk: 从模型输出流提取到响应模式: [{mode.upper()}]")
                    prefix_buffer = ""
                    incoming = remaining
                    if not incoming:
                        continue

                text_buffer += incoming

                if mode == "auto":
                    if len(text_buffer) > self.auto_short_max_chars:
                        mode = "long"
                    # 这里保持缓冲，不进行 flush

                if mode == "short":
                    text_buffer = await self._flush_text_buffer(
                        event,
                        mode,
                        text_buffer,
                        final=False,
                    )

        if mode is None:
            _, remaining, _ = self._detect_mode_from_prefix(prefix_buffer)
            mode = self.default_mode_no_tag
            if mode == "auto":
                mode = "short" if len(prefix_buffer) <= self.auto_short_max_chars else "long"
            text_buffer += remaining
        elif prefix_buffer:
            text_buffer += prefix_buffer

        if mode == "auto":
            mode = "short" if len(text_buffer) <= self.auto_short_max_chars else "long"

        if mode not in {"short", "long"}:
            mode = "long"

        logger.info(f"streamchunk: 流式返回处理完毕，最终采取发送模式: [{mode.upper()}]")
        await self._flush_text_buffer(event, mode, text_buffer, final=True)

    @filter.on_decorating_result(priority=-100)
    async def on_decorating_result(self, event: AstrMessageEvent):
        """Apply tag-based strategy for non-streaming outputs."""
        if not self._should_handle_event(event):
            return

        result = event.get_result()
        if result is None or not result.chain:
            return

        if result.result_content_type in {
            ResultContentType.STREAMING_RESULT,
            ResultContentType.STREAMING_FINISH,
        }:
            return

        if self.only_model_result and not result.is_model_result():
            return

        if not all(isinstance(comp, Plain) for comp in result.chain):
            return

        text = "".join(comp.text for comp in result.chain).strip()
        if not text:
            return

        mode, text = self._extract_mode(text)
        if mode is None:
            mode = self._default_mode(text)
            logger.info(f"streamchunk: 未提取到标签，使用回退模式: [{mode.upper()}]")
        else:
            logger.info(f"streamchunk: 从完整文本提取到响应模式: [{mode.upper()}]")

        if not text.strip():
            logger.warning("streamchunk: 检测到由于标签截断导致的空内容，已跳过发送。")
            return

        if mode == "short":
            chunks = self._split_short_text(text)
            await self._send_chunks(event, chunks)
            logger.info(f"streamchunk: [{mode.upper()}] 模式，已发送 {len(chunks)} 个分段消息。")
        else:
            await event.send(MessageChain([Plain(text)]))
            logger.info(f"streamchunk: [{mode.upper()}] 模式，已发送一条完整长文本消息。")

        # Clear chain to avoid duplicate send in respond stage.
        result.chain = []
