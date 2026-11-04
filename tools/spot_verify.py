"""验真：对高分线索做源头核验（GitHub 赏金查仓库/issue 元数据，网页类查 HTTP 状态）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from autobiz import http  # noqa: E402

GITHUB_API = "https://api.github.com"


def verify_github(url: str) -> None:
    # https://github.com/{owner}/{repo}/issues/{n}
    parts = url.rstrip("/").split("/")
    if "issues" not in parts:
        print("  非 issue 链接:", url)
        return
    i = parts.index("issues")
    owner, repo, num = parts[i - 2], parts[i - 1], parts[i + 1]
    r = http.get(f"{GITHUB_API}/repos/{owner}/{repo}", min_interval=6.0,
                 retries=2, headers={"Accept": "application/vnd.github+json"})
    meta = r.json()
    print(f"  仓库 {owner}/{repo}: stars={meta.get('stargazers_count')} "
          f"forks={meta.get('forks_count')} 创建={meta.get('created_at')} "
          f"开放issue={meta.get('open_issues_count')}")
    r2 = http.get(f"{GITHUB_API}/repos/{owner}/{repo}/issues/{num}",
                  min_interval=6.0, retries=2,
                  headers={"Accept": "application/vnd.github+json"})
    iss = r2.json()
    labels = [l["name"] for l in iss.get("labels", [])]
    print(f"  issue #{num}: state={iss.get('state')} comments={iss.get('comments')} "
          f"labels={labels} created={iss.get('created_at')}")
    body = (iss.get("body") or "")[:300].replace("\n", " ")
    print(f"  body 摘要: {body}")


def verify_web(url: str) -> None:
    try:
        r = http.get(url, retries=1, min_interval=1.0, timeout=20)
        print(f"  HTTP {r.status_code}，最终URL={r.url[:100]}，"
              f"长度={len(r.text)}")
    except Exception as e:
        print(f"  抓取失败: {e}")


if __name__ == "__main__":
    target = sys.argv[1]
    print("核验:", target)
    if "github.com" in target:
        verify_github(target)
    else:
        verify_web(target)
