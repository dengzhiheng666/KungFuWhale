
# -*- coding: utf-8 -*-
"""
Raw DeepSeek call template (OpenAI-compatible interface).
Configuration comes uniformly from config.py.
"""
from openai import OpenAI

import config

client = OpenAI(
    base_url=config.DEEPSEEK_BASE_URL,
    api_key=config.DEEPSEEK_API_KEY,
    timeout=config.REQUEST_TIMEOUT,
)

response = client.chat.completions.create(
    model=config.DEEPSEEK_MODEL,
    messages=[{"role": "user", "content": "Explain recursion with a short example."}],
    temperature=0.7,
    top_p=0.8,
    max_tokens=config.DEEPSEEK_MAX_TOKENS,
    # Example: enable thinking mode with the highest reasoning effort (all abilities on).
    extra_body={"thinking": {"type": "enabled"}},
    reasoning_effort=config.DEEPSEEK_REASONING_EFFORT,
    stream=True,
)
for chunk in response:
    if chunk.choices:
        delta = chunk.choices[0].delta
        reasoning = getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None)
        if reasoning:
            print("\n[thinking] " + reasoning, end="")
        if getattr(delta, "content", None):
            print(delta.content or "", end="")
