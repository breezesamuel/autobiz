"""OpenAI 兼容 LLM 客户端：GitHub Models / 智谱 / Ollama / Agnes AI 等任意兼容网关。"""
import shutil
import subprocess
import time

import requests

from ..config import ROOT, env

_GH_TOKEN_CACHE: dict = {}


def _api_key() -> str:
    key = env("LLM_API_KEY")
    if key:
        return key
    # 未写死密钥时动态读取本机 gh CLI 登录态（token 由 gh 自动续期）
    # gh 偶发挂起：仅缓存成功结果，失败下次重试，避免一次超时拖垮整个进程
    if not _GH_TOKEN_CACHE.get("tok"):
        cache = ROOT / "data" / ".gh_token"
        try:
            if cache.exists() and time.time() - cache.stat().st_mtime < 8 * 3600:
                _GH_TOKEN_CACHE["tok"] = cache.read_text(encoding="utf-8").strip()
        except Exception:
            pass
    if not _GH_TOKEN_CACHE.get("tok"):
        exe = shutil.which("gh") or r"C:\Program Files\GitHub CLI\gh.exe"
        for _ in range(3):
            try:
                tok = subprocess.run(
                    [exe, "auth", "token"], capture_output=True, text=True,
                    timeout=15,
                ).stdout.strip()
            except Exception:
                tok = ""
            if tok:
                _GH_TOKEN_CACHE["tok"] = tok
                try:
                    cache.parent.mkdir(parents=True, exist_ok=True)
                    cache.write_text(tok, encoding="utf-8")
                except Exception:
                    pass
                break
    return _GH_TOKEN_CACHE.get("tok", "")


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
