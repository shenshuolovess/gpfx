# A股分析脚本

## 环境安装

需要 Python 3.11。首次运行前在项目根目录创建虚拟环境并安装锁定依赖：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 主评级入口

项目唯一的主评级入口是：

```text
src/综合评级_安全缓存并发版(1).py
```

在项目根目录使用虚拟环境运行：

```powershell
.\.venv\Scripts\python.exe '.\src\综合评级_安全缓存并发版(1).py'
```

该脚本读取 `data/input/沪深.csv`，自动确定最近交易日，优先复用
`cache/baostock` 中的临时行情缓存，并将每次成功取得的行情同步合并到
`data/history` 正式历史库，分类结果写入 `data/output`。

历史实现 `评级.py` 和 `评级（新版）.py` 已移动到
`archive/bak/legacy_rating/`，仅供回溯，不应作为运行入口或修改基线。

## 目录结构

```text
src/          活动代码、资源和 ChromeDriver
data/input/   股票池、选股明细、标准表等输入数据
data/output/  评级、筛选、音频、日志等生成结果
data/history/ 正式历史行情、运行快照和完整性清单（不参与缓存清理）
cache/        Baostock 行情、Chrome 临时配置和历史运行日志
archive/      历史代码和旧版本
tests/        公共模块测试
```

`.venv`、编辑器配置和项目配置文件继续保留在项目根目录。

## 统一配置与自动选取文件

默认路径、输入文件模式和运行参数集中在 `pipeline_config.toml`。其中：

- `ranking_pattern` 用于自动选取日期最新的 `top200_stocks_*.xlsx`；
- `classification_pattern` 用于自动选取日期最新的 `沪深_分类总表_*.csv`；
- 固定股票池、缓存目录、并发数和盘中轮询间隔也在该文件配置。

自动选择优先比较文件名中的 `YYYYMMDD`，没有日期时再比较修改时间。
所有输入都可以通过命令行显式覆盖，例如：

```powershell
# 主评级：覆盖股票池、输出目录和并发数
.\.venv\Scripts\python.exe '.\src\综合评级_安全缓存并发版(1).py' `
  --input '.\data\input\沪深.csv' --output-dir '.\data\output' --workers 4

# 20 日均线筛选：不传输入参数时自动使用最新分类总表和选股明细
.\.venv\Scripts\python.exe '.\src\filter_zd_up_ma20.py'

# 标的计算：默认自动使用最新 top200_stocks 文件
.\.venv\Scripts\python.exe '.\src\计算标的.py'

# 盘中监控：自动使用最新分类总表，只运行一轮
.\.venv\Scripts\python.exe '.\src\推推.py' --once
```

可对各脚本执行 `--help` 查看完整参数。显式传入的文件不存在时程序会立即报错，
不会静默退回旧文件。

## 公共工具

通用实现集中在 `src/stock_utils.py`，业务脚本不再各自维护重复版本：

- `normalize_code`：支持六位代码、`.SH/.SZ/.BJ`、`.XSHG/.XSHE`、
  `sh.600000`、Excel 数值代码，并可输出六位、后缀式或 Baostock 格式；
- `read_csv_auto` / `read_table`：自动探测 UTF-8、GB18030、GBK 等常见编码；
- `latest_matching_file`：按文件名日期和修改时间选择最新文件；
- `dated_output_path` / `timestamped_output_path`：统一结果文件命名；
- `write_csv`：统一使用 UTF-8 BOM 输出，便于 Excel 打开。

新增公共能力时应优先放入该模块，避免在单个业务脚本内复制工具函数。

## 分类边界测试与历史回测

分类规则已独立到 `src/classification_rules.py`。运行全部边界和公共模块测试：

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

### 正式历史库

`cache/` 只用于提速，随时可以清理；回测不再读取它。`data/history/` 是正式数据：

- `daily/<市场>/<代码>.csv`：每只股票一份连续日线，按日期增量合并和去重；
- `benchmark/<市场>/<代码>.csv`：基准指数历史；
- `manifest.json`：来源、复权方式、日期范围、行数和 SHA-256 校验和；
- `snapshots/`：每次主评级的股票池、实际分类结果和分类规则版本。

首次升级时，把现有缓存一次性迁移进去（不会删除或移动缓存）：

```powershell
.\.venv\Scripts\python.exe '.\src\migrate_cache_to_history.py'
```

以后每次运行主评级都会自动增量更新正式历史库。建议把 `data/history` 定期备份到
另一块磁盘或备份系统；它被 `.gitignore` 排除，避免把大量行情误提交到 Git。

使用正式历史库做轻量历史回测：

```powershell
# 默认抽样 50 只股票、3 个历史截面，统计未来 5/20/60 日表现
.\.venv\Scripts\python.exe '.\src\backtest_classification.py'

