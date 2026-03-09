# AstrBot Plugin StreamChunk 智能流式消息分段插件

面向不支持原生流式消息的平台（如 QQ 等）的中间件插件。  
插件在本地接管流式输出，根据 `[SHORT]` / `[LONG]` 标签或回退策略决定“分段发送”或“整段发送”。

## 特性
- 智能策略：可注入提示词，引导模型在回复开头输出 `[SHORT]` 或 `[LONG]`。
- 本地流式：保留模型流式生成，在 AstrBot 本地消费并发送。
- 可配置分段：可自定义分段标点、最小/最大分段长度、发送间隔等。

## 适用场景
建议在不支持 Streaming 的 IM 平台启用；支持原生流式的平台通常无需开启（可通过配置控制）。

## 配置项
以下配置均可在前端管理面板调整：

1. `enabled`：启用/禁用插件。
2. `only_unsupported_platform`：仅在不支持原生流式的平台生效。
3. `local_stream_processing`：本地接管并消费流式输出。
4. `only_model_result`：仅处理模型结果，忽略普通插件等非模型结果。
5. `inject_prompt`：向系统提示词注入标签规则。
6. `default_mode_no_tag`：模型未输出标签时的回退模式，可选 `short` / `long` / `auto`。
7. `auto_short_max_chars`：`auto` 模式的短消息字数阈值。
8. `split_punctuations`：短消息分段触发正则（例如 `[。？！!?；;…\n]`）。
9. `drop_punctuations`：分段后末尾命中该正则时丢弃末尾符号（例如 `[。.]`）。
10. `min_chunk_chars`：最小分段片段长度。达到该长度后，遇到分段标点才会切分。
11. `max_chunk_chars`：最大分段片段长度。达到后强制切分。
12. `segment_interval_seconds`：分段消息之间的发送延迟（秒）。

## 说明
- 标签只在回复开头识别，避免正文中出现 `[SHORT]` / `[LONG]` 时误判。
- 在流式和非流式路径中，短消息分段规则保持一致。
