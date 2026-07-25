# Intraday Monitor · 盘中监控台

一个**本地优先**的 A 股盘中监控聚合台。

- `index.html` 作为侧边栏外壳，用 iframe 聚合多个**自包含**的看板页面；
- 看板行情来自公开接口，**无需 API Key 即可直接打开使用**；
- 可选 `monitor/` Python 后端，对自选股做日内压力/支撑突破监控，并通过企业微信推送告警。

---

## 目录结构

```
intraday-monitor/
├── index.html                      # 聚合外壳（侧边栏 + iframe 容器）
├── config.js                       # 页面导航配置（指向 pages/ 下看板）
├── pages/
│   ├── sector_flow_dashboard.html  # 板块资金流看板
│   └── bull_calc.html              # 牛股计算器看板
├── monitor/                        # 可选：Python 监控后端
│   ├── stock_monitor.py            # 监控主程序（突破告警 + 企业微信推送）
│   ├── start_monitor.ps1           # Windows 启动器（注册交易日 9:15 唤醒任务）
│   └── start_monitor.sh            # macOS / Linux 启动器
│   ├── requirements.txt            # Python 依赖
│   ├── .env.example                # 企业微信 Webhook 配置模板
│   └── watchlist.example.csv       # 自选股列表模板
├── .gitignore
├── LICENSE                         # MIT
└── README.md
```

---

## 快速开始（仅看板，无需后端）

**方式一：本地起服务（推荐）**

```bash
cd intraday-monitor
python -m http.server 8000
# 浏览器打开 http://localhost:8000/
```

> 推荐用本地服务方式：部分浏览器对 `file://` 下的 iframe 有同源限制，起一个静态服务最稳妥。

**方式二：直接打开**

直接双击 `index.html` 用浏览器打开也可，但个别浏览器会限制 `file://` iframe 加载。

左侧导航切换页面；牛股计算器页面可在线增删自选、刷新行情、计算成本/盈亏，并支持导出 `watchlist.csv`。

---

## 监控后端（可选）

仅当你需要「突破自动推送企业微信」时才需要部署。

### 1. 安装依赖

```bash
python -m pip install -r monitor/requirements.txt
```

> 若提示 `pip` 不是命令，请用上面 `python -m pip` 的写法（Windows 上 `pip` 常未加入 PATH）。
> 想装到指定 Python，可设 `WB_PYTHON` 后执行：`"$env:WB_PYTHON" -m pip install -r monitor/requirements.txt`

### 2. 配置企业微信推送（可选）

```bash
cp monitor/.env.example monitor/.env
# 编辑 monitor/.env，填入 WECHAT_WEBHOOK_URL
```

获取方式：企业微信群 → 添加群机器人 → 复制 Webhook 地址。

> **未配置 Webhook 也能正常运行**，只是不会推送消息（仅本地日志）。密钥走环境变量，`.env` 已被 `.gitignore` 忽略，**绝不入库**。

### 3. 准备自选股列表

- 从牛股计算器页面点击「导出」得到 `watchlist.csv`，放到 `monitor/` 目录；或
- 复制模板：`cp monitor/watchlist.example.csv monitor/watchlist.csv` 后按格式填写。

> `watchlist.csv` 含个人持仓/自选，已被 `.gitignore` 忽略，**请勿提交真实数据**。

### 4. 运行

三种方式任选其一，**全平台通用的是方式 A**：

```bash
# 方式 A：直接运行（Windows / macOS / Linux 通用，最简单）
cd monitor && python stock_monitor.py        # 或 python3 stock_monitor.py

# 方式 B：Windows 启动器（会注册交易日 9:15 唤醒任务，首次需管理员权限）
powershell -ExecutionPolicy Bypass -File monitor/start_monitor.ps1

# 方式 C：macOS / Linux 启动器
bash monitor/start_monitor.sh
```

如果本机有多个 Python，启动器会自动挑选：

1. 环境变量 `WB_PYTHON`（显式指定，最高优先级）
2. **WorkBuddy 内置 Python**（`~/.workbuddy/binaries/python/versions/<最新版本>/python`，已含 `requests`/`schedule`，开箱即用）
3. PATH 中的 `python3` / `python`

> Windows 的「交易日 9:15 唤醒任务」仅 Windows 生效（用系统计划任务实现）；macOS/Linux 用 `start_monitor.sh` 在前台运行即可，如需开机自启可自行用 `launchd` / `cron` 包装。
> 在牛股计算器页面点「启动监控」按钮时，会按当前系统自动复制对应的启动命令（Windows→`.ps1`，macOS/Linux→`.sh`）。

---

## 数据来源

- 板块资金流：`push2delay.eastmoney.com`（东方财富公开行情接口）
- 个股行情：`qt.gtimg.cn`（腾讯财经公开接口）

均为公开接口，本项目不存储、不中转你的任何数据。

---

## 安全与隐私

- 企业微信 Webhook 地址通过环境变量 `WECHAT_WEBHOOK_URL` 读取，配置在 `monitor/.env`（已被忽略），**绝不硬编码入库**。
- 自选股 `monitor/watchlist.csv` 含个人数据，已被 `.gitignore` 忽略。
- 所有脚本与看板均为本地运行，不存在远端收集。

---

## 许可证

[MIT](./LICENSE) © 2026 The Intraday Monitor Authors
