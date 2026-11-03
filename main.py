from __future__ import annotations

import asyncio
import re
import unicodedata
from collections.abc import AsyncGenerator, Callable
from dataclasses import replace
from typing import Any, cast

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, ResultContentType, filter
from astrbot.api.message_components import Node, Plain
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register


@register(
    "astrbot_plugin_streamchunk",
    "MUKAPP",
    "Smart chunk sender for platforms without native streaming support.",
    "1.0.5",
)
class StreamChunkPlugin(Star):
    ORIGINAL_STREAMING_SUPPORT_KEY = "_streamchunk_original_support_streaming_message"
    PLATFORM_META_PATCHED_KEY = "_streamchunk_platform_meta_patched"
    ORIGINAL_PLATFORM_META_KEY = "_streamchunk_original_platform_meta"
    ORIGINAL_PLATFORM_ALIAS_KEY = "_streamchunk_original_platform_alias"
    TOOL_START_COUNT_KEY = "_streamchunk_tool_start_count"
    TOOL_BOUNDARY_FLUSH_CALLBACK_KEY = "_streamchunk_tool_boundary_flush_callback"
    LAST_SHORT_CHUNK_SENT_AT_KEY = "_streamchunk_last_short_chunk_sent_at"
    LLM_RESPONSE_RESTORE_KEY = "_streamchunk_llm_response_restore"
    STREAM_PATCHED_KEY = "_streamchunk_stream_patched"
    TAG_PATTERN = re.compile(r"^\s*\[(SHORT|LONG)\]\s*", re.IGNORECASE)
    THINKING_OPEN_TAG = "<think>"
    THINKING_CLOSE_TAG = "</think>"
    PROMPT_SENTINEL = "[STREAMCHUNK_LENGTH_TAG_RULE]"
    PROMPT_TEMPLATE = (
        "[STREAMCHUNK_LENGTH_TAG_RULE]\n"
        "你必须在回复开始前输出一个系统标签：[SHORT] 或 [LONG]。\n"
        "【[SHORT] 使用条件】你打算回复的字数在100字以内时使用，会被系统分为多条消息发送，更像人们的聊天风格。例如日常闲聊时使用 [SHORT]。\n"
        "【[LONG] 使用条件】你打算回复的字数超过100字时使用，只会发送一条消息，避免消息太多刷屏。例如你的回复是一篇短文、文章、代码、包含多个列表项或者是长段落时使用 [LONG]。\n"
        "标签必须放在最前面，不要附带任何解释。"
    )
    TAG_DETECT_MAX_CHARS = 64
    TAG_PREFIX_CANDIDATES = ("[short]", "[long]")
    REPEATED_PUNCTUATION_CHARS = frozenset({"…", "—"})
    THINKING_BUFFER_MAX_CHARS = 4096

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
        self.inject_prompt = self._as_bool(
            self._config.get("inject_prompt", True), True
        )

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
        self.max_short_chunks = self._as_int(
            self._config.get("max_short_chunks", 0),
            0,
            min_value=0,
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
            if self.split_pattern.search("") is not None:
                raise ValueError("分段正则不能匹配空字符串")
        except (re.error, ValueError) as e:
            logger.error("streamchunk: 分段标点正则表达式无效: %s，将回退到默认设置", e)
            self.split_pattern = re.compile(r"[。？！!?；;…\n]")

        drop_punctuations = str(
            self._config.get("drop_punctuations", r"[。.]"),
        )
        try:
            self.drop_pattern = re.compile(drop_punctuations)
            if self.drop_pattern.search("") is not None:
                raise ValueError("丢弃正则不能匹配空字符串")
        except (re.error, ValueError) as e:
            logger.error(
                "streamchunk: 丢弃分段符号正则表达式无效: %s，将回退到默认设置", e
            )
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
        event.set_extra(self.ORIGINAL_PLATFORM_META_KEY, event.platform_meta)
        event.set_extra(self.ORIGINAL_PLATFORM_ALIAS_KEY, event.platform)

        try:
            patched_meta = replace(event.platform_meta, support_streaming_message=True)
        except Exception as e:
            logger.warning(
                "streamchunk: 平台元数据改写失败，将仅依赖 enable_streaming: %s", e
            )
            return

        event.platform_meta = patched_meta
        # keep compatibility with event.platform alias
        event.platform = patched_meta
        event.set_extra(self.PLATFORM_META_PATCHED_KEY, True)

    def _restore_event_streaming_pipeline(self, event: AstrMessageEvent) -> None:
        if not event.get_extra(self.PLATFORM_META_PATCHED_KEY, False):
            return

        original_meta = event.get_extra(self.ORIGINAL_PLATFORM_META_KEY, None)
        if original_meta is not None:
            event.platform_meta = original_meta

        original_platform = event.get_extra(self.ORIGINAL_PLATFORM_ALIAS_KEY, None)
        if original_platform is not None:
            event.platform = original_platform

        event.set_extra(self.ORIGINAL_STREAMING_SUPPORT_KEY, None)
        event.set_extra(self.ORIGINAL_PLATFORM_META_KEY, None)
        event.set_extra(self.ORIGINAL_PLATFORM_ALIAS_KEY, None)
        event.set_extra(self.PLATFORM_META_PATCHED_KEY, False)

    def _should_handle_event(self, event: AstrMessageEvent) -> bool:
        if not self.enabled:
            return False
        if self.only_unsupported_platform and self._is_platform_streaming_supported(
            event
        ):
            return False
        return True

    def _get_tool_start_count(self, event: AstrMessageEvent) -> int:
        return self._as_int(
            event.get_extra(self.TOOL_START_COUNT_KEY, 0),
            0,
            min_value=0,
        )

    @filter.on_using_llm_tool(priority=100)
    async def on_using_llm_tool(
        self,
        event: AstrMessageEvent,
        tool: Any,
        tool_args: dict[str, Any] | None,
    ):
        _ = tool
        _ = tool_args
        if not self._should_handle_event(event):
            return

        event.set_extra(
            self.TOOL_START_COUNT_KEY,
            self._get_tool_start_count(event) + 1,
        )

        flush_callback = cast(
            Any,
            event.get_extra(self.TOOL_BOUNDARY_FLUSH_CALLBACK_KEY, None),
        )
        if callable(flush_callback):
            await cast(Any, flush_callback)()
            await asyncio.sleep(0)

    @filter.on_waiting_llm_request(priority=100)
    async def on_waiting_llm_request(self, event: AstrMessageEvent):
        """Patch local stream handling before LLM execution pipeline starts."""
        if not self._should_handle_event(event):
            return

        is_stream_unsupported = not self._is_platform_streaming_supported(event)

        if (
            self.local_stream_processing
            and is_stream_unsupported
            and not event.get_extra(self.STREAM_PATCHED_KEY, False)
        ):
            # Keep model-side streaming enabled and force pipeline streaming branch.
            event.set_extra("enable_streaming", True)
            self._force_event_streaming_pipeline(event)

            async def patched_send_streaming(
                generator: AsyncGenerator[MessageChain, None],
                use_fallback: bool = False,
            ) -> None:
                _ = use_fallback
                logger.info(
                    "streamchunk: 已接管 send_streaming，开始本地消费流式输出。"
                )
                await self._process_stream_locally(event, generator)

            event.send_streaming = patched_send_streaming  # type: ignore[method-assign]
            event.set_extra(self.STREAM_PATCHED_KEY, True)

    @filter.on_llm_request(priority=100)
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """清理历史控制标签，并按配置注入本轮标签规则。"""
        if not self._should_handle_event(event):
            return

        self._strip_mode_tags_from_contexts(req.contexts)

        if not self.inject_prompt:
            return

        system_prompt = req.system_prompt or ""
        if self.PROMPT_SENTINEL in system_prompt:
            return

        if system_prompt and not system_prompt.endswith("\n"):
            system_prompt += "\n"
        system_prompt += f"{self.PROMPT_TEMPLATE}\n"
        req.system_prompt = system_prompt

    def _strip_leading_mode_tags(self, text: str) -> str:
        cleaned = text.lstrip()
        while True:
            match = self.TAG_PATTERN.match(cleaned)
            if not match:
                return cleaned
            cleaned = cleaned[match.end() :].lstrip()

    def _strip_thinking(self, text: str) -> str | None:
        """移除开头的思考块；没有思考块时原样返回，思考块未闭合时返回 None。"""
        stripped = text.lstrip()
        if not stripped.startswith(self.THINKING_OPEN_TAG):
            return text
        closing_index = stripped.find(self.THINKING_CLOSE_TAG)
        if closing_index == -1:
            return None
        return stripped[closing_index + len(self.THINKING_CLOSE_TAG) :]

    def _extract_mode(self, text: str) -> tuple[str | None, str]:
        body = self._strip_thinking(text)
        if body is None:
            return None, ""
        if body != text:
            body = body.lstrip()

        match = self.TAG_PATTERN.match(body)
        if not match:
            return None, body
        mode = match.group(1).lower()
        cleaned = self._strip_leading_mode_tags(body[match.end() :])
        return mode, cleaned

    def _strip_leading_control_tags(self, text: str) -> str:
        """只移除回复开头的分段控制标签，保留其他文本。"""
        if not self.TAG_PATTERN.match(text):
            return text
        return self._strip_leading_mode_tags(text)

    @staticmethod
    def _get_mapping_field(item: dict[str, Any], name: str) -> Any:
        return item.get(name)

    @staticmethod
    def _set_mapping_field(item: dict[str, Any], name: str, value: Any) -> None:
        item[name] = value

    @staticmethod
    def _get_attribute_field(item: Any, name: str) -> Any:
        return getattr(item, name, None)

    def _strip_mode_tags_from_messages(
        self,
        messages: list[Any],
        get_field: Callable[[Any, str], Any],
        set_field: Callable[[Any, str, Any], None],
    ) -> None:
        """移除助手消息开头的分段控制标签，不改写用户和工具消息。

        消息与内容块可以是字典（请求上下文）或对象（运行历史），
        通过 get_field/set_field 统一读写。
        """
        for message_index in range(len(messages) - 1, -1, -1):
            message = messages[message_index]
            if get_field(message, "role") != "assistant":
                continue

            content = get_field(message, "content")
            if isinstance(content, str):
                cleaned = self._strip_leading_control_tags(content)
                if cleaned != content:
                    if cleaned or get_field(message, "tool_calls"):
                        set_field(message, "content", cleaned)
                    else:
                        del messages[message_index]
                continue
            if not isinstance(content, list):
                continue

            first_text_index = 0
            while (
                first_text_index < len(content)
                and get_field(content[first_text_index], "type") == "think"
            ):
                first_text_index += 1

            end_index = first_text_index
            while (
                end_index < len(content)
                and get_field(content[end_index], "type") == "text"
            ):
                end_index += 1
            if end_index == first_text_index:
                continue

            text = "".join(
                get_field(content[index], "text")
                for index in range(first_text_index, end_index)
            )
            cleaned = self._strip_leading_control_tags(text)
            removed_chars = len(text) - len(cleaned)
            if not removed_chars:
                continue

            index = first_text_index
            while index < end_index and removed_chars:
                part = content[index]
                part_text = get_field(part, "text")
                if removed_chars < len(part_text):
                    set_field(part, "text", part_text[removed_chars:])
                    break
                removed_chars -= len(part_text)
                index += 1
            del content[first_text_index:index]
            if not content and not get_field(message, "tool_calls"):
                del messages[message_index]

    def _strip_mode_tags_from_contexts(self, contexts: list[dict[str, Any]]) -> None:
        """清理请求中已存在的助手标签。"""
        self._strip_mode_tags_from_messages(
            contexts,
            self._get_mapping_field,
            self._set_mapping_field,
        )

    def _strip_mode_tags_from_history(self, run_context: Any) -> None:
        """从待持久化的全部助手消息中移除分段控制标签。"""
        messages = getattr(run_context, "messages", None)
        if not isinstance(messages, list):
            return
        self._strip_mode_tags_from_messages(
            messages,
            self._get_attribute_field,
            setattr,
        )

    def _restore_llm_response(self, event: AstrMessageEvent, response: Any) -> None:
        """恢复仅为平台历史清理而临时改写的模型响应。"""
        restore_data = event.get_extra(self.LLM_RESPONSE_RESTORE_KEY, None)
        event.set_extra(self.LLM_RESPONSE_RESTORE_KEY, None)
        if not isinstance(restore_data, tuple) or restore_data[0] is not response:
            return

        original_chain, original_components, original_text = restore_data[1:]
        if original_chain is None:
            response.completion_text = original_text
            return

        response.result_chain = original_chain
        original_chain.chain[:] = original_components

    @filter.on_llm_response(priority=100)
    async def on_llm_response(self, event: AstrMessageEvent, response: Any):
        """临时移除标签，避免平台群聊历史保存控制文本。"""
        if not self._should_handle_event(event):
            return

        text = getattr(response, "completion_text", None)
        if not isinstance(text, str):
            return

        cleaned = self._strip_leading_control_tags(text)
        if cleaned == text:
            return

        original_chain = getattr(response, "result_chain", None)
        original_components = (
            list(original_chain.chain) if original_chain is not None else None
        )
        event.set_extra(
            self.LLM_RESPONSE_RESTORE_KEY,
            (response, original_chain, original_components, text),
        )
        response.completion_text = cleaned

    @filter.on_agent_done(priority=100)
    async def on_agent_done(
        self,
        event: AstrMessageEvent,
        run_context: Any,
        response: Any,
    ):
        """清理内部会话历史后恢复用于发送的完整模型响应和平台元数据。"""
        try:
            if self._should_handle_event(event):
                self._strip_mode_tags_from_history(run_context)
        finally:
            self._restore_llm_response(event, response)
            # 流式消费结束时也会恢复；这里覆盖未进入本地流式消费的情况。
            self._restore_event_streaming_pipeline(event)

    def _detect_mode_from_prefix(self, text: str) -> tuple[str | None, str, bool]:
        """Detect mode from stream prefix.

        Returns:
            mode: "short"/"long"/"auto"/None
            remaining: text after removing tag if present
            need_more: True when tag parsing is still uncertain
        """
        has_thinking = text.lstrip().startswith(self.THINKING_OPEN_TAG)
        body = self._strip_thinking(text)
        if body is None:
            return None, "", True

        mode, cleaned = self._extract_mode(body)
        if mode is not None:
            return mode, cleaned, False

        if self._could_be_tag_prefix(body, allow_thinking=not has_thinking):
            return None, "", True

        return self.default_mode_no_tag, body, False

    def _could_be_tag_prefix(self, text: str, allow_thinking: bool) -> bool:
        """判断流式前缀是否仍可能补全为模式标签或思考块起始标记。"""
        if len(text) >= self.TAG_DETECT_MAX_CHARS:
            return False
        stripped = text.lstrip()
        if not stripped:
            return True
        lowered = stripped.lower()
        if any(tag.startswith(lowered) for tag in self.TAG_PREFIX_CANDIDATES):
            return True
        return allow_thinking and self.THINKING_OPEN_TAG.startswith(stripped)

    def _resolve_mode(self, mode: str, text: str) -> str:
        """将 auto 模式按文本长度解析为 short 或 long。"""
        if mode != "auto":
            return mode
        return "short" if len(text) <= self.auto_short_max_chars else "long"

    def _default_mode(self, text: str) -> str:
        return self._resolve_mode(self.default_mode_no_tag, text)

    @staticmethod
    def _is_grapheme_extend(char: str) -> bool:
        return (
            unicodedata.combining(char) != 0
            or "\ufe00" <= char <= "\ufe0f"
            or "\U000e0100" <= char <= "\U000e01ef"
            or "\U0001f3fb" <= char <= "\U0001f3ff"
        )

    @staticmethod
    def _is_regional_indicator(char: str) -> bool:
        return "\U0001f1e6" <= char <= "\U0001f1ff"

    def _safe_chunk_end(self, text: str, start: int, end: int) -> int:
        """Avoid splitting common Unicode grapheme clusters at a hard boundary."""
        text_length = len(text)
        if end >= text_length:
            return text_length

        while end < text_length:
            current = text[end]
            previous = text[end - 1] if end > start else ""
            if (
                self._is_grapheme_extend(current)
                or current == "\u200d"
                or (current == previous and current in self.REPEATED_PUNCTUATION_CHARS)
            ):
                end += 1
                continue
            if previous == "\u200d":
                end += 1
                continue
            if self._is_regional_indicator(current):
                regional_count = 0
                cursor = end - 1
                while cursor >= start and self._is_regional_indicator(text[cursor]):
                    regional_count += 1
                    cursor -= 1
                if regional_count % 2 == 1:
                    end += 1
                    continue
            break

        return end

    def _normalize_chunk(self, text: str) -> str:
        chunk = text.strip()
        if not chunk:
            return ""

        suffix_match = None
        for match in self.drop_pattern.finditer(chunk):
            if match.start() < match.end() and match.end() == len(chunk):
                suffix_match = match
        if suffix_match is not None:
            chunk = chunk[: suffix_match.start()].strip()
        return chunk

    def _get_forward_threshold(self, event: AstrMessageEvent) -> int:
        get_config = getattr(self.context, "get_config", None)
        if not callable(get_config):
            return 0

        try:
            config = get_config(getattr(event, "unified_msg_origin", None))
        except Exception as e:
            logger.warning(
                "streamchunk: failed to get AstrBot config for forward threshold: %s",
                e,
            )
            return 0

        if not hasattr(config, "get"):
            return 0

        try:
            platform_settings = config.get("platform_settings", {})
            threshold = int(platform_settings.get("forward_threshold", 0))
        except (TypeError, ValueError, AttributeError):
            return 0

        return max(threshold, 0)

    def _should_send_as_forward(self, event: AstrMessageEvent, text: str) -> bool:
        if event.get_platform_name() != "aiocqhttp":
            return False

        threshold = self._get_forward_threshold(event)
        if threshold <= 0:
            return False

        return len(text) > threshold

    async def _send_forward_message(self, event: AstrMessageEvent, text: str) -> bool:
        normalized = text.strip()
        if not normalized or not self._should_send_as_forward(event, normalized):
            return False

        logger.info(
            "streamchunk: text length %s exceeded forward threshold, sending merged forward instead.",
            len(normalized),
        )
        node = Node(
            uin=event.get_self_id(),
            name="AstrBot",
            content=[Plain(normalized)],
        )
        await event.send(MessageChain([node]))
        return True

    def _split_short_text_incremental(
        self,
        text: str,
        final: bool = False,
        sent_chunks: int = 0,
    ) -> tuple[list[str], str, bool]:
        if self.max_short_chunks > 0 and sent_chunks >= self.max_short_chunks:
            return [], text, True

        chunks: list[str] = []
        last_cut = 0
        text_length = len(text)

        while last_cut < text_length:
            max_end = min(last_cut + self.max_chunk_chars, text_length)
            search_start = last_cut
            split_end: int | None = None

            while search_start < text_length:
                match = self.split_pattern.search(text, search_start)
                if match is None:
                    break
                if match.start() == match.end():
                    search_start = match.end() + 1
                    continue
                if match.end() - last_cut < self.min_chunk_chars:
                    search_start = match.start() + 1
                    continue
                if match.end() > max_end:
                    break
                if (
                    not final
                    and match.end() == text_length
                    and match.group() in self.REPEATED_PUNCTUATION_CHARS
                ):
                    break
                split_end = self._safe_chunk_end(text, last_cut, match.end())
                break

            if split_end is None:
                if text_length - last_cut < self.max_chunk_chars:
                    break
                split_end = self._safe_chunk_end(text, last_cut, max_end)

            part = self._normalize_chunk(text[last_cut:split_end])
            if part:
                chunks.append(part)
            last_cut = split_end
            if (
                self.max_short_chunks > 0
                and sent_chunks + len(chunks) >= self.max_short_chunks
            ):
                return chunks, text[last_cut:], True

        remain = text[last_cut:]
        if final and remain.strip():
            if (
                self.max_short_chunks > 0
                and sent_chunks + len(chunks) >= self.max_short_chunks
            ):
                return chunks, remain, True
            part = self._normalize_chunk(remain)
            if part:
                chunks.append(part)
            remain = ""

        return chunks, remain, False

    async def _send_short_text(
        self,
        event: AstrMessageEvent,
        text: str,
    ) -> tuple[int, bool]:
        return await self._send_final_short_text(event, text, sent_chunks=0)

    async def _send_chunks(self, event: AstrMessageEvent, chunks: list[str]) -> None:
        total = len(chunks)
        loop = asyncio.get_running_loop()
        for idx, chunk in enumerate(chunks):
            if self.segment_interval_seconds > 0:
                last_sent_at = event.get_extra(
                    self.LAST_SHORT_CHUNK_SENT_AT_KEY,
                    None,
                )
                if isinstance(last_sent_at, (int, float)):
                    elapsed = loop.time() - last_sent_at
                    remaining_interval = self.segment_interval_seconds - elapsed
                    if remaining_interval > 0:
                        await asyncio.sleep(remaining_interval)

            logger.debug(
                "streamchunk: 发送分段消息 %s/%s (len=%s)",
                idx + 1,
                total,
                len(chunk),
            )
            await event.send(MessageChain([Plain(chunk)]))
            event.set_extra(self.LAST_SHORT_CHUNK_SENT_AT_KEY, loop.time())

    async def _send_long_text_if_present(
        self,
        event: AstrMessageEvent,
        long_buffer: str,
    ) -> str:
        text = long_buffer.strip()
        if text:
            if await self._send_forward_message(event, text):
                return ""
            await event.send(MessageChain([Plain(text)]))
            return ""
        return long_buffer

    async def _send_final_short_text(
        self,
        event: AstrMessageEvent,
        text: str,
        sent_chunks: int,
    ) -> tuple[int, bool]:
        """发送 SHORT 缓冲的全部内容，达到分段上限时剩余内容整段发送。"""
        chunks, remain, limit_reached = self._split_short_text_incremental(
            text,
            final=True,
            sent_chunks=sent_chunks,
        )
        await self._send_chunks(event, chunks)
        if limit_reached and remain.strip():
            logger.info("streamchunk: SHORT 模式分段达到上限，剩余内容改为整段发送。")
            await self._send_long_text_if_present(event, remain)
        return len(chunks), limit_reached

    async def _flush_text_buffer(
        self,
        event: AstrMessageEvent,
        mode: str,
        text_buffer: str,
        final: bool,
    ) -> str:
        if mode == "short":
            chunks, remain, _ = self._split_short_text_incremental(
                text_buffer,
                final=final,
            )
            await self._send_chunks(event, chunks)
            return remain
        if final:
            return await self._send_long_text_if_present(event, text_buffer)
        return text_buffer

    def _collect_buffered_break_text(
        self,
        mode: str | None,
        prefix_buffer: str,
        text_buffer: str,
    ) -> str:
        pending_prefix = prefix_buffer
        if mode is None and pending_prefix:
            _, pending_prefix = self._extract_mode(pending_prefix)
        return f"{pending_prefix}{text_buffer}".strip()

    async def _process_stream_locally(
        self,
        event: AstrMessageEvent,
        generator: AsyncGenerator[MessageChain, None],
    ) -> None:
        mode: str | None = None
        prefix_buffer = ""
        text_buffer = ""
        short_chunks_sent = 0
        discarding_thinking = False
        thinking_close_tail = ""
        seen_tool_start_count = self._get_tool_start_count(event)

        async def flush_tool_boundary_buffer(log_message: str) -> None:
            nonlocal mode, prefix_buffer, text_buffer, short_chunks_sent
            nonlocal discarding_thinking, thinking_close_tail, seen_tool_start_count

            pending = self._collect_buffered_break_text(
                mode,
                prefix_buffer,
                text_buffer,
            )
            if pending:
                logger.info(log_message)
                if not await self._send_forward_message(event, pending):
                    await event.send(MessageChain([Plain(pending)]))

            mode = None
            prefix_buffer = ""
            text_buffer = ""
            short_chunks_sent = 0
            discarding_thinking = False
            thinking_close_tail = ""
            seen_tool_start_count = self._get_tool_start_count(event)

        async def flush_on_tool_start() -> None:
            await flush_tool_boundary_buffer(
                "streamchunk: 检测到工具开始事件，立即发送工具前缓冲文本。"
            )

        event.set_extra(self.TOOL_BOUNDARY_FLUSH_CALLBACK_KEY, flush_on_tool_start)

        try:
            async for chain in generator:
                current_tool_start_count = self._get_tool_start_count(event)
                if current_tool_start_count > seen_tool_start_count:
                    await flush_tool_boundary_buffer(
                        "streamchunk: 检测到工具调用边界，先发送工具前缓冲文本。"
                    )

                if not isinstance(chain, MessageChain):
                    continue

                if chain.type == "break":
                    # Agent inserts a break before starting tool execution.
                    # Send buffered pre-tool text separately and reset aggregation state.
                    await flush_tool_boundary_buffer(
                        "streamchunk: 收到流式 break，发送工具前缓冲文本。"
                    )
                    continue

                for comp in chain.chain:
                    if not isinstance(comp, Plain):
                        if (
                            mode is None
                            and prefix_buffer.strip()
                            and self._strip_thinking(prefix_buffer) is not None
                        ):
                            mode = self._default_mode(prefix_buffer)
                            text_buffer += self._collect_buffered_break_text(
                                mode=None,
                                prefix_buffer=prefix_buffer,
                                text_buffer="",
                            )
                            prefix_buffer = ""

                        if mode is not None:
                            mode = self._resolve_mode(mode, text_buffer)

                        if mode == "short":
                            sent_result = await self._send_final_short_text(
                                event,
                                text_buffer,
                                short_chunks_sent,
                            )
                            sent_count, limit_reached = sent_result
                            short_chunks_sent += sent_count
                            text_buffer = ""
                            if limit_reached:
                                mode = "long"
                        elif mode is not None:
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
                    logger.debug(
                        "streamchunk: 收到流式文本片段 (len=%s)", len(incoming)
                    )

                    if discarding_thinking:
                        incoming = thinking_close_tail + incoming
                        closing_index = incoming.find(self.THINKING_CLOSE_TAG)
                        if closing_index == -1:
                            thinking_close_tail = incoming[
                                -(len(self.THINKING_CLOSE_TAG) - 1) :
                            ]
                            continue
                        discarding_thinking = False
                        thinking_close_tail = ""
                        incoming = incoming[
                            closing_index + len(self.THINKING_CLOSE_TAG) :
                        ]
                        if not incoming:
                            continue

                    if mode is None:
                        prefix_buffer += incoming
                        mode_candidate, remaining, need_more = (
                            self._detect_mode_from_prefix(
                                prefix_buffer,
                            )
                        )
                        if need_more:
                            if (
                                prefix_buffer.lstrip().startswith(
                                    self.THINKING_OPEN_TAG
                                )
                                and len(prefix_buffer) > self.THINKING_BUFFER_MAX_CHARS
                            ):
                                logger.warning(
                                    "streamchunk: 未闭合的思考内容超过缓冲上限，已丢弃。"
                                )
                                thinking_close_tail = prefix_buffer[
                                    -(len(self.THINKING_CLOSE_TAG) - 1) :
                                ]
                                prefix_buffer = ""
                                discarding_thinking = True
                            continue
                        mode = mode_candidate
                        if mode is not None:
                            logger.info(
                                "streamchunk: 从模型输出流提取到响应模式: [%s]",
                                mode.upper(),
                            )
                        prefix_buffer = ""
                        incoming = remaining
                        if not incoming:
                            continue

                    text_buffer += incoming

                    # auto 在超过阈值前继续缓冲，避免尚未生成完就按 short 发送。
                    if (
                        mode == "auto"
                        and self._resolve_mode(mode, text_buffer) == "long"
                    ):
                        mode = "long"

                    if mode == "short":
                        chunks, remain, limit_reached = (
                            self._split_short_text_incremental(
                                text_buffer,
                                final=False,
                                sent_chunks=short_chunks_sent,
                            )
                        )
                        await self._send_chunks(event, chunks)
                        short_chunks_sent += len(chunks)
                        text_buffer = remain
                        if limit_reached:
                            logger.info(
                                "streamchunk: SHORT 模式分段达到上限，剩余内容切换为整段发送。"
                            )
                            mode = "long"

            if mode is None:
                if discarding_thinking:
                    prefix_buffer = ""
                mode_candidate, remaining = self._extract_mode(prefix_buffer)
                text_buffer += remaining
                mode = mode_candidate or self.default_mode_no_tag
            elif prefix_buffer:
                text_buffer += prefix_buffer

            mode = self._resolve_mode(mode, text_buffer)

            if mode not in {"short", "long"}:
                mode = "long"

            logger.info(
                "streamchunk: 流式返回处理完毕，最终采取发送模式: [%s]",
                mode.upper(),
            )
            if mode == "short":
                await self._send_final_short_text(
                    event,
                    text_buffer,
                    short_chunks_sent,
                )
                return
            await self._flush_text_buffer(event, mode, text_buffer, final=True)
        finally:
            event.set_extra(self.TOOL_BOUNDARY_FLUSH_CALLBACK_KEY, None)
            self._restore_event_streaming_pipeline(event)

    @filter.on_decorating_result(priority=-100)
    async def on_decorating_result(self, event: AstrMessageEvent):
        """Apply tag-based strategy for non-streaming outputs."""
        result = event.get_result()
        if result is None:
            return

        if result.result_content_type in {
            ResultContentType.STREAMING_RESULT,
            ResultContentType.STREAMING_FINISH,
        }:
            return

        # 非流式回退可能没有触发 on_agent_done，且空消息链不会触发发送后钩子。
        self._restore_event_streaming_pipeline(event)
        if not self._should_handle_event(event) or not result.chain:
            return

        if self.only_model_result and not result.is_model_result():
            return

        if not all(isinstance(comp, Plain) for comp in result.chain):
            return

        text = "".join(comp.text for comp in result.chain).strip()
        if not text:
            return

        body = self._strip_thinking(text)
        if body is None:
            logger.warning("streamchunk: 思考内容未闭合，已跳过发送。")
            result.chain = []
            return

        mode, text = self._extract_mode(text)
        if mode is None:
            mode = self._default_mode(text)
            logger.info("streamchunk: 未提取到标签，使用回退模式: [%s]", mode.upper())
        else:
            logger.info("streamchunk: 从完整文本提取到响应模式: [%s]", mode.upper())

        if not text.strip():
            logger.warning("streamchunk: 移除控制标签或思考内容后为空，已跳过发送。")
            result.chain = []
            return

        if await self._send_forward_message(event, text):
            logger.info(
                "streamchunk: sent merged forward message using AstrBot forward_threshold."
            )
            result.chain = []
            return

        if mode == "short":
            chunk_count, limit_reached = await self._send_short_text(event, text)
            logger.info(
                "streamchunk: [%s] 模式，已发送 %s 个分段消息。",
                mode.upper(),
                chunk_count,
            )
            if limit_reached:
                logger.info("streamchunk: SHORT 模式剩余内容已改为整段发送。")
        else:
            await event.send(MessageChain([Plain(text)]))
            logger.info(
                "streamchunk: [%s] 模式，已发送一条完整长文本消息。", mode.upper()
            )

        # Clear chain to avoid duplicate send in respond stage.
        result.chain = []
