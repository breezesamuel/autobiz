# 部署指南：让 AutoBiz 关机也运转（24/7）

按你手边的资源选一层即可，层数越高越“无人值守”。

## 方案 A：本机 24/7（已安装 ✅）

两个常驻进程 + 开机自启：

| 组件 | 状态 | 说明 |
|---|---|---|
| daemon 抓取循环 | 已在后台运行 | `python run.py daemon --interval 3600`，带单实例锁 |
| 仪表盘 | http://localhost:8787 | `python run.py dashboard` |
| 开机自启 | 已装 | 启动文件夹 `AutoBiz-autostart.cmd`，登录即拉起两个进程 |

- 查看自启项：`Win+R` → `shell:startup`
- 卸载自启：删除启动文件夹里的 `AutoBiz-autostart.cmd`
- 电脑睡眠会导致暂停：设置 → 电源 → 从不睡眠（推荐插电常开）

## 方案 B：Docker（任意常驻主机：家里 NAS / 云主机）

```bash
docker compose -f deploy/docker-compose.yml up -d --build
```

两个容器：`autobiz-daemon`（抓取循环）+ `autobiz-dashboard`（:8787 看板），
`restart: unless-stopped` 崩溃自动拉起，主机重启自动恢复。

## 方案 C：GitHub Actions 云端免费跑（电脑关机照样转）⭐

仓库：`f:\2026\diqi\.github\workflows\autobiz.yml`，已配置每 2 小时跑一轮
全流水线并把最新线索数据 + 经济价值报告提交回仓库。

1. 安装 git 与 gh：`winget install Git.Git GitHub.cli`
2. 初始化并推送：
   ```powershell
   cd f:\2026\diqi
   git init
   git add -A
   git commit -m "AutoBiz: autonomous lead-gen + payments platform"
   gh repo create autobiz --private --source=. --push
   ```
3. （可选，推荐）仓库 → Settings → Secrets and variables → Actions，添加：
   `PAYPAL_CLIENT_ID` / `PAYPAL_CLIENT_SECRET` / `MOLTSPAY_API_KEY` / `MOLTSPAY_SECRET`
   （值在本地 `.env` 里，**绝不提交 .env**）→ 云端报告即带真实收款状态
4. Actions 页看到 `AutoBiz cloud pipeline` 每 2 小时自动执行；
   手动触发：workflow_dispatch（可填每轮提案数）

## 方案 D：VPS 免费层（Oracle Cloud / 阿里云试用）

```bash
git clone <你的仓库> && cd autobiz
pip install -r requirements.txt
cp .env.example .env   # 填入密钥
nohup python run.py daemon --interval 3600 > autobiz.log 2>&1 &
python run.py dashboard   # 或用 nginx 反代 8787
```

systemd 守护示例（`/etc/systemd/system/autobiz.service`）：

```ini
[Service]
WorkingDirectory=/opt/autobiz
ExecStart=/usr/bin/python3 run.py daemon --interval 3600
Restart=always
[Install]
WantedBy=multi-user.target
```

## 健康检查（任一方案通用）

```powershell
python run.py status        # 数据库统计
python run.py pay-test      # 两条收款通道实测
Invoke-RestMethod http://localhost:8787/api/summary
```