# 扩大样本；0 表示使用股票池全部股票
.\.venv\Scripts\python.exe '.\src\backtest_classification.py' `
  --max-stocks 0 --snapshots 5 --step 20 --horizons 5,20,60
```

回测只读取 `data/history`，不会联网，并默认按 `manifest.json` 校验文件完整性。
每个截面使用截至当日的滚动指标，并预留完整未来收益窗口；结果明细和分类汇总写入 `data/output`。
汇总同时包含平均/中位/10%截尾收益、指数超额、同截面股票池超额、不同股票数、
覆盖截面数、截面稳定性、最大单股贡献、窗口重叠和数据质量提示。

参数含义：

- `max-stocks`：最多回测股票数，`0` 表示全部；
- `snapshots`：历史截面数，`0` 表示使用全部可用截面；
- `step`：相邻截面间隔的交易日数，必须大于0；
- `horizons`：未来观察周期，使用逗号分隔，例如 `5,20,60`。

若希望收益窗口互不重叠，建议单独检验不同周期，例如60日回测使用
`--step 60 --horizons 60`。窗口重叠并不会使收益计算错误，但会降低样本独立性。

这个简单回测仍然是“用当前分类规则重算历史信号”。`snapshots/signals` 保存的是主评级
当日实际输出，可用于以后实现无回看偏差的逐日回放；规则文件哈希可确认当时使用的版本。

## 分类规则对比与优化

生产分类阈值集中在 `src/classification_rules.py` 的 `RuleConfig`，默认值保持当前规则不变。
探索性候选只写在根目录 `classification_rule_configs.toml`，不会自动影响主评级。例如：

```toml
[candidates.relaxed_rising]
description = "探索性方案：适度放宽上升趋势门槛"
rising_trend_min = 68
rising_direction_min = 24
```

在完全相同的股票、日期、指标和未来收益上比较基线与候选：

```powershell
.\.venv\Scripts\python.exe '.\src\compare_classification_rules.py'

# 全股票、使用全部可用截面；截面之间间隔5个交易日
.\.venv\Scripts\python.exe '.\src\compare_classification_rules.py' `
  --max-stocks 0 --snapshots 0 --step 5 --horizons 5,20,60
