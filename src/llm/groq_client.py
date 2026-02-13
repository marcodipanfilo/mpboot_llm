from __future__ import annotations

from groq import Groq
from typing import Optional, Dict, Any

class GroqClient:
    def __init__(self, api_key: str, default_model: str = "qwen/qwen3-32b"):
        self.client = Groq(api_key=api_key)
        self.default_model = default_model

    def chat(self, prompt: str, model: Optional[str] = None, **kwargs: Dict[str, Any]) -> str:
        m = model or self.default_model
        resp = self.client.chat.completions.create(
            model=m,
            messages=[{"role": "user", "content": prompt}],
            **kwargs,
        )
        return resp.choices[0].message.content
