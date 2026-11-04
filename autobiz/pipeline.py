"""主流水线：抓取 → 评分 → 短名单 → 提案草稿 → 报告；支持守护循环。"""
import time
from datetime import datetime, timezone

from . import db
from .config import CFG, DB_PATH


def phase_crawl(sources: list = None) -> dict:
    from .crawlers import crawl_all
    print(f"[{_ts()}] [crawl] 开始抓取 {len(sources or CFG['crawl']['sources'])} 个渠道 ...")
    leads, errors, new = crawl_all(sources)
    db.add_run("crawl", len(leads), new,
               [f"{k}: {v}" for k, v in errors.items()])
    print(f"[crawl] 获取 {len(leads)} 条，新增 {new} 条，"
          f"异常渠道 {len(errors)} 个: {', '.join(errors) or '无'}")
    return {"found": len(leads), "new": new, "errors": errors}


def phase_score() -> dict:
    from .ai.scorer import score_all
    st = score_all()
    print(f"[score] 评分 {st['scored']} 条，新进短名单 {st['new_shortlisted']} 条，"
          f"排除 {st['excluded']} 条")
    return st


def phase_propose(limit: int = 10) -> dict:
    from .ai.proposal import generate
    cands = db.leads_by_status("shortlisted")
    generated = 0
    for lead in cands:
        if generated >= limit:
            break
        if db.proposals_for(lead["id"]):
            continue
        content, method = generate(lead)
        db.add_proposal(lead["id"], content, method)
        db.set_status(lead["id"], "proposed")
        generated += 1
        print(f"  [提案] {lead['title'][:60]}  ({method})")
    print(f"[propose] 生成提案草稿 {generated} 份（待人工发送）")
    return {"generated": generated, "candidates": len(cands)}


def phase_report() -> str:
    from .report import generate_report
    path = generate_report()
    print(f"[report] 经济价值报告已生成: {path}")
    return str(path)


def run_all(sources: list = None, propose_limit: int = 10) -> dict:
    t0 = time.time()
    crawl = phase_crawl(sources)
    score = phase_score()
    propose = phase_propose(propose_limit)
    report = phase_report()
    return {"crawl": crawl, "score": score, "propose": propose,
            "report": report, "elapsed_sec": round(time.time() - t0, 1)}


def _acquire_lock(interval: int) -> "object":
    """单实例锁：锁文件 mtime 超过 2 个周期视为僵尸，可接管。"""
    import os
    lock = DB_PATH.parent / "daemon.lock"
    if lock.exists():
        age = time.time() - lock.stat().st_mtime
        if age < interval * 2:
            print(f"[daemon] 已有实例在运行（锁 {int(age)}s 前刷新），退出")
            return None
        print(f"[daemon] 接管僵尸锁（{int(age)}s 未刷新）")
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text(str(os.getpid()), encoding="utf-8")
    return lock


def daemon(interval: int = None, rounds: int = None) -> None:
    """24/7 守护循环：到点自动抓取+评分+提案+刷新报告。"""
    interval = interval or CFG["crawl"]["interval_seconds"]
    lock = _acquire_lock(interval)
    if lock is None:
        return
    print(f"[daemon] 启动，周期 {interval}s（Ctrl+C 退出）")
    i = 0
    try:
        while rounds is None or i < rounds:
            i += 1
            try:
                print(f"\n=== 第 {i} 轮 {_ts()} ===")
                run_all()
            except KeyboardInterrupt:
                print("\n[daemon] 手动停止")
                break
            except Exception as e:
                print(f"[daemon] 本轮异常（继续下一轮）: {e}")
                db.add_run("daemon", 0, 0, [str(e)[:300]])
            lock.write_text(str(__import__("os").getpid()), encoding="utf-8")
            try:
                time.sleep(interval)
            except KeyboardInterrupt:
                print("\n[daemon] 手动停止")
                break
    finally:
        try:
            lock.unlink()
        except Exception:
            pass
    print("[daemon] 已退出")


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%SZ")