```

脚本按时间顺序做60%训练期、20%验证期、20%测试期切分，并输出：

- 明细：同一样本在每套规则下的分类，以及未来收益、超额、最大涨幅和最大回撤；
- 表现：分规则、分类、周期和样本区间统计，并给出平均超额的Bootstrap 95%区间；
- 基线差异：候选规则各项指标减去生产基线；
- 覆盖率和稳定性：分类占比、边界模糊率、相邻截面变化率；
- 变化矩阵和变化样本：候选究竟把哪些类别改成了哪些类别；
- 迁移代表股票：按原分类→新分类分别列出同池超额正向榜和负向榜；
- 阈值变化：列出候选相对基线修改的参数、方向和具体触发信号；
- 元数据：完整阈值、规则哈希、截面和输出文件，保证实验可追溯。

工作台左侧的“规则对比”会读取最近一次完整实验，可按总体、训练期、验证期和测试期切换，并排查看基线与候选规则的同池超额、跑赢同池率、截面置信区间、样本质量和分类稳定性。“分类迁移诊断”可折叠查看每种迁移的具体触发阈值、代表股票正向榜和负向榜。页面不会自动把候选规则替换为生产规则。

任务中心“规则对比”的默认参数为全部股票、30 个截面、5 日间隔和 5 日收益，形成不重叠的短周期基准实验。中长期实验建议分别运行 `step=20, horizons=20` 和 `step=60, horizons=60`，不要把不同周期混在一次重叠窗口实验中得出结论。

`样本充足=False` 的结果只作观察。候选只有在验证期和未参与调参的测试期表现都稳定，
且没有明显提高分类跳变率或制造规则空档时，才考虑更新生产默认值。当前历史股票池仍有
幸存者偏差，正式定稿前应继续积累逐日股票池快照并扩充到多个市场周期。

## 机会评分

机会评分位于分类之上，不参与九种分类判断。分类继续描述股票当前状态，机会评分只提供
研究排序。评分配置集中在根目录 `opportunity_score.toml`，包括趋势质量、相对强弱、
突破动能、趋势确认、形态准备、大盘调整和风险扣分。

综合评级会自动在分类总表中增加评分字段，并另行输出按分数降序排列的
`沪深_机会评分_YYYYMMDD.csv`。也可以只读取最新分类总表重新生成：

```powershell
.\.venv\Scripts\python.exe '.\src\generate_opportunity_scores.py'
```

机会评分的验证目标是横截面排序能力，而不是某个分类的平均收益。运行同池五分位回测：

```powershell
.\.venv\Scripts\python.exe '.\src\backtest_opportunity_score.py' `
  --max-stocks 0 --snapshots 30 --step 5 --horizons 5
```

回测输出五档分层、逐截面秩相关 IC、Q5 减 Q1 同池超额及训练/验证/测试分段结果。
`v1.0-transparent` 当前明确标记为实验版；在测试期稳定为正以前，不应把分数解释为收益概率
或直接据此交易。

## 产业标签

使用 `src/stock_industry_tags.py` 为股票池追加最多三个细分产业标签。相关度表示业务
相关程度，不是股价相关性。程序综合最新主营产品收入、东方财富细分行业和业务题材，
其中主营收入权重最高，概念归属只能作为佐证，低于阈值不会为凑满三个而输出。

```powershell
# 有当天原始资料时直接离线重算
.\.venv\Scripts\python.exe '.\src\stock_industry_tags.py' --offline

# 重新获取公开F10题材和主营构成
.\.venv\Scripts\python.exe '.\src\stock_industry_tags.py' --refresh
```

标准标签、同义词、噪声过滤和主营产品映射位于根目录 `industry_tags.toml`。输出包括：

- `data/output/沪深_产业标签_YYYYMMDD.csv`：只保留代码、名称、市场及标签相关字段；
- `data/output/沪深_产业标签_审计_YYYYMMDD.csv`：同样使用精简字段，便于核对依据；
- `data/history/company_profiles/eastmoney_corethemes_YYYYMMDD.json`：可离线复算的原始资料。

每个标签同时记录分数和证据，例如最新主营产品、报告日期、收入占比或细分行业。
指数、ETF等非个股不打标签。公开F10资料和自动映射可能存在遗漏，正式用于选股前应优先
人工检查审计文件中分数较低、名称过宽或首次出现的标签。

## 本地 Web 控制台

`src/web_console.py` 提供本地可视化操作页面，用于替代日常命令行操作。第一版包含数据状态概览、
任务白名单、后台运行、实时进度、实时日志、任务停止和结果浏览。能够输出“当前/总数”的任务显示
精确百分比；无数量信息的准备阶段显示动态执行状态。所有任务均独立启动，不提供一次性执行全部功能，
便于每一步完成后先核对输出，再决定是否继续下一项。
服务只监听 `127.0.0.1`，前端不能提交任意系统命令，所有数据仍保存在本项目目录。

