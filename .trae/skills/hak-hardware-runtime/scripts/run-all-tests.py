"""HAK Hardware Runtime — 一键全部自测试.

用法:
  python .trae/skills/hak-hardware-runtime/scripts/run-all-tests.py
  python .trae/skills/hak-hardware-runtime/scripts/run-all-tests.py --skip-e2e

依次运行:
  1. skill_runtime    (120 skills load + 5 live reads)
  2. composer         (Pipeline + Trigger + Composer)
  3. skill_factory     (注册/注销 + 静态写保护)
  4. regex_routes      (dashboard 路由正则单元测试)
  5. e2e smoke test    (需要 dashboard 跑在 :8787)
"""
import subprocess, sys, os, argparse

# 脚本在 .trae/skills/hak-hardware-runtime/scripts/
# 向上 4 级才是项目根
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
os.chdir(ROOT)
# 确保子进程能 import autobiz
os.environ["PYTHONPATH"] = ROOT + os.pathsep + os.environ.get("PYTHONPATH", "")

TESTS = [
    ("skill_runtime",   [sys.executable, "-m", "autobiz.hardware.skill_runtime"]),
    ("composer",        [sys.executable, "-m", "autobiz.hardware.composer"]),
    ("skill_factory",   [sys.executable, "-m", "autobiz.hardware.skill_factory"]),
    ("regex_routes",    [sys.executable, os.path.join(ROOT, "tools", "_test_regex_routes.py")]),
]

E2E = ("e2e_smoke", [sys.executable, os.path.join(ROOT, "tools", "_e2e.py")])

def run(name: str, full_cmd: list[str]) -> bool:
    print(f"\n{'='*55}")
    print(f"  {name}")
    print(f"  {'='*55}")
    result = subprocess.run(full_cmd, cwd=ROOT, env=os.environ)
    ok = result.returncode == 0
    print(f"  → {'✅ PASS' if ok else '❌ FAIL'} ({name})")
    return ok

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-e2e", action="store_true", help="跳过需要 dashboard 启动的 HTTP 端到端测试")
    args = ap.parse_args()

    results = []
    for name, cmd in TESTS:
        results.append(run(name, cmd))

    if not args.skip_e2e:
        results.append(run(*E2E))

    total = len(results)
    passed = sum(results)
    print(f"\n{'='*55}")
    print(f"  总计: {passed}/{total} passed")
    print(f"  {'🎉 全部通过' if all(results) else '⚠️  有失败项'}")
    print(f"{'='*55}")
    sys.exit(0 if all(results) else 1)

if __name__ == "__main__":
    main()
