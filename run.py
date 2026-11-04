#!/usr/bin/env python3
"""AutoBiz 命令行入口。

用法：
  python run.py init          初始化数据库 + 数字产品货架
  python run.py crawl         抓一轮线索
  python run.py score         评分 + 短名单
  python run.py propose       生成提案草稿
  python run.py report        生成经济价值报告
  python run.py all           crawl+score+propose+report 一次跑完
  python run.py daemon        24/7 守护循环（--interval 秒 --rounds 轮）
  python run.py dashboard     启动本地控制台（--port 端口）
  python run.py status        打印系统状态摘要
  python run.py pay-test      实测 PayPal / MoltsPay 收款通道
  python run.py pay-link 99   生成 99 USD 的 PayPal 收款链接
  python run.py record-revenue 99 --channel paypal --note "客户A"
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def cmd_init(_args):
    from autobiz import db
    db.get_conn()
    from autobiz.payment.moltspay import MoltsPay
    p = MoltsPay().write_manifest()
    print(f"[init] 数据库就绪: {db.DB_PATH}")
    print(f"[init] 数字产品货架: {p}")


def cmd_crawl(args):
    from autobiz import pipeline
    src = args.sources.split(",") if args.sources else None
    pipeline.phase_crawl(src)


def cmd_score(_args):
    from autobiz import pipeline
    pipeline.phase_score()


def cmd_propose(args):
    from autobiz import pipeline
    pipeline.phase_propose(args.limit)


def cmd_report(_args):
    from autobiz import pipeline
    pipeline.phase_report()


def cmd_all(args):
    from autobiz import pipeline
    src = args.sources.split(",") if args.sources else None
    result = pipeline.run_all(src, args.limit)
    print("\n[all] 完成:", json.dumps(
        {k: v for k, v in result.items() if k != "report"},
        ensure_ascii=False))


def cmd_daemon(args):
    from autobiz import pipeline
    pipeline.daemon(args.interval, args.rounds)


def cmd_dashboard(args):
    from autobiz import dashboard
    dashboard.serve(args.port)


def cmd_status(_args):
    from autobiz import db
    s = db.summary()
    print(json.dumps(s, ensure_ascii=False, indent=2))


def cmd_pay_test(_args):
    from autobiz.payment.moltspay import MoltsPay
    from autobiz.payment.paypal import PayPal
    print("PayPal :", json.dumps(PayPal().health(), ensure_ascii=False, indent=2))
    print("MoltsPay:", json.dumps(MoltsPay().status(), ensure_ascii=False, indent=2))


def cmd_pay_link(args):
    from autobiz.payment.paypal import PayPal
    result = PayPal().charge(
        args.amount, args.desc, args.currency, args.email,
        args.reference or f"autobiz-{int(__import__('time').time())}")
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_record_revenue(args):
    from autobiz import db
    rid = db.add_revenue(args.amount, args.channel, args.note,
                         args.currency, args.status)
    print(f"[revenue] 入账记录 id={rid} {args.currency} {args.amount} "
          f"({args.channel}) {args.note}")


def main():
    p = argparse.ArgumentParser(description="AutoBiz 自动赚钱系统")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init").set_defaults(fn=cmd_init)

    c = sub.add_parser("crawl")
    c.add_argument("--sources", default="", help="逗号分隔的渠道名")
    c.set_defaults(fn=cmd_crawl)

    sub.add_parser("score").set_defaults(fn=cmd_score)

    pr = sub.add_parser("propose")
    pr.add_argument("--limit", type=int, default=10)
    pr.set_defaults(fn=cmd_propose)

    sub.add_parser("report").set_defaults(fn=cmd_report)

    a = sub.add_parser("all")
    a.add_argument("--sources", default="")
    a.add_argument("--limit", type=int, default=10)
    a.set_defaults(fn=cmd_all)

    d = sub.add_parser("daemon")
    d.add_argument("--interval", type=int, default=None)
    d.add_argument("--rounds", type=int, default=None)
    d.set_defaults(fn=cmd_daemon)

    dash = sub.add_parser("dashboard")
    dash.add_argument("--port", type=int, default=None)
    dash.set_defaults(fn=cmd_dashboard)

    sub.add_parser("status").set_defaults(fn=cmd_status)
    sub.add_parser("pay-test").set_defaults(fn=cmd_pay_test)

    pl = sub.add_parser("pay-link")
    pl.add_argument("amount", type=float)
    pl.add_argument("--desc", default="AutoBiz service")
    pl.add_argument("--currency", default="USD")
    pl.add_argument("--email", default="", help="客户邮箱（发票轨道用）")
    pl.add_argument("--reference", default="")
    pl.set_defaults(fn=cmd_pay_link)

    rv = sub.add_parser("record-revenue")
    rv.add_argument("amount", type=float)
    rv.add_argument("--channel", default="manual")
    rv.add_argument("--note", default="")
    rv.add_argument("--currency", default="USD")
    rv.add_argument("--status", default="confirmed")
    rv.set_defaults(fn=cmd_record_revenue)

    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
