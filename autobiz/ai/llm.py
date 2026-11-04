"""OpenAI 兼容 LLM 客户端：GitHub Models / 智谱 / Ollama / Agnes AI 等任意兼容网关。"""
import shutil
import subprocess

import requests

from ..config import env

_GH_TOKEN_CACHE: dict = {}


def _api_key() -> str:
    key = env("LLM_API_KEY")
    if key:
        return key
    # 未写死密钥时动态读取本机 gh CLI 登录态（token 由 gh 自动续期）
    if "tok" not in _GH_TOKEN_CACHE:
        exe = shutil.which("gh") or r"C:\Program Files\GitHub CLI\gh.exe"
        try:
            _GH_TOKEN_CACHE["tok"] = subprocess.run(
                [exe, "auth", "token"], capture_output=True, text=True, timeout=10
            ).stdout.strip()
        except Exception:
            _GH_TOKEN_CACHE["tok"] = ""
    return _GH_TOKEN_CACHE["tok"]


def available() -> bool:
    return bool(env("LLM_BASE_URL")) and bool(_api_key())


def chat(system: str, user: str, max_tokens: int = 700,
         temperature: float = 0.7) -> str:
    if not available():
        raise RuntimeError("LLM 未配置（.env 缺 LLM_BASE_URL / LLM_API_KEY）")
    base = env("LLM_BASE_URL").rstrip("/")
    model = env("LLM_MODEL") or "gpt-4o-mini"
    headers = {"Authorization": f"Bearer {_api_key()}",
               "Content-Type": "application/json"}
    if "models.github.ai" in base:
        headers["X-API-Version"] = "2025-04-01"
    if "githubcopilot.com" in base:
        headers["Editor-Version"] = "copilot-chat/0.0.0"
        headers["Copilot-Integration-Id"] = "copilot-chat"
    r = requests.post(
        f"{base}/chat/completions",
        headers=headers,
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
