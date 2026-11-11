"""适配器层：把统一请求翻译成各协议原生请求，并把原生响应翻译回统一结构。

协议实现：
  - OpenAIResponsesAdapter   POST {base}/responses      Authorization: Bearer
  - OpenAIChatAdapter        POST {base}/chat/completions（OpenAI 兼容协议的兜底选项）
  - AnthropicMessagesAdapter POST {base}/v1/messages    x-api-key + anthropic-version
"""
