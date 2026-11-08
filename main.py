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
    LLM_RESPONSE_MODE_KEY = "_streamchunk_llm_response_mode"
    STREAM_PATCHED_KEY = "_streamchunk_stream_patched"
    TAG_PATTERN = re.compile(r"^\s*\[(SHORT|LONG)\]\s*", re.IGNORECASE)
    # 模型偶尔把标签放到末尾，或按 Markdown 转义写成 \[SHORT]。
    TRAILING_TAG_PATTERN = re.compile(r"\s*\\?\[(SHORT|LONG)\\?\]\s*\Z", re.IGNORECASE)
    THINKING_OPEN_TAG = "<think>"
    THINKING_CLOSE_TAG = "</think>"
    PROMPT_SENTINEL = "[STREAMCHUNK_LENGTH_TAG_RULE]"
    PROMPT_TEMPLATE = (
        "[STREAMCHUNK_LENGTH_TAG_RULE]\n"
        "你必须在回复开始前输出一个系统标签：[SHORT] 或 [LONG]。\n"
        "按回复类型选择标签，不要按预估字数选择。默认使用 [SHORT]。\n"
        "【[SHORT] 使用条件】聊天式回复都用 [SHORT]，包括闲聊、表态、吐槽、拒绝、简短的建议或解答；即使分成好几段，也用 [SHORT]。系统会把它拆成多条消息发送，像真人聊天。\n"
        "【[LONG] 使用条件】只有回复包含代码、列表、表格、步骤教程，或是文章、详细讲解等需要整体阅读的内容时，才用 [LONG]。系统会整段发送一条消息，避免刷屏。\n"
        "拿不准时选 [SHORT]。\n"
        "标签必须放在回复最前面，不要放在末尾或正文中间，也不要附带任何解释。\n"
        "历史消息中的标签已被系统移除，无论历史格式如何，本次回复都必须以标签开头。"
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

    @staticmethod
    def _create_temp_text_part(text: str) -> Any | None:
        """创建仅发给 provider、不写入历史的文本块；旧版本不支持时返回 None。"""
        try:
            from astrbot.core.agent.message import TextPart

            text_part = TextPart(text=text)
        except (ImportError, TypeError, ValueError):
            return None

        mark_as_temp = getattr(text_part, "mark_as_temp", None)
        if not callable(mark_as_temp):
            return None
        return mark_as_temp()

    def _append_prompt_to_user_content(self, req: ProviderRequest) -> bool:
        """将本轮标签规则追加到当前用户消息末尾，而不是历史上下文。"""
        extra_parts = getattr(req, "extra_user_content_parts", None)
        if isinstance(extra_parts, list):
            for part in extra_parts:
                text = (
                    part.get("text")
                    if isinstance(part, dict)
                    else getattr(part, "text", None)
                )
                if isinstance(text, str) and self.PROMPT_SENTINEL in text:
                    return True
            temp_part = self._create_temp_text_part(self.PROMPT_TEMPLATE)
            if temp_part is None:
                return False
            extra_parts.append(temp_part)
            return True

        # 兼容没有 extra_user_content_parts 的旧版请求对象。
        prompt = getattr(req, "prompt", None)
        if isinstance(prompt, str):
            if self.PROMPT_SENTINEL in prompt:
                return True
            separator = "" if not prompt or prompt.endswith("\n") else "\n"
            req.prompt = f"{prompt}{separator}{self.PROMPT_TEMPLATE}"
            return True

        return False

    def _append_prompt_to_system_prompt(self, req: ProviderRequest) -> None:
        """把本轮标签规则追加到系统提示词，作为无法进入用户消息时的兜底。"""
        system_prompt = req.system_prompt or ""
        if self.PROMPT_SENTINEL in system_prompt:
            return

        if system_prompt and not system_prompt.endswith("\n"):
            system_prompt += "\n"
        system_prompt += f"{self.PROMPT_TEMPLATE}\n"
        req.system_prompt = system_prompt

    @filter.on_llm_request(priority=100)
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """清理历史控制标签，并按配置注入本轮标签规则。"""
        if not self._should_handle_event(event):
            return

        event.set_extra(self.LLM_RESPONSE_MODE_KEY, None)
        self._strip_mode_tags_from_contexts(req.contexts)

        if not self.inject_prompt:
            return

        if self._append_prompt_to_user_content(req):
            return

        self._append_prompt_to_system_prompt(req)

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

    def _extract_mode(
        self,
        text: str,
        include_trailing: bool = True,
    ) -> tuple[str | None, str]:
        """提取模式标签。include_trailing 仅用于完整文本，流式前缀不能判断末尾。"""
        body = self._strip_thinking(text)
        if body is None:
            return None, ""
        if body != text:
            body = body.lstrip()

        match = self.TAG_PATTERN.match(body)
        if not match:
            if not include_trailing:
                return None, body
            # 开头没有标签时，才采用末尾标签决定模式。
            return self._extract_trailing_mode(body)
        mode = match.group(1).lower()
        cleaned = self._strip_leading_mode_tags(body[match.end() :])
        if include_trailing:
            cleaned = self._extract_trailing_mode(cleaned)[1]
        return mode, cleaned

    def _extract_trailing_mode(self, text: str) -> tuple[str | None, str]:
        """识别并移除回复末尾的分段控制标签；没有标签时原样返回。"""
        mode: str | None = None
        cleaned = text
        while True:
            match = self.TRAILING_TAG_PATTERN.search(cleaned)
            if not match:
                break
            if mode is None:
                mode = match.group(1).lower()
            cleaned = cleaned[: match.start()]
        if mode is None:
            return None, text
        return mode, cleaned

    def _strip_leading_control_tags(self, text: str) -> str:
        """只移除回复开头的分段控制标签，保留其他文本。"""
        if not self.TAG_PATTERN.match(text):
            return text
        return self._strip_leading_mode_tags(text)

    def _strip_control_tags(self, text: str) -> str:
        """移除回复开头和末尾的分段控制标签，正文中间的标签保持不变。"""
        cleaned = self._strip_leading_control_tags(text)
        return self._extract_trailing_mode(cleaned)[1]

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
        """移除助手消息开头和末尾的分段控制标签，不改写用户和工具消息。

        消息与内容块可以是字典（请求上下文）或对象（运行历史），
        通过 get_field/set_field 统一读写。
        """
        for message_index in range(len(messages) - 1, -1, -1):
            message = messages[message_index]
            if get_field(message, "role") != "assistant":
                continue

            content = get_field(message, "content")
            if isinstance(content, str):
                cleaned = self._strip_control_tags(content)
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

            changed = False
            if end_index > first_text_index:
                text = "".join(
                    get_field(content[index], "text")
                    for index in range(first_text_index, end_index)
                )
                cleaned = self._strip_leading_control_tags(text)
                removed_chars = len(text) - len(cleaned)
                if removed_chars:
                    changed = True
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

            if self._strip_trailing_tags_from_parts(content, get_field, set_field):
                changed = True
            if changed and not content and not get_field(message, "tool_calls"):
                del messages[message_index]

    def _strip_trailing_tags_from_parts(
        self,
        content: list[Any],
        get_field: Callable[[Any, str], Any],
        set_field: Callable[[Any, str, Any], None],
    ) -> bool:
        """移除末尾连续文本块中的分段控制标签，删除因此耗尽的文本块。"""
        start_index = len(content)
        while start_index > 0 and get_field(content[start_index - 1], "type") == "text":
            start_index -= 1
        if start_index == len(content):
            return False

        text = "".join(
            get_field(content[index], "text")
            for index in range(start_index, len(content))
        )
        _, cleaned = self._extract_trailing_mode(text)
        removed_chars = len(text) - len(cleaned)
        if not removed_chars:
            return False

        index = len(content)
        while index > start_index and removed_chars:
            part = content[index - 1]
            part_text = get_field(part, "text")
            if removed_chars < len(part_text):
                set_field(part, "text", part_text[:-removed_chars])
                break
            removed_chars -= len(part_text)
            index -= 1
        del content[index:]
        return True

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

    def _consume_response_mode(self, event: AstrMessageEvent) -> str | None:
        """取出并清除已清理标签的模型响应模式。"""
        mode = cast(str | None, event.get_extra(self.LLM_RESPONSE_MODE_KEY, None))
        event.set_extra(self.LLM_RESPONSE_MODE_KEY, None)
        return mode

    @filter.on_llm_response(priority=100)
    async def on_llm_response(self, event: AstrMessageEvent, response: Any):
        """记录分段模式并移除标签，保留后续插件对响应的修改。"""
        if not self._should_handle_event(event):
            return

        event.set_extra(self.LLM_RESPONSE_MODE_KEY, None)
        result_chain = getattr(response, "result_chain", None)
        if result_chain is None:
            text = getattr(response, "completion_text", None)
        else:
            text = "".join(
                comp.text for comp in result_chain.chain if isinstance(comp, Plain)
            )
        if not isinstance(text, str):
            return

        mode, _ = self._extract_mode(text)
        event.set_extra(self.LLM_RESPONSE_MODE_KEY, mode)
        cleaned = self._strip_control_tags(text)
        if cleaned == text:
            return

        if result_chain is None:
            response.completion_text = cleaned
            return

        # 只保留正文对应的文本范围，避免 completion_text setter 重排图文组件。
        body_start = len(text) - len(self._strip_leading_control_tags(text))
        body_end = body_start + len(cleaned)
        text_offset = 0
        cleaned_components = []
        for comp in result_chain.chain:
            if not isinstance(comp, Plain):
                cleaned_components.append(comp)
                continue

            start = max(body_start - text_offset, 0)
            end = min(body_end - text_offset, len(comp.text))
            text_offset += len(comp.text)
            if start < end:
                comp.text = comp.text[start:end]
                cleaned_components.append(comp)
        result_chain.chain[:] = cleaned_components

    @filter.on_agent_done(priority=100)
    async def on_agent_done(
        self,
        event: AstrMessageEvent,
        run_context: Any,
        response: Any,
    ):
        """清理内部会话历史并恢复平台元数据，不覆盖模型响应。"""
        try:
            if self._should_handle_event(event):
                self._strip_mode_tags_from_history(run_context)
        finally:
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

        mode, cleaned = self._extract_mode(body, include_trailing=False)
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
        pending = f"{pending_prefix}{text_buffer}"
        return self._extract_trailing_mode(pending)[1].strip()

    async def _process_stream_locally(
        self,
        event: AstrMessageEvent,
        generator: AsyncGenerator[MessageChain, None],
    ) -> None:
        mode: str | None = None
        mode_from_tag = False
        prefix_buffer = ""
        text_buffer = ""
        short_chunks_sent = 0
        discarding_thinking = False
        thinking_close_tail = ""
        seen_tool_start_count = self._get_tool_start_count(event)

        async def flush_tool_boundary_buffer(log_message: str) -> None:
            nonlocal mode, mode_from_tag, prefix_buffer, text_buffer, short_chunks_sent
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
            mode_from_tag = False
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
                            response_mode = self._consume_response_mode(event)
                            mode = response_mode or self._default_mode(prefix_buffer)
                            mode_from_tag = response_mode is not None
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
                        mode_from_tag = (
                            self._extract_mode(prefix_buffer, include_trailing=False)[0]
                            is not None
                        )
                        # 非流式回退也可能在响应钩子之后转成流式片段，标签已被清理。
                        response_mode = self._consume_response_mode(event)
                        if not mode_from_tag and response_mode is not None:
                            mode = response_mode
                            mode_from_tag = True
                        if mode_from_tag:
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
                response_mode = self._consume_response_mode(event)
                mode_from_tag = mode_candidate is not None or response_mode is not None
                mode = mode_candidate or response_mode or self.default_mode_no_tag
            elif prefix_buffer:
                text_buffer += prefix_buffer

            trailing_mode, text_buffer = self._extract_trailing_mode(text_buffer)
            # 开头没有标签且尚未分段发送时，末尾标签仍可决定本轮剩余内容的发送方式。
            if trailing_mode is not None and not mode_from_tag and not short_chunks_sent:
                logger.info(
                    "streamchunk: 从回复末尾提取到响应模式: [%s]", trailing_mode.upper()
                )
                mode = trailing_mode

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
            event.set_extra(self.LLM_RESPONSE_MODE_KEY, None)
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
            if result.result_content_type == ResultContentType.STREAMING_FINISH:
                event.set_extra(self.LLM_RESPONSE_MODE_KEY, None)
            return

        response_mode = None
        if result.is_model_result():
            response_mode = self._consume_response_mode(event)

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
        mode = mode or response_mode
        if mode is None:
            mode = self._default_mode(text)
            logger.info("streamchunk: 未提取到标签，使用回退模式: [%s]", mode.upper())
        else:
            logger.info("streamchunk: 使用模型标签指定的响应模式: [%s]", mode.upper())

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