```powershell
# 首次使用或依赖更新后安装
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 启动控制台
.\.venv\Scripts\python.exe .\src\web_console.py
```

浏览器打开 `http://127.0.0.1:8000`。需要联网的任务会在卡片上标注；市场日报默认使用免费的
确定性统计模式。关闭启动控制台的终端后服务停止，已经生成的数据和日志不会丢失。
日常使用也可以直接双击项目根目录下的 `启动研究控制台.bat`，它会启动服务并自动打开浏览器。

## 股票图示页面

`src/generate_stock_page.py` 把本地行情、综合评级、产业标签、公司概况、主要财务指标和
主营构成合并成一个完全离线的响应式HTML页面。页面包含注册地、公司简介、管理层、成立及
上市日期、最新财报摘要、盈利能力、成长、现金流、偿债能力、价格及MA20/60/200、趋势评分、
主营收入环图、标签依据、估值指标和风险观察。
页面还会展示最近三份公开个股研报的发布日期、机构、评级、报告标题、正文观点标题和风险提示；
“事件”“投资要点”等无信息量的模板标题会被过滤。研报观点归属于原发布机构，不代表本项目判断。
页面会用本地规则生成“最新三份研报对比”：比较评级及评级变化、各年度EPS/PE预测、EPS预测
分歧、高频关注方向、重复风险主题，以及研报覆盖数和新鲜度，再给出一段可追溯的简短结论。
这个过程不调用大模型，不需要API密钥，也不会产生模型费用；原始研报卡片仍保留供逐份核对。

页面首屏采用“最小充分分析”：只显示业务、盈利、估值、市场状态四个相互独立的判断，重大
风险作为单独否决项，不生成容易重复计分的综合总分。估值分位基于当前股票池内同业有效样本，
会同时显示样本数；公司资料、完整指标和图表默认折叠为分析证据，可按需展开。

```powershell
# 更新公司概况和最新财务资料（联网执行，结果持久化到 data/history）
.\.venv\Scripts\python.exe '.\src\fetch_company_financials.py'

# 更新每只股票最新三份研报及观点（联网执行，结果持久化到 data/history）
.\.venv\Scripts\python.exe '.\src\fetch_stock_research.py' --workers 8 --limit 3

# 生成兆易创新页面
.\.venv\Scripts\python.exe '.\src\generate_stock_page.py' 603986.SH

# 其他股票只需替换代码
.\.venv\Scripts\python.exe '.\src\generate_stock_page.py' 300308.SZ

# 批量生成沪深.csv中的全部个股，并同时生成可搜索目录页
.\.venv\Scripts\python.exe '.\src\generate_stock_page.py' --all
```

个股页面输出为 `data/output/stock_pages/<公司名称>.html`，批量模式还会生成 `index.html`
搜索目录和 `generation_manifest.json` 生成清单。页面可以直接用浏览器打开，不依赖网络或外部
图表服务。指数等非个股会跳过；公司名称中的 Windows 非法文件名字符会自动替换。生成前需要
先有该股票的正式历史行情、最新分类总表、产业标签和主营资料。公司及财务资料保存在
`data/history/company_financials/eastmoney_company_financials_YYYYMMDD.json`，不会依赖临时缓存；
研报快照保存在 `data/history/research_reports/eastmoney_stock_reports_YYYYMMDD.json`。需要更新时
重新运行相应抓取命令，若当天需要强制重抓可加 `--force`。没有公开研报的股票会显示明确缺省提示。

## 行业与产业标签日报

`src/daily_market_brief.py` 自动读取最新分类总表、产业标签、上一交易日信号快照和公司财务
快照。所有涨跌、排名、行业聚合和代表股票都由代码计算；大模型只选择证据编号并归纳文字，
不能自行提供数字。日报会逐一分析股票数严格大于 3 只的全部行业和全部标签；样本恰好
4 只时会标记为小样本。模型输出如果遗漏、重复或引用错类型，会被校验拒绝并自动降级。

