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

### 0. 前置条件：安装 Python 3

监控后端用 Python 编写，**电脑需先装好 Python 3**（启动器找不到 Python 会明确提示）。按你的系统装一个即可：

- **Windows**：到 [python.org](https://www.python.org/downloads/windows/) 下载最新 3.x 安装包，**安装时务必勾选 `Add python.exe to PATH`**（最关键的勾，勾了才能直接 `python` 命令）。装完打开「命令提示符」验证：
  ```bat
  python --version
  ```
  能显示版本号即成功。
- **macOS**：系统常自带 Python 3；若没有，装 [Homebrew](https://brew.sh/) 后执行 `brew install python`，或到 python.org 下载 macOS 安装包。验证：
  ```bash
  python3 --version
  ```
- **Linux（Debian/Ubuntu）**：
  ```bash
  sudo apt update && sudo apt install -y python3 python3-pip
  python3 --version
  ```

> 只要装了 Python 3，后续依赖（requests / schedule）和启动命令都交给启动器自动处理，无需你手动装。

### 1. 安装依赖（开箱即用，一般无需手动）

监控脚本 `stock_monitor.py` 依赖 `requests` 与 `schedule`（见 `monitor/requirements.txt`）。
**启动器会自动检测并在缺失时安装**，所以绝大多数情况下你什么都不用做——点「启动监控」、粘贴命令即可。

仅在想手动预装或自动安装失败时运行：

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

### 4. 运行（在页面里点「启动监控」即可）

最推荐的方式：**直接在牛股计算器页面操作，无需记任何命令**。

1. 用浏览器打开看板（见上方「快速开始」），进入 **牛股计算器** 页面（默认首页）；
2. 在页面中找到并点击 **「启动监控」** 按钮；
3. 页面会自动按当前系统复制对应的启动命令，并弹出提示：
   - Windows → 复制 `powershell -ExecutionPolicy Bypass -File "...\monitor\start_monitor.ps1"`
   - macOS / Linux → 复制 `bash ".../monitor/start_monitor.sh"`
4. 打开 **命令行**（Windows 用「命令提示符 cmd」，macOS/Linux 用「终端 Terminal」），**右键粘贴**刚才复制的命令，按 **回车**；
5. 脚本会自动检测并安装依赖（首次需联网），随后进入监控循环。看到 `股价监控系统` 相关输出即表示运行成功。

> 若复制失败，页面会弹出命令框，手动选中复制即可。
> 依赖缺失时脚本会自己 `pip install`，**小白无需手动装任何东西**。

如果本机有多个 Python，启动器会自动挑选：

1. 环境变量 `WB_PYTHON`（显式指定，最高优先级）
2. **WorkBuddy 内置 Python**（`~/.workbuddy/binaries/python/versions/<最新版本>/python`，已含 `requests`/`schedule`，开箱即用）
3. PATH 中的 `python3` / `python`

**备选：手动在终端运行**（不通过页面按钮）

```bash
# Windows 启动器（会注册交易日 9:15 唤醒任务，首次需管理员权限）
powershell -ExecutionPolicy Bypass -File monitor/start_monitor.ps1

# macOS / Linux 启动器
bash monitor/start_monitor.sh

# 任意系统，最简直接运行（无需唤醒任务）
cd monitor && python stock_monitor.py        # 或 python3 stock_monitor.py
```

> Windows 的「交易日 9:15 唤醒任务」仅 Windows 生效（用系统计划任务实现）；macOS/Linux 用 `start_monitor.sh` 在前台运行即可，如需开机自启可自行用 `launchd` / `cron` 包装。

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
