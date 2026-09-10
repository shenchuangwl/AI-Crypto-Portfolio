# AI Crypto Portfolio

[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
![Python](https://img.shields.io/badge/Python-%E2%89%A53.11-3776AB?logo=python&logoColor=white)
![Node.js](https://img.shields.io/badge/Node.js-%5E20.19%20%7C%7C%20%E2%89%A522.12-339933?logo=node.js&logoColor=white)
![React](https://img.shields.io/badge/React-19-61DAFB?logo=react&logoColor=black)

> Binance USDT-M 永续合约**自动选币与复盘工作台**（内部代号 *Hermes*）。
> 行情采集 → 四道闸门打分 → 滞回状态机分区 → 多板面投影 → 复盘账本 → Web 终端展示，
> 执行侧只提供**纸面（paper）闭环**，不包含真实下单能力。

仓库名：`AI-Crypto-Portfolio` · 展示名：**AI Crypto Portfolio** · 作者：[@shenchuangwl](https://github.com/shenchuangwl)（原项目独立开发者）· 许可证：[Apache-2.0](LICENSE)

---

## ⚠️ 风险提示

- 本项目是**选币研究与可视化工具**，输出的分区、评分、胜率与盈亏统计**不构成任何投资建议**。
- 永续合约交易具有高杠杆、高波动风险，可能在短时间内损失全部保证金。历史复盘结果不代表未来收益。
- 行情数据来自 Binance 公共接口与 CoinGecko，可能存在延迟、缺失或错误；请勿将本项目作为唯一决策依据。
- 本仓库**不含**真实下单代码：`dmr-executor` 只做纸面记录。若你自行接入真实交易，由此产生的一切后果由你自行承担。
- 依 Apache-2.0 第 7、8 条，本项目按“**原样**”（AS IS）提供，不附带任何明示或默示的担保，作者不对使用本项目造成的任何损失承担责任。
- 使用 Binance 行情须遵守 [Binance API Terms](https://www.binance.com/en/terms)，不得转售实时数据流；使用 CoinGecko 数据须遵守其条款与署名要求。

---

## 目录

- [项目简介](#项目简介)
- [实际功能](#实际功能)
- [技术栈](#技术栈)
- [目录结构](#目录结构)
- [系统要求](#系统要求)
- [快速开始](#快速开始)
- [跨平台部署指南](#跨平台部署指南)
- [CoinGecko 配置（必读）](#coingecko-配置必读)
- [环境变量](#环境变量)
- [服务、端口与访问方式](#服务端口与访问方式)
- [开发、构建与测试](#开发构建与测试)
- [部署](#部署)
- [未随仓库发布的数据与文件](#未随仓库发布的数据与文件)
- [验证状态](#验证状态)
- [常见问题](#常见问题)
- [安全提示](#安全提示)
- [贡献方式](#贡献方式)
- [许可证与作者归属](#许可证与作者归属)

---

## 项目简介

AI Crypto Portfolio 面向 Binance **USDT 本位永续合约**（USDT + PERPETUAL + TRADING，`underlyingType ∈ {COIN, 缺失}`，
实测宇宙约 526 个合约），每 15 分钟对全宇宙做一次横截面扫描：

1. **market-ingest** 用 Binance 原生 fapi REST + WebSocket 维护合约宇宙与实时价格；
2. **coin-selection** 依次执行 G1（流动性硬门槛）→ G2（动量/一致性）→ G3（CoinGecko 流通量映射）→ G4（阶梯评分），
   再由**滞回状态机**把币种分到 观察 / 符合 / 确认 / DMR 精选 / 淘汰 等分区；
3. 同一轮取数结果被**投影**到多个参数版本的板面（选币榜 / 选币榜X / 选币榜Y），不重复请求外部接口；
4. 每轮快照旁路写入 SQLite **复盘账本**，统计每一次完整“选入→退出”的已实现盈亏；
5. **api-gateway** 以 REST + SSE 对外提供数据，并托管前端单页应用；
6. **dmr-adapter / dmr-executor** 只消费 `CONFIRMED` 候选，做契约校验与纸面执行记录。

**适用场景**：量化/主观交易者的选币研究、参数版本 A/B 复盘、行情与候选池的可视化监控、选币规则的可回测验证。

**不适用场景**：自动实盘交易、面向公众的行情服务、投资顾问类产品。

## 实际功能

| 页面（前端路由） | 功能 |
|---|---|
| `/terminal`（默认首页） | AICoin 式多维甄选台：漏斗、涨跌热度、状态区、门槛榜、单币 G1→状态机过程 |
| `/quotes` | USDT-M 合约宇宙报价（市值 / 24h / 价格 / 代码） |
| `/screener` | **选币榜**（参数 `param-v1.4.0-staircase-confirm-dmr`）：七个分区、虚拟滚动表、30m/2h/6h 流通市值等级、1Week/1Month 涨跌幅、SSE 实时刷新 |
| `/screener-x` | **选币榜X**（`param-v1.3.0-screener-x`）：主扫描顺带投影的独立板面，候选不可执行 |
| `/screener-y` | **选币榜Y**（`param-v2.0.0-screener-y`）：24h 周期（00:00 UTC 重置分区）、216 组合流通市值主导层、DMR 精选与 OnlyCoin 当日列表；候选不可执行 |
| `/review` | **复盘选币**：历史选入/退出账本，按板面（`?board=main\|x\|y`）与参数版本（`?pv=`）统计胜率、均值、盈亏比 |
| `/markets` | 合约清单（虚拟列表） |
| `/market/:symbol` | 单币 K 线：TradingView Lightweight Charts ↔ KLineChart 可切换，数据来自 Binance fapi |

后端要点（均可在源码中核对）：

- **G1** 6/12/26 日平均成交额硬门槛（默认 300 万美元）；**G3** 通过 CoinGecko 获取流通供应量并处理 `1000SHIB` 等乘数合约（`packages/config/mapping_overrides.json`）。
- **周期流通市值等级 A–F**（`mcap_timeframe.py`）：`流通供应量 × K 线收盘价 / 合约乘数` 的 6/12/26 根均线排列；K 线不足或并列时为 `null`。
- **板面变体注册表** `packages/config/board-variants.json`：参数差异只写在 `overrides` 中，投影失败只记 WARN，不拖垮主扫描。
- **规则身份 manifest**（`packages/config/rule-manifests/`）：按生效边界自动切换 revision，复盘按规则段隔离统计。
- **数据保留策略** `packages/config/retention.json` + `scripts/prune_data.py`（默认 dry-run，保留上限 31 天）。
- **硬约束**：浏览器不直连交易所、不持有任何密钥；生产禁止 `SM_FAST` / `--fast-confirm`；DMR 只吃 `CONFIRMED`；`dmr_executable=false` 的板面走独立 inbox 且不进 `DMR_PARAM_WHITELIST`。

## 技术栈

| 层 | 技术 |
|---|---|
| 后端语言 | Python ≥ 3.11（已在 3.12.3 验证），服务以标准库为主（`http.server`、`sqlite3`、`urllib`） |
| Python 依赖 | `websockets>=12`、`pydantic>=2`、`jsonschema>=4`；测试额外需要 `pytest>=8` |
| 前端 | React 19、React Router 7、TanStack Query / Table / Virtual、Zustand、Day.js |
| 图表 | TradingView Lightweight Charts 5、KLineChart 9、dockview 4（社区版） |
| 构建与检查 | Vite 8、TypeScript ~6.0、oxlint；前端验证闸用 rolldown 打包后以 Node 运行 |
| 存储 | 文件 JSON 快照（热数据）+ SQLite（复盘账本、OnlyCoin 库） |
| 外部服务 | Binance USDT-M fapi（公共行情，无需密钥）、CoinGecko API（流通量/市值，建议自备 key） |
| 契约 | OpenAPI 3.0、JSON Schema、TypeScript 类型、Pydantic v2 模型（`contracts/`） |
| 容器（可选） | Docker Compose，镜像 `python:3.12-slim` |

## 目录结构

```text
AI-Crypto-Portfolio/
├── apps/web/                    # 前端终端（React + Vite + TS）；tests/ 为前端验证闸
├── services/
│   ├── api-gateway/             # 网关：REST + SSE + 托管 apps/web/dist（默认 :18080）
│   │   ├── mock_server.py       #   入口（名称沿用历史，默认读取实时数据）
│   │   ├── review_api.py        #   复盘接口
│   │   └── onlycoin_api.py      #   OnlyCoin 接口
│   ├── market-ingest/           # Binance 原生 REST + WS 行情采集（默认 :18100）
│   ├── coin-selection/          # G1–G4、状态机、板面投影、复盘账本、周期重置
│   │   ├── src/coin_selection/
│   │   └── tests/               #   stdlib runner 单测 + pytest（OnlyCoin）
│   ├── dmr-adapter/             # 候选消息契约校验 / 拒绝规则（不下单）
│   └── dmr-executor/            # 纸面执行器（不下单）
├── contracts/                   # OpenAPI、JSON Schema、TS 类型、Pydantic 模型、样例 JSON
├── packages/config/             # 参数快照 YAML、板面注册表、规则 manifest、216 映射、保留策略
│   └── candidates/              #   待授权的候选配置（生产代码永不自动加载）
├── scripts/                     # 启停、冒烟、验收闸、回放、研究脚本
├── deploy/docker-compose.yml    # 容器编排（market-ingest + gateway，可选 selection-loop）
├── docs/                        # 机制说明、OnlyCoin 源控制接口契约
├── requirements.txt             # 运行依赖
├── requirements-dev.txt         # 开发/测试依赖（含 pytest）
├── .env.example                 # 环境变量模板（仅占位符）
├── LICENSE                      # Apache License 2.0 全文
├── NOTICE                       # 版权声明与第三方组件署名（再分发时须保留）
├── COPYRIGHT.md                 # 版权、作者归属与署名要求说明
└── CONTRIBUTING.md              # 贡献指南
```

## 系统要求

| 项 | 要求 | 说明 |
|---|---|---|
| 操作系统 | Ubuntu 24.04（已实测）、macOS、Windows（WSL2 / Docker Desktop / 原生受限） | 各平台步骤与差异见 [跨平台部署指南](#跨平台部署指南) |
| Python | ≥ 3.11 | `services/market-ingest/pyproject.toml` 声明；已验证 3.12.3 |
| Node.js | `^20.19.0 \|\| >=22.12.0` | Vite 8 的引擎要求；已验证 Node 24.18.0 / npm 12.0.2 |
| 网络 | 可访问 `fapi.binance.com`、`fstream.binance.com`、`api.coingecko.com` | 部分地区访问 Binance 受限，请自行确认合规 |
| 磁盘 | 视保留期而定 | 按默认 31 天保留策略，原作者环境稳态约数 GB；冒烟运行只需几十 MB |
| Docker（可选） | Docker + Compose v2 | 已验证 Docker 29.6.1 / Compose v5.3.1（默认 profile） |

## 快速开始

以下命令除特别说明外，均在**仓库根目录**（`AI-Crypto-Portfolio/`）执行。

### 1. 克隆仓库

```bash
git clone https://github.com/shenchuangwl/AI-Crypto-Portfolio.git
cd AI-Crypto-Portfolio
```

### 2. 安装 Python 依赖

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
# 需要跑测试时改装（包含上面的运行依赖 + pytest）：
.venv/bin/pip install -r requirements-dev.txt
source .venv/bin/activate          # 后续脚本中的 python3 将指向虚拟环境
```

`scripts/daemonize.py` 启动服务时会优先使用仓库内的 `.venv/bin/python`。

### 3. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env，至少填写 COINGECKO_API_KEY（见下文 CoinGecko 专节）
```

`.env` 位于仓库根目录，已被 `.gitignore` 忽略。它会被 **coin-selection 扫描**以及 **`scripts/daemonize.py`（`start-all.sh`）拉起的服务**自动注入（进程中已存在的同名变量优先）；
直接运行 `python services/api-gateway/mock_server.py` 或 `python -m market_ingest` 时**不会**读取 `.env`。

### 4. 构建前端

```bash
cd apps/web
npm ci
npm run build          # 产物输出到 apps/web/dist，由网关托管
cd ../..
```

构建时会出现“chunk 大于 500 kB”的提示，这是警告而非失败。

### 5. 初始化数据（二选一）

仓库不附带任何运行数据，`data/` 下的目录会在首次运行时自动创建。

**A. 隔离冒烟（推荐先跑）**：仅评估 40 个合约，数据写入 `data/smoke/p1/`，不影响正式板面。

```bash
bash scripts/smoke-p1-pipeline.sh
```

流程：`market-ingest --once` → 限量 G1–G4 扫描 → DMR adapter 样例入队 → 纸面执行器。末行输出 `OK p1 pipeline smoke` 即成功。

**B. 正式首轮全量扫描**：对全宇宙运行一次，生成 `data/coin-selection/latest.json` 及选币榜X/Y 投影。
会对 Binance 与 CoinGecko 发起较多请求，耗时取决于网络与限频。

```bash
PYTHONPATH=services/coin-selection/src .venv/bin/python -m coin_selection --force --workers 8
```

### 6. 启动服务

```bash
bash scripts/start-all.sh                                    # market-ingest + gateway（后台守护）
START_SELECTION_LOOP=1 bash scripts/start-all.sh             # 同时启动 15 分钟选币循环
START_SELECTION_LOOP=1 START_VITE=1 bash scripts/start-all.sh  # 再加 Vite 开发服
bash scripts/stop-all.sh                                     # 停止以上全部
```

单独管理某个服务：

```bash
python3 scripts/daemonize.py gateway        start|stop|status
python3 scripts/daemonize.py market-ingest  start|stop|status
python3 scripts/daemonize.py selection-loop start|stop|status
python3 scripts/daemonize.py web-dev        start|stop|status
```

PID 与日志位于 `data/coin-selection/*.pid`、`data/coin-selection/logs/*.log`。

### 7. 访问

浏览器打开 <http://127.0.0.1:18080/terminal>（其他页面见 [服务、端口与访问方式](#服务端口与访问方式)）。
健康检查：`curl http://127.0.0.1:18080/api/v1/health`。尚未产出扫描数据时返回 `"status": "degraded"` 属正常现象。

### 8. 构建复盘账本（可选）

复盘账本由 15 分钟循环旁路增量写入。已有快照时也可手动重建：

```bash
python3 scripts/build_review_ledger.py --reset                # 选币榜（main）
PYTHONPATH=services/coin-selection/src .venv/bin/python scripts/replay_screener_y.py --all   # 用 v2.0.0 参数回放出选币榜Y账本
```

## 跨平台部署指南

本节给出 **Ubuntu、macOS、Windows** 三个平台的完整部署流程。上文“快速开始”是通用摘要，各平台的差异以本节为准。

> **验证范围说明**：Ubuntu 24.04 的流程已实测。macOS 与 Windows 没有真机环境，**未实测**，
> 相关结论来自以下可复现的静态核查：代码可移植性审计、模拟 Windows 缺少 `fcntl` 时的模块导入测试、
> 用 `pip download --only-binary=:all:` 核对各平台的预编译 wheel、npm 锁文件中的原生二进制清单。
> 如在这两个平台上遇到问题，欢迎提交 Issue。

### 平台支持矩阵

| 平台 | 选币扫描 / 15 分钟循环 | 网关 + 前端 | market-ingest | 启停脚本（`*.sh` / `daemonize.py`） | 状态 |
|---|---|---|---|---|---|
| **Ubuntu 24.04**（x86_64） | ✅ | ✅ | ✅ | ✅ | **已实测** |
| Ubuntu 22.04 | ✅（需另装 Python ≥ 3.11） | ✅ | ✅ | ✅ | 未实测 |
| **macOS**（Apple Silicon / Intel） | ✅ | ✅ | ✅ | ✅ | 未实测（代码为 POSIX 实现，依赖均有 wheel） |
| **Windows + WSL2**（推荐） | ✅ | ✅ | ✅ | ✅ | 未实测（在 WSL 中的流程与 Ubuntu 相同） |
| Windows + Docker Desktop | ✅（Linux 容器） | ✅ | ✅ | 由 Compose 代替 | 未实测（Compose 已在 Linux 实测） |
| **Windows 原生** | ❌ 不支持 | ✅ | ✅ | ❌ 不支持 | 未实测（模拟导入测试通过） |

Windows 原生不支持选币扫描的原因：`services/coin-selection/src/coin_selection/scan.py` 在文件顶部 `import fcntl`，用 `fcntl.flock` 给扫描节点加文件锁，而 `fcntl` 只存在于 POSIX 系统。
模拟测试显示，缺少 `fcntl` 时只有 `coin_selection.scan` 无法导入；api-gateway、market-ingest、dmr-adapter、dmr-executor 均可正常导入。
`scripts/*.sh` 依赖 bash；`scripts/daemonize.py` 依赖 POSIX 进程语义（`os.kill(pid, 0)` 存活检测、`SIGKILL`、`start_new_session`），在 Windows 上行为不正确。

### Python 依赖（requirements.txt）与各系统说明

**三个平台使用同一份 `requirements.txt`，不需要按操作系统增减任何包。**

| 文件 | 内容 | 用途 |
|---|---|---|
| `requirements.txt` | `websockets>=12.0` | market-ingest 的 Binance WebSocket 行情 |
| | `pydantic>=2.0` | `contracts/python/models.py`：dmr-adapter 校验候选消息 |
| | `jsonschema>=4.0` | 契约 JSON Schema 单测 |
| `requirements-dev.txt` | `-r requirements.txt` + `pytest>=8.0` | 运行全部单测（OnlyCoin 系列使用 pytest） |

对全部源码做 import 审计后，标准库以外的依赖只有以上 4 个包（`pytest` 仅测试使用）。api-gateway 与选币引擎主体只用标准库。

| 平台 / 架构 | Python 3.11 | Python 3.12 | Python 3.13 | 说明 |
|---|---|---|---|---|
| Linux x86_64（manylinux） | ✅ | ✅ | ✅ | 本机实装验证（3.12） |
| Linux aarch64（manylinux） | ✅ | ✅ | ✅ | |
| macOS arm64（Apple Silicon） | ✅ | ✅ | ✅ | `pydantic-core` / `rpds-py` 为 `macosx_11_0_arm64` wheel |
| macOS x86_64（Intel） | ✅ | ✅ | ✅ | `macosx_10_12_x86_64` wheel |
| Windows x64 | ✅ | ✅ | ✅ | `win_amd64` wheel |
| Windows ARM64 | ✅ | ✅ | ✅ | `win_arm64` wheel |

表中每一格都在 2026-09-10 用 `pip download --only-binary=:all: --platform <平台> --python-version <版本> -r requirements-dev.txt` 核对过：全部依赖（含传递依赖，共 16 个包）均能下载到预编译 wheel，安装时**不需要 C/Rust 编译器**。

关于特定平台的包：

- **不需要 `tzdata`**：代码没有使用 `zoneinfo`，时间一律按 UTC 计算。
- **不要 `pip install fcntl`**：`fcntl` 是 POSIX 标准库，PyPI 上没有可在 Windows 上替代它的同名包。Windows 请改用 WSL2 或 Docker。
- **不需要 `python-dotenv`**：`.env` 由项目自带的加载器解析（`coin_selection/envutil.py`、`scripts/daemonize.py`）。
- 前端依赖由 `apps/web/package-lock.json` 锁定。锁文件已包含 rolldown 与 oxlint 在 `darwin-arm64/x64`、`win32-x64/arm64-msvc`、`linux-x64/arm64` 上的原生二进制，各平台直接执行 `npm ci` 即可。

系统级依赖（不在 `requirements.txt` 中，需要用系统包管理器安装）：

| 依赖 | Ubuntu | macOS | Windows |
|---|---|---|---|
| Git | `apt install git` | Xcode Command Line Tools 或 `brew install git` | `winget install Git.Git`（WSL2 内用 `apt`） |
| Python ≥ 3.11 + venv | 24.04 自带 3.12，需另装 `python3-venv` | `brew install python@3.12` 或 python.org 安装包 | WSL2 内同 Ubuntu；原生用 `winget install Python.Python.3.12` |
| Node.js `^20.19 \|\| >=22.12` | NodeSource 或 nvm（apt 源版本过旧） | `brew install node` 或 nvm | WSL2 内同 Ubuntu；原生用 `winget install OpenJS.NodeJS.LTS` |
| CA 证书 | `ca-certificates` | python.org 安装包需运行 `Install Certificates.command` | 系统自带 |
| Docker（可选） | Docker Engine + Compose v2 | Docker Desktop | Docker Desktop（WSL2 后端） |

### Ubuntu（24.04 LTS，已实测）

以下命令在终端中执行。克隆之后的命令均在**仓库根目录**执行，另有说明的除外。

**1. 安装系统依赖**

```bash
sudo apt update
sudo apt install -y git curl ca-certificates python3 python3-venv python3-pip
python3 --version            # 24.04 为 3.12.x；须 ≥ 3.11
```

Ubuntu 22.04 自带 Python 3.10，不满足要求，可通过 deadsnakes PPA 安装 3.12，然后在后续命令中用 `python3.12` 代替 `python3`：

```bash
sudo add-apt-repository -y ppa:deadsnakes/ppa
sudo apt install -y python3.12 python3.12-venv
```

**2. 安装 Node.js**

Ubuntu 官方源中的 `nodejs` 版本低于 Vite 8 要求的 20.19，请改用 NodeSource（或 nvm）：

```bash
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt install -y nodejs
node -v && npm -v            # node 须 ≥ v22.12（或 ≥ v20.19）
```

**3. 克隆并安装依赖**

```bash
git clone https://github.com/shenchuangwl/AI-Crypto-Portfolio.git
cd AI-Crypto-Portfolio
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt        # 需要跑测试时改用 requirements-dev.txt
cd apps/web && npm ci && npm run build && cd ../..
```

**4. 配置**

```bash
cp .env.example .env
nano .env                    # 填写 COINGECKO_API_KEY；付费 key 同时设置 COINGECKO_USE_PRO=1
```

**5. 初始化数据并启动**

```bash
source .venv/bin/activate
bash scripts/smoke-p1-pipeline.sh                     # 隔离冒烟，末行应为 OK p1 pipeline smoke
START_SELECTION_LOOP=1 bash scripts/start-all.sh      # ingest + gateway + 15 分钟选币循环
curl -s http://127.0.0.1:18080/api/v1/health          # 首轮扫描完成前返回 degraded 属正常
```

浏览器访问 <http://127.0.0.1:18080/terminal>。停止服务：`bash scripts/stop-all.sh`。

**6. Ubuntu 配置要点**

- 服务默认只监听 `127.0.0.1`。如需局域网访问，请设置 `HOST=0.0.0.0 bash scripts/start-all.sh`，并用 `ufw` 限制来源 IP，因为网关没有鉴权。
- `daemonize.py` 拉起的进程重启后不会自动恢复，开机后需重新执行 `start-all.sh`，或自行配置 systemd 等进程管理器（仓库未提供现成配置）。
- 定时清理数据：先把 `packages/config/retention.json` 的 `mount` 改为你的数据盘挂载点，再按 [部署](#部署) 一节配置 cron。
- 保持系统时间同步（`timedatectl status` 应显示 `System clock synchronized: yes`），15 分钟节点按 UTC 墙钟对齐。

### macOS（Apple Silicon / Intel，未实测）

以下命令在“终端”（默认 zsh）中执行。

**1. 安装系统依赖**

```bash
xcode-select --install                                 # 提供 git 等命令行工具
# 如未安装 Homebrew：
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
brew install python@3.12 node
python3.12 --version && node -v                        # Python ≥ 3.11；Node ≥ 22.12
```

也可以使用 python.org 的官方安装包。**安装后必须运行一次** `/Applications/Python 3.12/Install Certificates.command`，
否则访问 Binance / CoinGecko 时 `urllib` 与 `websockets` 会报 `CERTIFICATE_VERIFY_FAILED`。Homebrew 版 Python 没有这个问题。

**2. 克隆并安装依赖**

```bash
git clone https://github.com/shenchuangwl/AI-Crypto-Portfolio.git
cd AI-Crypto-Portfolio
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
cd apps/web && npm ci && npm run build && cd ../..
```

**3. 配置**

```bash
cp .env.example .env
open -e .env                 # 用“文本编辑”填写 COINGECKO_API_KEY
```

**4. 初始化数据并启动**

```bash
source .venv/bin/activate
bash scripts/smoke-p1-pipeline.sh
START_SELECTION_LOOP=1 bash scripts/start-all.sh
open http://127.0.0.1:18080/terminal
```

停止服务：`bash scripts/stop-all.sh`。

**5. macOS 配置要点**

- **bash 版本**：macOS 自带 bash 3.2。仓库脚本未使用 bash 4 语法（已逐一检查），请始终以 `bash scripts/xxx.sh` 调用。
- **架构一致**：Apple Silicon 上请使用原生 arm64 的 Python 与 Node，不要在 Rosetta（x86_64）终端里安装依赖，否则会装错原生二进制。一旦混装，删除 `.venv` 和 `apps/web/node_modules` 后重新安装。
- **防止休眠**：Mac 休眠会暂停 15 分钟循环。需要长时间运行时，在系统设置中关闭自动休眠，或另开一个终端执行 `caffeinate -dims`（按 Ctrl+C 结束）。
- **防火墙**：默认只监听 `127.0.0.1`，不会触发弹窗。设置 `HOST=0.0.0.0` 后，系统可能询问是否允许 Python 接受传入连接。
- **进程管理**：请使用 `start-all.sh` / `daemonize.py` 管理选币循环。`scripts/run-coin-selection-loop.sh stop` 中的孤儿进程清理依赖 Linux 的 `/proc`，在 macOS 上这一步不会生效。
- **定时清理**：`prune_data.py` 可在 macOS 上运行（`os.statvfs` 可用）；定时可用 `crontab` 或 launchd（未验证）。不同 cron 实现对 `CRON_TZ` 的支持不同，建议直接按本地时间书写。
- **Docker**：也可以安装 Docker Desktop for Mac，按 [部署](#部署) 中的 Compose 流程运行。

### Windows

Windows 有三种方式，按推荐程度排序：

| 方式 | 适用 | 功能完整度 |
|---|---|---|
| **A. WSL2 + Ubuntu**（推荐） | 需要完整功能的开发与运行 | 完整 |
| B. Docker Desktop | 习惯用容器、不想配 Python 环境 | 完整（`selection-loop` profile 未验证） |
| C. 原生 PowerShell | 只做前端开发或界面预览 | **不含选币扫描** |

#### 方式 A：WSL2 + Ubuntu（推荐）

**1. 安装 WSL2**：在“以管理员身份运行”的 PowerShell 中执行，完成后按提示重启：

```powershell
wsl --install -d Ubuntu-24.04
```

重启后从开始菜单打开 “Ubuntu 24.04”，设置 Linux 用户名与密码。

**2. 在 WSL 的 Ubuntu 终端中**，完整执行上文 [Ubuntu](#ubuntu2404-lts已实测) 的第 1–5 步。

**3. 在 Windows 浏览器中**访问 <http://127.0.0.1:18080/terminal>。WSL2 默认把 `localhost` 端口转发到 Windows。

WSL2 配置要点：

- **把仓库克隆到 Linux 文件系统**（如 `~/AI-Crypto-Portfolio`），不要放在 `/mnt/c/...`。跨文件系统访问很慢，还会让 Vite 的文件监听与脚本可执行位出问题。
- **在 WSL 内安装 Node**：执行 `which node npm`，应显示 `/usr/bin/...`，而不是 `/mnt/c/Program Files/...`（那是 Windows 版 Node，通过 PATH 互通混进来的）。
- 如果 Windows 侧无法访问 `127.0.0.1:18080`，可尝试在 WSL 中以 `HOST=0.0.0.0 bash scripts/start-all.sh` 启动，或启用 WSL 的 mirrored 网络模式。
- 主机休眠唤醒后，如发现 WSL 时间落后（`date -u` 与实际不符），可执行 `sudo hwclock -s` 同步，否则 15 分钟节点会错位。
- 换行符：仓库的 `.gitattributes` 规定 `*.sh`、`*.py`、`*.yml` 始终以 LF 检出，即使在 Windows 侧克隆，脚本也不会因 CRLF 报 `$'\r': command not found`。

#### 方式 B：Docker Desktop

1. 安装 [Docker Desktop](https://www.docker.com/products/docker-desktop/)，启用 WSL2 后端。大型企业使用 Docker Desktop 可能需要付费订阅，请留意其许可条款。
2. 在宿主机构建前端（Windows 原生 Node 或 WSL 内均可）：

   ```powershell
   cd AI-Crypto-Portfolio\apps\web
   npm ci
   npm run build
   cd ..\..
   ```

3. 在仓库根目录启动（建议加上 [部署](#部署) 一节中的 `docker-compose.local.yml`，只监听本机）：

   ```powershell
   docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.local.yml up -d
   docker compose -f deploy/docker-compose.yml --profile loop up -d    # 选币循环（未验证）
   ```

4. 浏览器访问 <http://127.0.0.1:18080/terminal>。

仓库位于 WSL 文件系统中时，在 WSL 终端里执行 `docker compose` 性能更好。`selection-loop` 容器会读取仓库根目录的 `.env`，`COINGECKO_API_KEY` 在其中配置即可。

#### 方式 C：原生 PowerShell（仅前端开发 / 界面预览）

此方式可以运行 market-ingest、api-gateway 与前端，**不能运行选币扫描**（原因见 [平台支持矩阵](#平台支持矩阵)）。
没有扫描数据时，选币榜显示 `contracts/examples` 中的样例快照，选币榜X/Y 返回 503；合约清单与 K 线使用 Binance 实时数据。

**1. 安装系统依赖**（PowerShell，完成后重新打开窗口以刷新 PATH）：

```powershell
winget install -e --id Git.Git
winget install -e --id Python.Python.3.12
winget install -e --id OpenJS.NodeJS.LTS
py -3.12 --version; node -v
```

**2. 克隆并安装依赖**：

```powershell
git clone https://github.com/shenchuangwl/AI-Crypto-Portfolio.git
cd AI-Crypto-Portfolio
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
cd apps\web; npm ci; npm run build; cd ..\..
```

**3. 启动**（两个 PowerShell 窗口，均在仓库根目录执行，窗口保持打开）：

```powershell
# 窗口 1：market-ingest（:18100）
$env:PYTHONUTF8 = "1"
$env:PYTHONPATH = "services\market-ingest\src"
.\.venv\Scripts\python.exe -m market_ingest
```

```powershell
# 窗口 2：api-gateway + 前端（:18080）
$env:PYTHONUTF8 = "1"
.\.venv\Scripts\python.exe services\api-gateway\mock_server.py --host 127.0.0.1 --port 18080
```

浏览器访问 <http://127.0.0.1:18080/terminal>。在窗口中按 Ctrl+C 即可停止。

**前端开发**：后端可以运行在 WSL2 或另一台机器上，只在 Windows 原生运行 Vite：

```powershell
cd apps\web
$env:VITE_PROXY_TARGET = "http://127.0.0.1:18080"
npm run dev                  # http://127.0.0.1:5173
```

Windows 原生配置要点：

- **`PYTHONUTF8=1`**：部分脚本读取含中文的 JSON 时没有显式指定编码，控制台重定向输出也可能遇到中文编码问题。开启 Python UTF-8 模式可避免这类错误。可以写入用户环境变量：`[Environment]::SetEnvironmentVariable("PYTHONUTF8", "1", "User")`。
- **环境变量写法**：PowerShell 用 `$env:NAME = "value"`，只对当前窗口有效；`PYTHONPATH` 在 Windows 上用分号 `;` 分隔多个路径。
- **虚拟环境路径**是 `.venv\Scripts\python.exe`，不是 `.venv/bin/python`。
- **`.env` 不会被自动读取**：原生方式没有经过 `daemonize.py`，网关与 ingest 需要的变量请用 `$env:` 设置。
- **不要执行** `scripts\*.sh`、`start-all.sh`、`stop-all.sh` 或 `python scripts\daemonize.py`，它们只支持 POSIX 系统。
- `python -m coin_selection` 会报 `ModuleNotFoundError: No module named 'fcntl'`，这是预期行为，请改用方式 A 或 B。
- PowerShell 提示 `npm.ps1 cannot be loaded because running scripts is disabled` 时，执行 `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`，或改用 `npm.cmd`。
- Windows ARM64：Python 依赖有 `win_arm64` wheel，锁文件也包含 `win32-arm64-msvc` 原生二进制（未实测）。

### 各系统配置要点对照

| 配置项 | Ubuntu / WSL2 | macOS | Windows 原生 |
|---|---|---|---|
| `.env` 位置 | 仓库根目录 | 仓库根目录 | 仓库根目录（原生方式不会自动加载） |
| `.env` 的读取方 | 扫描器 + `daemonize.py` 拉起的服务 | 同左 | 无，需用 `$env:` 设置 |
| 端口 / 监听地址 | shell 中设置 `PORT`、`HOST` | 同左 | 用 `--host` / `--port` 参数 |
| 虚拟环境 Python | `.venv/bin/python` | `.venv/bin/python` | `.venv\Scripts\python.exe` |
| 启停方式 | `start-all.sh` / `stop-all.sh` | 同左 | 前台窗口 + Ctrl+C |
| 数据目录 | `data/`（自动创建） | 同左 | `data\`（自动创建） |
| 时间基准 | UTC（00:00 UTC 锚点，15 分钟节点） | 同左 | 同左 |
| 定时清理 | cron + `run-retention.sh` | crontab / launchd（未验证） | 不支持（`run-retention.sh` 为 bash） |
| 换行符 | LF | LF | `.gitattributes` 强制脚本为 LF |

### 跨平台常见问题

| 现象 | 平台 | 处理 |
|---|---|---|
| `ModuleNotFoundError: No module named 'fcntl'` | Windows 原生 | 预期行为，选币扫描请在 WSL2 或 Docker 中运行 |
| `SSL: CERTIFICATE_VERIFY_FAILED` | macOS（python.org 版） | 运行 `Install Certificates.command`，或改用 Homebrew 版 Python |
| `Cannot find module '@rolldown/binding-…'` 或 `'@oxlint/binding-…'` | 全部 | 删除 `apps/web/node_modules` 后重新执行 `npm ci`；不要把在别的系统上安装的 `node_modules` 拷贝过来 |
| `$'\r': command not found` | WSL / Git Bash | 用 `git config core.autocrlf input` 重新克隆；仓库的 `.gitattributes` 已对 `*.sh` 强制 LF |
| `error: externally-managed-environment` | Ubuntu 24.04 / Homebrew Python | 不要直接用系统 `pip`，按上文先创建 `.venv` 再安装 |
| `ensurepip is not available` | Ubuntu | `sudo apt install python3-venv`（或 `python3.12-venv`） |
| Vite 报 Node 版本过低 | 全部 | 升级到 Node ≥ 20.19 或 ≥ 22.12 |
| Windows 浏览器打不开 WSL 中的服务 | WSL2 | 见方式 A 的配置要点 |
| `npm.ps1 cannot be loaded` | Windows PowerShell | `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` 或改用 `npm.cmd` |

## CoinGecko 配置（必读）

### 为什么需要

G3 闸门以及依赖流通量的所有功能（流通市值、30m/2h/6h 流通市值等级、选币榜Y 的 216 组合主导层）都依赖 CoinGecko：

| 调用 | 用途 | 代码位置 |
|---|---|---|
| `GET /derivatives/exchanges/binance_futures` | Binance 合约 → `coingecko_id` 映射（叠加 `mapping_overrides.json` 的乘数伪币覆盖） | `services/coin-selection/src/coin_selection/gate3.py` |
| `GET /coins/markets?vs_currency=usd&ids=...` | 批量获取流通供应量 / 市值 | 同上 |

### 你必须自备 API key

- **请自行前往 [CoinGecko](https://www.coingecko.com/) 注册并申请 API key，相关费用由你自行承担。** 需使用你自己名下、且符合本项目接口调用量与套餐要求的 key，依赖该服务的功能才能稳定运行。
- **本仓库不提供、也不共享原作者或任何其他人的 CoinGecko API key。** 仓库中只有空占位符；请勿把你的 key 提交到任何公开位置。
- 不配置 key 时，程序会以无 key 方式访问 `api.coingecko.com`（公共接口按 IP 共享限额），并自动放慢节奏（批大小 ≤150、批间隔 ≥3 秒、429 时加长退避）。
  发布验证中，无 key 模式在 40 个合约的冒烟中成功完成了映射与流通量获取，但全量扫描下**极易触发 429**，不建议长期使用。

### 配置字段

| 变量 | 默认 | 说明 |
|---|---|---|
| `COINGECKO_API_KEY` | 空 | 你的 key。为空时读取别名 `CG_API_KEY` |
| `CG_API_KEY` | 空 | 备用变量名，二选一 |
| `COINGECKO_USE_PRO` | `0` | `0`：Demo key，请求 `https://api.coingecko.com/api/v3`，请求头 `x-cg-demo-api-key`；`1`：付费 key，请求 `https://pro-api.coingecko.com/api/v3`，请求头 `x-cg-pro-api-key` |
| `COINGECKO_BASE` | 空 | 覆盖基础 URL。URL 中含 `pro-api` 时自动改用 Pro 请求头 |

**Demo key 必须保持 `COINGECKO_USE_PRO=0`**，付费 key 才设为 `1`。两者用错会返回鉴权错误。

### 配置步骤

1. 在 CoinGecko 官网注册账户，进入开发者控制台创建 Demo key，或订阅付费套餐获取 Pro key。
2. 在仓库根目录 `cp .env.example .env`，填入 `COINGECKO_API_KEY=<你的 key>`；付费 key 同时设置 `COINGECKO_USE_PRO=1`。
3. 运行一次扫描（冒烟或全量），检查 `latest.json` 中的 `meta.gate3.coingecko_key` 为 `true`、`meta.gate3.coingecko_host` 与套餐一致（`demo` / `pro`），且 `supply_missing` 接近 0。
4. 长期运行时关注 `meta.coingecko_credits`（程序估算的当日/当月用量，`month_cap` 默认按 10,000 计）。

### 接口限制（以 CoinGecko 官方为准）

以下信息于 **2026-09-10** 查阅自 [CoinGecko API Pricing](https://www.coingecko.com/en/api/pricing) 与 [官方错误码文档](https://docs.coingecko.com/docs/common-errors-rate-limit)，可能随时调整：

| 套餐 | 价格（月付） | 月调用额度 | 速率上限 |
|---|---|---|---|
| Public（无 key） | 免费 | — | 按 IP 共享限额 |
| Demo | 免费（需注册） | 10,000 | 100 次/分钟 |
| Basic | $35 | 100,000 | 300 次/分钟 |
| Analyst | $129 | 500,000 | 500 次/分钟 |
| Lite | $499 | 2,000,000 | 500 次/分钟 |
| Enterprise | 定制 | 定制 | 定制 |

- `429`：超出速率上限；`10002`：缺少 API key；`10005`：当前套餐无权访问该接口。**失败请求同样计入速率限额。**
- Demo 套餐要求在展示数据处署名 CoinGecko 并附链接；本项目前端页脚显示 “Powered by CoinGecko”，请按 CoinGecko 最新署名规范核对。
- **未验证事项**：①官方文档未明确 `/derivatives/exchanges/{id}` 在 Demo 套餐下的可用性，本次仅验证了无 key 模式；②Demo 月额度能否支撑 7×24 小时的 15 分钟循环未经实测，请以 `meta.coingecko_credits` 的实际读数评估是否需要升级套餐。

## 环境变量

完整模板见 [.env.example](.env.example)。常用变量如下（默认值取自源码）：

| 变量 | 默认 | 作用 |
|---|---|---|
| `HOST` / `PORT` | `127.0.0.1` / `18080` | 网关监听地址（`daemonize.py` 从 shell 读取，写在 `.env` 中无效） |
| `MARKET_INGEST_HOST` / `MARKET_INGEST_PORT` | `127.0.0.1` / `18100` | ingest 状态接口；`0` 表示随机端口 |
| `MARKET_INGEST_URL` | `http://127.0.0.1:18100` | 网关访问 ingest 的地址 |
| `MARKET_INGEST_ENABLE_WS` | `1` | 关闭则仅 REST 轮询 |
| `BINANCE_FAPI_REST` | `https://fapi.binance.com` | Binance 合约 REST |
| `BINANCE_FUTURES_WS` / `BINANCE_FAPI_WS_PATHS` | `wss://fstream.binance.com` / `/stream` | WS 地址与路径（不要改成 `/ws`，可能连上后静默） |
| `API_GATEWAY_SERVE_WEB` / `WEB_DIST` | `1` / `apps/web/dist` | 网关是否托管前端及产物路径 |
| `API_GATEWAY_USE_LIVE` | `1` | `0` 时网关使用 `contracts/examples` 样例数据 |
| `ENABLE_BOARD_X` / `ENABLE_BOARD_Y` | 开 | 临时关停选币榜X / Y（扫描器与网关需同步设置） |
| `COIN_SELECTION_DATA_DIR` / `DMR_INBOX_DIR` / `MARKET_INGEST_DATA_DIR` | `data/...` | 数据目录覆盖（冒烟脚本用它做隔离） |
| `ENABLE_MCAP_TF` / `ENABLE_LONG_RET` 及 `*_WORKERS`、`*_RATE_PER_SEC` | 开 | 流通市值等级 / 长周期涨跌幅列的开关与限速 |
| `DMR_ROOT` | `third_party/DMR_binance_Version_V16_A1` | 外部 DMR 执行层路径（未随仓库发布，见下文） |
| `DMR_PARAM_WHITELIST` | 见源码 | DMR adapter 接受的参数版本白名单 |
| `ONLYCOIN_ADMIN_TOKEN` | 空 | OnlyCoin 源控制写接口的 Bearer 令牌；未设置时该接口返回 503 |
| `VITE_API_BASE` / `VITE_USE_MOCK` / `VITE_PROXY_TARGET` | `/api/v1` / 关 / `http://127.0.0.1:18080` | 前端 API 前缀、离线 mock、开发代理目标 |

## 服务、端口与访问方式

| 服务 | 默认地址 | 启动入口 |
|---|---|---|
| api-gateway + 前端 | `http://127.0.0.1:18080` | `services/api-gateway/mock_server.py`（`--host` / `--port`） |
| market-ingest | `http://127.0.0.1:18100` | `python -m market_ingest`（`--once` / `--no-ws`） |
| 选币扫描 / 循环 | —（写文件） | `python -m coin_selection --force [--loop]` |
| Vite 开发服 | `http://127.0.0.1:5173` | `bash scripts/run-web.sh`（`/api` 反代到 18080） |

网关主要接口（完整定义见 `contracts/openapi/screener-api.v1.yaml`、`contracts/API_CONTRACT.md`）：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/health` | 健康状态（含循环心跳） |
| GET | `/api/v1/boards` | 板面变体注册表 |
| GET | `/api/v1/screener/latest` · `/confirmed` · `/events` | 选币榜快照 / 确认区 / SSE |
| GET | `/api/v1/screener-x/...`、`/api/v1/screener-y/...` | 选币榜X / Y 同构接口（未产出时返回 503 `board_snapshot_missing`） |
| GET | `/api/v1/markets/universe`、`/api/v1/market/{symbol}/klines` | 合约宇宙、K 线 |
| GET | `/api/v1/review/coverage` · `/summary` · `/trades` · `/symbols/{symbol}` | 复盘账本（`?board=main\|x\|y`） |
| GET | `/api/v1/dmr/candidates`、`/api/v1/selection/loop-status` | DMR 候选消息、循环状态 |
| GET | `/api/v1/screener-y/dmr-daily`、`/api/v1/review/onlycoin`、`/api/v1/onlycoin/sources/screener-y/status` | OnlyCoin 当日列表、历史与源状态 |
| PUT | `/api/v1/onlycoin/sources/screener-y/control` | OnlyCoin 源开关（需 `Authorization: Bearer $ONLYCOIN_ADMIN_TOKEN`） |

market-ingest：`/v1/status`、`/v1/universe`、`/v1/universe/stats`、`/v1/prices`、`/v1/klines`。

## 开发、构建与测试

### 前端

```bash
cd apps/web
npm run dev          # 开发服 :5173（或在仓库根目录 bash scripts/run-web.sh）
npm run build        # tsc -b && vite build → dist/
npm run lint         # oxlint
npm run verify       # 全部前端验证闸（排序、计数、板面拆分、复盘时间、OnlyCoin 等）
npm run verify:board-split   # 也可单独运行某一个闸
```

离线看界面：`VITE_USE_MOCK=1 npm run dev` 使用 `src/mocks` 中的模拟快照，无需后端。更多说明见 [apps/web/README.md](apps/web/README.md)。

### 后端

```bash
# 全部 Python 单测 + CI 护栏 + 验收闸 + 前端验证闸（缺数据的闸会 SKIP）；需先安装 requirements-dev.txt
PY=.venv/bin/python bash scripts/run-tests.sh

# 单个测试文件（stdlib runner，直接运行）
PYTHONPATH=services/coin-selection/src .venv/bin/python services/coin-selection/tests/test_board_variant_y.py

# OnlyCoin 系列使用 pytest
PYTHONPATH=services/coin-selection/src .venv/bin/python -m pytest -q services/coin-selection/tests/test_onlycoin_*.py

# 冒烟
bash scripts/smoke-p1-pipeline.sh    # ingest --once + 限量扫描 + DMR 纸面（隔离目录）
bash scripts/smoke-s2-s4.sh          # S2→S4 端到端
bash scripts/smoke-live.sh           # 需 ingest + gateway 已启动
```

在**全新克隆**中运行 `scripts/run-tests.sh` 时，已知会有以下失败。它们不是代码缺陷，而是依赖原作者本地的运行状态（详见 [验证状态](#验证状态)）：

- `test_x_v13_candidate_config.py`、`test_x_v13_switch.py`：依赖原作者本地的规则切换审计流水、注册表备份与部署时间窗口。后者的输出中还会出现一行“选币内核单元套件全绿”的 FAIL，它是被测脚本用系统 `python3` 重跑单测时的附带输出，不是独立的闸；
- `verify_new_four_zone.py`：需要先产出 `data/coin-selection/latest.json`（跑一轮扫描后通过）。

另有两处与内部设计文档交叉校验的用例（`test_mcap_combo.py`、`test_mcap_mapping.py` 中各一条），在文档不存在时会打印 `skip` 并跳过；216 映射本身仍由冻结文件、`mapping_hash` 与其余不变量用例约束。

### 离线工具

| 脚本 | 用途 |
|---|---|
| `scripts/verify_new_four_zone.py [--board y] [--day YYYYMMDD]` | 分区硬约束验收闸 |
| `scripts/verify_review_ledger.py`、`scripts/verify_rule_consistency.py` | 复盘账本 / 在线↔离线一致性验收 |
| `scripts/replay_dual_path.py`、`scripts/replay_screener_y.py` | 离线回放（不写生产状态机） |
| `scripts/reset_board_cycle.py --status\|--board y [--force]` | 选币榜Y 周期状态与重置（主板面硬拒） |
| `scripts/prune_data.py [--plan\|--apply]` | 数据保留清理（默认 dry-run） |
| `scripts/research_*.py` | 研究与回测脚本（读取本地快照，结果仅供参考） |

## 部署

### 本机守护进程（已验证）

按“快速开始”执行 `bash scripts/start-all.sh`。服务默认只监听 `127.0.0.1`。

如需定时清理数据，可参考以下 cron 配置。使用前请先把 `packages/config/retention.json` 中的 `mount` 改为你的数据盘挂载点（原值 `/data120` 为原作者环境）：

```cron
CRON_TZ=Asia/Shanghai
20 4 * * * /path/to/AI-Crypto-Portfolio/scripts/run-retention.sh >/dev/null 2>&1
```

### Docker Compose（默认 profile 已验证）

`deploy/docker-compose.yml` 以挂载源码的方式运行 `python:3.12-slim`，定义三个服务：`market-ingest`（启动时 `pip install -r requirements.txt`）、`gateway`（仅用标准库）和可选的 `selection-loop`（profile `loop`）。
容器内不构建前端，需先在宿主机构建：

```bash
# 仓库根目录
cd apps/web && npm ci && npm run build && cd ../..
docker compose -f deploy/docker-compose.yml up -d                  # market-ingest + gateway
docker compose -f deploy/docker-compose.yml --profile loop up -d   # 含 15 分钟选币循环（未验证）
docker compose -f deploy/docker-compose.yml down                   # 停止
```

- 默认映射 `18080:18080` 与 `18100:18100`，会**监听宿主机所有网卡**。只想本机访问，或需要换端口时，可加一个覆盖文件（Compose v2.24+ 支持 `!override`）：

  ```yaml
  # deploy/docker-compose.local.yml
  services:
    market-ingest:
      ports: !override
        - "127.0.0.1:18100:18100"
    gateway:
      ports: !override
        - "127.0.0.1:18080:18080"
  ```

  ```bash
  docker compose -f deploy/docker-compose.yml -f deploy/docker-compose.local.yml up -d
  ```

- `selection-loop` 容器未显式安装 `requirements.txt`，这一路径**未验证**；如遇依赖缺失，请在其 `command` 中补上 `pip install -q -r requirements.txt &&`。
- `.env` 的读取：`selection-loop` 中的扫描器（`coin_selection`）会通过挂载的源码目录读取仓库根目录的 `.env`，因此 `COINGECKO_API_KEY` 在该容器内生效；`gateway` / `market-ingest` 容器不读取 `.env`，`ONLYCOIN_ADMIN_TOKEN` 等变量需通过 compose 的 `environment` / `env_file` 传入。

### 公网部署

本项目**未提供**鉴权、限流与 TLS，GET 接口均无需认证。不建议直接暴露到公网；如确有需要，请置于带认证的反向代理之后。

## 未随仓库发布的数据与文件

| 内容 | 未发布原因 | 如何获得 |
|---|---|---|
| `data/`（快照、复盘账本 SQLite、K 线缓存、日志、PID；原作者环境约 45 GB） | 体量大、运行产物、可再生 | 首次运行自动创建；扫描循环持续产出；账本用 `build_review_ledger.py` / `replay_screener_y.py` 重建 |
| `.env` | 含个人凭据 | 由 `.env.example` 复制后自行填写 |
| `third_party/DMR_binance_Version_V16_A1`（DMR 执行层） | 独立的私有项目，含实盘交易代码与本地配置 | 不提供。`dmr-adapter` / `dmr-executor` 找不到它时只是探测失败（`exists=false`），不影响选币、网关与前端；如你有自己的执行层，可用 `DMR_ROOT` 指向它 |
| 内部设计、方案、评审与验收报告、研究材料 | 内部资料 | 不提供。项目机制说明见 `docs/qualified-vs-confirmed-explained.md` 与代码注释 |
| `apps/web/node_modules`、`apps/web/dist`、`.venv` | 可重新生成 | `npm ci`、`npm run build`、`pip install` |

## 验证状态

以下为 **2026-09-10** 在全新导出副本中的实测结果（Ubuntu / Python 3.12.3 / Node 24.18.0 / npm 12.0.2 / Docker 29.6.1，未配置 CoinGecko key）：

| 步骤 | 结果 |
|---|---|
| `pip install -r requirements-dev.txt` | ✅ 通过 |
| `npm ci` | ✅ 通过 |
| `npm run build` | ✅ 通过（有 chunk > 500 kB 警告） |
| `npm run verify` | ✅ 通过（exit 0） |
| `npm run lint` | ✅ 通过（3 条 react-hooks 警告） |
| `bash scripts/smoke-p1-pipeline.sh`（无 key） | ✅ 通过：40 合约扫描完成，G3 流通量 17/17 获取成功，DMR 纸面执行正常 |
| 网关在 `127.0.0.1` 启动并托管前端；`/api/v1/health`、`/api/v1/boards`、SPA 路由 | ✅ HTTP 200 |
| Docker Compose 默认 profile（market-ingest + gateway，端口经覆盖文件改到本机） | ✅ 两个容器正常运行；ingest 拉取 526 个合约并连上 WebSocket；网关 health / boards / SPA 返回 200 |
| `scripts/run-tests.sh` Python 单测 | ⚠️ 52 个测试文件通过，2 个失败（`test_x_v13_candidate_config.py`、`test_x_v13_switch.py`，依赖原作者本地切换状态；前者在原作者环境同样有 1 项失败）。前端验证闸全部通过 |
| Docker `selection-loop` profile | ⏸ **未验证** |
| 全量宇宙扫描 / 7×24 小时 15 分钟循环 | ⏸ **未验证**（耗时长、调用量大） |
| 配置 CoinGecko Demo / Pro key 后的运行 | ⏸ **未验证**（仓库不提供 key） |
| macOS / Windows | ⏸ **未实测**；已完成静态核查（依赖 wheel、模拟无 `fcntl` 导入、锁文件原生二进制），见 [跨平台部署指南](#跨平台部署指南) |

## 常见问题

**Q：18080 / 18100 / 5173 端口被占用？**
在 shell 中设置 `PORT=28080 bash scripts/start-all.sh`；ingest 用 `MARKET_INGEST_PORT`；前端开发服用 `VITE_PORT`，并设置 `VITE_PROXY_TARGET` 指向新的网关端口。Docker 部署见上文的覆盖文件。

**Q：`/api/v1/screener-y/latest` 返回 503 `board_snapshot_missing`？**
选币榜Y 由主扫描顺带投影产生。先跑一轮全量扫描，或用 `scripts/replay_screener_y.py` 回放。

**Q：`/api/v1/screener/latest` 显示的是 2026-08 的旧数据？**
尚未产出 `data/coin-selection/latest.json` 时，主板面会回落到 `contracts/examples` 中的样例快照。跑一轮扫描即可。

**Q：CoinGecko 返回 429 或 10002 / 10005？**
429 表示超出限额：配置自己的 key 或降低频率。10002 表示缺少 key，或 Demo/Pro 与 `COINGECKO_USE_PRO` 不匹配。10005 表示套餐无权访问该接口。

**Q：页面打不开或空白？**
确认已执行 `npm run build` 且存在 `apps/web/dist`；网关日志见 `data/coin-selection/logs/gateway.log`。

**Q：DMR probe 显示 `exists: false`？**
正常。DMR 执行层未随仓库发布，纸面执行器仍会处理 `accepted/` 中的候选。

**Q：能否打开 `--fast-confirm` / `SM_FAST` 让确认区更快出现？**
仅限实验环境。生产禁止使用，它会绕过停留时间门槛导致跨级级联。

**Q：可以商用或二次开发吗？**
可以。Apache-2.0 允许商用、修改与再分发，但须满足第 4 条的要求：附带 `LICENSE`、保留 `NOTICE` 与版权署名、标注修改（见 [许可证与作者归属](#许可证与作者归属)）。
Binance、CoinGecko 等第三方数据服务的使用条款**不受**本项目许可证约束，商用前请自行确认。

## 安全提示

- **不要提交 `.env`** 或任何真实凭据；仓库中的 `.env.example` 只含占位符。
- 如果你曾在 fork 或提交中泄露过 key，请立即到对应平台**撤销并轮换**，仅删除文件并不能清除 Git 历史。
- 浏览器端不持有任何交易所或 CoinGecko 密钥，所有外部请求均由后端发起。
- `ONLYCOIN_ADMIN_TOKEN` 请使用高强度随机值，且只在本机环境中设置。
- 网关默认只监听本机；Docker 默认会映射到宿主机所有网卡，对外暴露前请自行加上认证、限流与 TLS。
- 如发现安全问题，请通过 GitHub 私下联系维护者，不要在公开 Issue 中披露细节。

## 贡献方式

欢迎提交 Issue 与 Pull Request，详细流程见 [CONTRIBUTING.md](CONTRIBUTING.md)。要点：

1. Issue 请附复现步骤、环境信息，日志**先脱敏**。
2. PR 前至少运行 `npm run lint`、`npm run verify`、`npm run build` 与相关 Python 单测，确保不引入新的失败。
3. 不要提交 `data/`、`.env`、构建产物或任何凭据。
4. 选币参数改动走 `board-variants.json` 的 `overrides` 与规则 manifest 流程，不直接改动已冻结的主板面参数。
5. 依 Apache-2.0 第 5 条，除非你另行明确声明，提交的贡献按 Apache-2.0 授权。

## 许可证与作者归属

本项目以 **[Apache License 2.0](LICENSE)** 开源（SPDX: `Apache-2.0`）。

```text
Copyright 2026 shenchuangwl (https://github.com/shenchuangwl)
```

- **原作者**：本项目由原项目独立开发者 [@shenchuangwl](https://github.com/shenchuangwl) 独立开发、维护与发布。详见 [COPYRIGHT.md](COPYRIGHT.md)。
- **你可以**：使用、复制、修改、合并、发布、分发、再许可和销售本项目及其衍生作品（包括商业用途）。许可证同时包含专利授权（第 3 条）。
- **再分发时须**（第 4 条）：①附带 `LICENSE` 副本；②在修改过的文件中注明修改；③保留原有的版权、专利、商标与署名声明；④以可读形式保留 [`NOTICE`](NOTICE) 中的署名内容。你可以追加自己的署名，但不得删改原有署名。
- **署名与名称**：请保留原作者署名，不得冒称本项目原作者。依第 6 条，许可证不授予使用原作者名称或商标的权利，但描述作品来源等合理惯常用途除外。推荐署名写法：

  ```text
  Based on AI Crypto Portfolio by shenchuangwl
  https://github.com/shenchuangwl/AI-Crypto-Portfolio — Licensed under Apache-2.0
  ```

- **与许可证的关系**：`COPYRIGHT.md` 中的作者归属说明不构成对 Apache-2.0 所授权利的额外限制；两者如有不一致，以 `LICENSE` 为准。
- **第三方组件**：各依赖遵循其自身许可证。TradingView Lightweight Charts（Apache-2.0）要求在渲染图表的页面保留指向 <https://www.tradingview.com> 的署名；展示 CoinGecko 数据时须按其要求署名。详见 [NOTICE](NOTICE)。
