# AutoBiz — 全自动自运营赚钱系统平台

> 抓需求 → 评分筛选 → AI 生成提案 → 收款 → 24/7 自运转，全链路一个文件夹交付。

## 24 小时价值速览（数字全部来自本机实测库，可复核）

| 维度 | 实测结果 |
|---|---|
| 需求线索 | `python run.py status` 查看当前累计（首轮即 389 条，带可点击原始 URL） |
| 需求渠道 | 13 个已注册，8 个免授权直抓：GitHub Bounty / HN / StackOverflow / RemoteOK / Remotive / Himalayas / Jobicy / Greenhouse（18 家公司招聘板） |
| 预算总值 | 报告内 `budget_sum_usd`（年薪已折算、多币种已换汇，不虚高） |
| AI 文本处理 | 100 分制评分引擎 + 提案生成（模板双风格：接单/求职；LLM 接口即插即用） |
| 收款通道 | **PayPal 实测打通（live）**：OAuth ✅、发票 API ✅、Payment Link 403 自动降级；MoltsPay x402 已配置（CDP 凭据格式正确、服务目录连通、产品货架就绪） |
| 自运转 | 本机后台 daemon + 启动文件夹自启 + Docker + GitHub Actions 云端 cron（关机也跑） |
| 经济价值模型 | 见 [docs/ECONOMIC_VALUE_REPORT.md](docs/ECONOMIC_VALUE_REPORT.md)（三档情景 + 千万目标放大量与杠杆清单） |

## 快速开始

```powershell
python run.py init        # 初始化数据库 + 数字产品货架
python run.py all         # 抓取→评分→提案→报告 一条龙
python run.py daemon      # 24/7 守护循环（--interval 3600）
python run.py dashboard   # 打开 http://localhost:8787 控制台
python run.py pay-test    # 实测两条收款通道
python run.py pay-link 99 --desc "scraper delivery" --email client@x.com   # 开 $99 收款
```

## 系统架构

```
run.py  (CLI：crawl / score / propose / report / all / daemon / dashboard / pay-*)
autobiz/
├── config.py            配置加载（config.json + .env 密钥，.env 永不入 git）
├── db.py                SQLite：leads / proposals / runs / revenue
├── http.py              限速重试的共享 HTTP 客户端
├── crawlers/            13 渠道
│   ├── dev_sources.py   GitHub Bounty、HN、StackOverflow、Reddit
│   ├── job_boards.py    RemoteOK、Arbeitnow、Remotive、Himalayas、Jobicy
│   ├── ats.py           Greenhouse 18 家公司招聘板（替代被 CF 拦截的 arbeitnow）
│   └── marketplaces.py  Upwork（官方 API 待授权）、Fiverr、Contra
├── ai/
│   ├── scorer.py        预算35+关键词30+新鲜度15+渠道15+详情5 = 100 分制
│   ├── proposal.py      接单/求职双模板 + LLM 定制（智谱/Ollama/任意兼容网关）
│   └── llm.py           OpenAI 兼容客户端
├── payment/
│   ├── paypal.py        发票主轨道 + Payment Link 降级，capability 自动探测
│   └── moltspay.py      x402/CDP：产品货架 manifest + 连通性验证
├── pipeline.py          流水线编排 + 单实例锁守护循环
├── dashboard.py         零依赖 Web 控制台（KPI/线索/提案/收款）
└── report.py            经济价值报告（实测数据 + 收入模型 + 千万路径）
```

## 渠道状态（如实记录，无伪造）

| 渠道 | 状态 | 备注 |
|---|---|---|
| github_bounty / hn / stackoverflow / remoteok / remotive / himalayas / jobicy / greenhouse | ✅ 直抓 | 免注册公开 API |
| reddit | ⚠️ 待授权 | 2026-05 起未认证 JSON 403，需官方 OAuth |
| arbeitnow | ⚠️ 已停用 | Cloudflare JS 盾（requests / curl_cffi 均 403），由 greenhouse 源替补 |
| upwork | ⚠️ 待授权 | developers.upwork.com 申请 token 写入 `.env` 即自动启用 |
| fiverr | ⛔ 无读取 API | 仅 Affiliate 接口，同类需求由其他源覆盖 |

## 收款（实测结论）

- **PayPal 发票轨道**：`pay-link` 一键开票 → 填客户邮箱直发 → 客户在线付款（当前账户的可用主轨道）
- PayPal Payment Links / Orders：403 NOT_AUTHORIZED（应用未开通 Checkout），代码自动降级，不用改任何调用
- **MoltsPay**：USDC/USDT + 支付宝/微信法币；`services/moltspay.services.json` 已生成 4 个标品货架，
  `npx moltspay start` 即可开卖（需在 `.env` 补 `MOLTSPAY_WALLET`）

## 24/7 运行矩阵

| 层 | 方式 | 关机后 |
|---|---|---|
| 本机 | daemon 后台 + `shell:startup` 自启脚本 | 重启登录自动恢复 |
| Docker | `deploy/docker-compose.yml` | 常驻主机重启自动恢复 |
| 云端 | `.github/workflows/autobiz.yml` 每 2 小时 | ✅ 完全不依赖本机 |

详细步骤见 [deploy/DEPLOY.md](deploy/DEPLOY.md)。

## 安全

- 全部密钥只存 `.env`（`.gitignore` 已隔离，`git status` 可复核）
- 收款接口只做“开票/换 token”，无任何自动扣款、无自动对外发送
- 提案只生成草稿，对外发送由人在仪表盘点确认（状态流转可审计）
