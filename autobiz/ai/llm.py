"""OpenAI 兼容 LLM 客户端：支持智谱 / Ollama / Agnes AI 等任意兼容网关。"""
import requests

from ..config import env


def available() -> bool:
    return bool(env("LLM_BASE_URL") and env("LLM_API_KEY"))


def chat(system: str, user: str, max_tokens: int = 700,
         temperature: float = 0.7) -> str:
    if not available():
        raise RuntimeError("LLM 未配置（.env 缺 LLM_BASE_URL / LLM_API_KEY）")
    base = env("LLM_BASE_URL").rstrip("/")
    model = env("LLM_MODEL") or "gpt-4o-mini"
    r = requests.post(
        f"{base}/chat/completions",
        headers={"Authorization": f"Bearer {env('LLM_API_KEY')}",
                 "Content-Type": "application/json"},
        json={
            "model": model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "max_tokens": max_tokens,
            "temperature": temperature,
        },
        timeout=90,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"].strip()
