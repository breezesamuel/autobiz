# Windows 下启动 Dashboard

## 三种方式

### 方式 A: Runner 脚本（推荐）

仓库里已经有 `tools/_dash_runner.py`，自带 `sys.path` 修复，**必须用这个**——直接 `python -m autobiz.dashboard` 不起作用（模块没 `__main__`）。

```powershell
# 后台启动 (PowerShell 5)
Start-Process python -ArgumentList "tools/_dash_runner.py" `
    -WorkingDirectory "f:\2026\diqi" -WindowStyle Hidden

# 前台启动 (看输出)
cd "f:\2026\diqi"; python tools/_dash_runner.py
```

Runner 脚本内容：
```python
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from autobiz.dashboard import serve
serve()
```

### 方式 B: 直接 Python 调用

```powershell
python -c "from autobiz.dashboard import serve; serve()"
```

缺点：无法用 PowerShell 5 的 `-ArgumentList` 传多行 `-c`，必须写单行。

### 方式 C: Start-Job（PowerShell 7+）

```powershell
$job = Start-Job -ScriptBlock {
    Set-Location "f:\2026\diqi"
    python tools/_dash_runner.py
}
Start-Sleep 10
Receive-Job $job -Keep
```

## 端口

`config.json` 里 `dashboard_port: 8787`，不是 8765。改了就全对不上。

## 健康检查

```powershell
# Python
python -c "import urllib.request; print(urllib.request.urlopen('http://localhost:8787/api/health').read()[:200])"

# PowerShell
Invoke-WebRequest http://localhost:8787/api/health -UseBasicParsing
```

## 杀进程 + 重启

```powershell
# 杀所有 python
Get-Process python | Stop-Process -Force

# 等端口释放
netstat -ano | findstr ":8787"
```

## 注意事项

1. PowerShell 5 **不支持** `&` 后台语法——别用。用 `Start-Process`。
2. `Start-Process` 找不到模块 `autobiz`——因为子进程 cwd 和 sys.path 不对——所以 `_dash_runner.py` 里有 `sys.path.insert(0, ...)`。
3. `market_data` warmup 可能因为外部 API 不通失败——这是非致命的，dashboard 照样起来。
4. 端口被占：换个没被用的，改 `config.json` 或 `serve(8788)`。