日报会从分类总表单独提取上证指数、上证50、沪深300、中证500、深证成指、创业板指和
科创50，展示各指数当日及中期表现。行业和标签默认以沪深300为统一大盘基准，计算当日、
5日、20日和60日超额表现；若沪深300缺失则回退到上证指数。股票池广度与大盘指数口径分开表述。

每个行业和标签同时显示数据质量提示：股票数量、财务数据覆盖率、标签平均相关度，以及
留一法单股影响检测。留一法依次剔除每只股票；若当日中位变化至少1.5个百分点、20日中位
变化至少5个百分点，或强势分类占比变化至少15个百分点，则标记影响最大的股票和触发原因。

报告同时生成行业、标签的红榜和黑榜。红榜依据包括相对抗跌、上涨覆盖面、中期表现、
趋势结构、相对强弱、分类转强、盈利增长和 ROIC；黑榜依据包括弱于股票池、下跌覆盖面、
中期走弱、低相对强弱、分类转弱、盈利承压、高位衰竭及高估值叠加利润压力。每条入榜依据
都会直接展示，信号计数只用于分组，不作为收益预测或买卖评分。

每个行业和标签还会展示最多 8 只主要股票：行业按总市值降序选择，标签先按标签相关度、
再按总市值选择。主要股票与“当日领涨代表”分开显示，避免把短期涨幅最高的小市值股票误当成
行业或标签核心。

```powershell
# 不调用模型，生成可审计的确定性统计日报
.\.venv\Scripts\python.exe '.\src\daily_market_brief.py' --no-llm

# 只生成模型将要读取的证据包
.\.venv\Scripts\python.exe '.\src\daily_market_brief.py' --evidence-only

# 配置任意支持 Chat Completions JSON 模式的 OpenAI 兼容接口
$env:LLM_API_KEY='替换为实际密钥'
$env:LLM_MODEL='替换为模型名称'
$env:LLM_BASE_URL='https://api.example.com/v1'
.\.venv\Scripts\python.exe '.\src\daily_market_brief.py'
```

未配置模型或模型调用失败时会自动降级为确定性统计版；使用 `--strict-llm` 可以改为失败即
报错。完整证据、模型结构化结果和运行清单保存在
`data/history/daily_briefs/YYYYMMDD/`，最终生成：

- `data/output/daily_briefs/沪深行业标签日报_YYYYMMDD.md`
- `data/output/daily_briefs/沪深行业标签日报_YYYYMMDD.html`

模型密钥只从环境变量读取，不会写入输出文件或代码仓库。模型提示词位于
`prompts/daily_market_brief.md`。

## 日志轮转与缓存清理

筛选和清仓分析使用统一轮转日志，默认写入 `data/output/logs`：

- 单个日志最大 5 MB；
- 默认保留 5 个备份；
- 参数在 `pipeline_config.toml` 的 `[logging]` 中调整。

缓存清理命令默认只预览，不会删除文件：

```powershell
.\.venv\Scripts\python.exe '.\src\maintenance.py'
```

确认预览结果后执行删除：

```powershell
.\.venv\Scripts\python.exe '.\src\maintenance.py' --apply
```

默认策略是缓存至少保留 30 天、每个股票代码至少保留最新 3 份行情，日志保留
30 天。可通过 `[maintenance]` 配置或 `--cache-days`、`--keep-per-code`、
`--log-days` 临时覆盖。建议先用 `--cache-days 0` 预览仅按份数压缩缓存的效果。
无论怎样设置缓存根目录，配置中的 `data/history` 都会被明确排除，不会被该工具删除。

## 仓库文件规则

`.gitignore` 默认排除虚拟环境、输入数据、生成结果、缓存、日志、音频和本地
ChromeDriver。代码、配置、测试、README 和 `requirements.txt` 可正常纳入版本控制。
