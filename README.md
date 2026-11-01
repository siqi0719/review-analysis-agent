# 评论分析 Agent（review-analysis-agent）

用 AI coding 方式搭建的门店评论分析智能体：每天的评论 Excel 放到桌面后，自动完成**入库 → 大模型提取问题与满意度 → 日报 → 可视化分析**，并支持用中文对话提问。

- **大模型**：DeepSeek（`deepseek-flash` 做批量提取，`deepseek-v4-pro` 做对话和日报）
- **三个可直接调用的分析 Skill**：问题提取与分类、问题关系拓扑（相关性分析）、满意度变化
- **自动化**：后台进程每小时轮询桌面「评论数据」文件夹
- **界面**：Streamlit 网页，包含自动看板和对话分析

---

## 一、整体流程

```
桌面\评论数据\评论_YYYYMMDD.xlsx      ← 每天一个文件
        │  watcher.py 每小时检查一次
        ▼
① 入库 SQLite（按 用户+门店+内容 去重）
        ▼
② 大模型提取（deepseek-flash）：每条评论 → [方面标签 + 正面/负面 + 原文片段] + 满意度 1–5 + 摘要
        ▼
③ 日报（deepseek-v4-pro）：SQL 先算好所有数字，大模型只写结论和建议
        ▼
④ 刷新 3 个 Skill 的可视化报告（HTML）
        ▼
app.py 网页：自动看板 + 对话 Agent（工具调用：3 个 Skill / 只读 SQL / 日报 / 立即拉取）
```

## 二、三个分析 Skill

| Skill | 回答的问题 | 方法 | 可视化 |
|---|---|---|---|
| **issue-extract** 问题提取与分类 | 用户主要夸什么、骂什么？哪些问题变多了？ | 按「提到该方面的评论条数」计数；负面率 = 每百条评论中的负面条数，用于跨期比较 | 正负面对比条形图、问题构成旭日图、环比变化图、典型原话 |
| **issue-topology** 问题关系拓扑 | 哪些问题总是一起出现？哪个问题最伤满意度？ | 提升度 lift = P(A∩B)/(P(A)P(B))，并要求最少共现数；Louvain 社区发现给问题聚类 | 问题关系网络图、关联热力图、满意度影响排行 |
| **satisfaction-trend** 满意度变化 | 满意度怎么变？哪天异常下跌、为什么？哪家店在变差？ | 按评论数加权的 7 日均线；异常 = z ≤ −2 且比前 28 天基线至少低 0.3 分 | 每日趋势 + 异常标注、分项评分周趋势、问题×周热力图、门店变化排行 |

每个 Skill 都在 `skills/<名称>/` 下，包含 `SKILL.md`（说明）和 `run.py`（实现），支持三种调用方式：

```bash
python skills/issue_topology/run.py --days 90          # 命令行，生成 reports/skills/*.html
```
- **Claude Code**：运行 `scripts/install_claude_skills.ps1` 后，用 `/issue-topology` 等命令调用
- **对话 Agent**：每个 `run.py` 的 `TOOL_SPEC` 注册为 DeepSeek 的工具，由 Agent 自动选择调用

## 三、问题标签体系（16 个 + 其他）

先由大模型从 300 条评论中归纳出草稿（`config/taxonomy_draft.json`），再经人工合并重叠项、划清边界后确定（`config/taxonomy.json`）。标签固定，保证不同日期的统计口径一致。

| 分组 | 标签 |
|---|---|
| 菜品 | 口味、食材新鲜、分量、菜品特色、菜品选择 |
| 服务 | 服务态度、上菜速度、服务效率 |
| 环境 | 环境氛围、卫生状况 |
| 价格 | 价格、性价比、优惠活动 |
| 到店 | 排队等位、交通位置、停车便利 |

**满意度口径**：以用户打的总评分（1–5）为主，缺失时用大模型判断的分数补上（视图 `v_comments.satisfaction`）。

## 四、数据来源与处理

### 来源
- **数据集**：大众点评 yf_dianping（442 万条餐厅评分与评论），收录于 [SophonPlus/ChineseNlpCorpus](https://github.com/SophonPlus/ChineseNlpCorpus)
- **实际下载**：HuggingFace 镜像 [dirtycomputer/yf_dianping](https://huggingface.co/datasets/dirtycomputer/yf_dianping) 自动转换的第一个 parquet 分片 `0000.parquet`（约 114 万行，2009-07 ~ 2012-02）
- **字段**：`userId, restId, rating, rating_env, rating_flavor, rating_service, timestamp, comment`
- 本仓库仅用于学习与研究演示。评论版权归原作者与原平台所有；如有异议请提 issue，会及时删除相关数据。

### 处理（`scripts/prepare_data.py`）
1. **选门店**：取有效评论（≥ 8 字）最多的 100 家门店（镜像没有门店名称表，门店显示为「门店_ID」）
2. **清洗**：去除非法字符和多余空白，按 用户+门店+内容 去重，丢弃缺少评分或时间的记录
3. **选时间窗口**：找评论最密集的连续 365 天（原始时间 2011-02-08 ~ 2012-02-07）
4. **日期平移**：整体平移到 2025-10-28 ~ 2026-10-27，模拟「每天有新评论」，原始时间保留在「原始时间」列
5. **控制成本**：每天随机抽样最多 60 条（seed=42），最终 12,540 条、363 个每日文件
6. **入库与分析**：0 分评分视为缺失；12,540 条评论全部经 deepseek-flash 提取，共 39,646 条方面标签

### 仓库中包含的数据
| 路径 | 内容 |
|---|---|
| `data/daily/评论_YYYYMMDD.xlsx` | 363 个每日评论文件（模拟每天放到桌面的文件） |
| `data/processed/sample_all.csv` | 抽样后的全部评论（汇总表） |
| `data/agent.db` | SQLite 数据库：评论、大模型分析结果、方面标签、轮询记录。克隆后可直接运行，无需重新调用大模型 |
| `reports/` | 每日日报（Markdown / JSON）与 3 个 Skill 的历史 HTML 报告 |

**未上传**：原始 parquet（305 MB，超过 GitHub 单文件限制）。如需从头复现，把 `0000.parquet` 下载到 `data/raw/` 后运行 `python scripts/prepare_data.py`。

### 数据的已知局限
- 原始数据是 2011 年的，日期经过平移
- 没有门店名称；样本中混有少量商场类门店，会落入「其他」标签（约占 10%）
- 约 28% 的评论缺少口味/环境/服务分项评分
- 每天的评论量不均匀（1–60 条），评论很少的日子结论仅供参考

## 五、快速开始

```bash
pip install -r requirements.txt
```
复制 `.env.example` 为 `.env`，填入自己的 DeepSeek API Key。然后在两个终端分别运行：

```bash
python watcher.py              # 后台轮询：启动时立即检查一次，之后每 60 分钟一次
```
```bash
streamlit run app.py           # 网页：http://localhost:8501
```

### 其他命令
```bash
python cli.py ingest                       # 把 data/daily 下的文件全部入库
python cli.py taxonomy                     # 让大模型重新归纳标签草稿
python cli.py extract --days 7             # 分析最近 7 天未分析的评论
python cli.py report --date 2026-10-27     # 生成某一天的日报
python watcher.py --once                   # 只轮询一次（可配合 Windows 任务计划程序）
python scripts/simulate_feed.py --since 20261021 --interval 60   # 演示：每 60 秒往桌面放一个文件
```

## 六、目录结构

```
app.py                 Streamlit 网页（看板 + 对话）
watcher.py             每小时轮询的后台进程
cli.py                 命令行入口
core/
  config.py            路径与环境变量（桌面路径通过系统接口获取，兼容 OneDrive）
  db.py                SQLite 表结构（WAL 模式，支持多进程同时读写）
  ingest.py            Excel 增量入库
  llm.py               DeepSeek 调用封装（JSON 输出、重试）
  taxonomy.py          标签体系归纳与加载
  extract.py           批量提取（每批 20 条，并发 8）
  report.py            日报生成
  pipeline.py          一次完整轮询（锁文件防止重复执行）
  agent.py             对话 Agent（工具调用）
skills/
  common.py            日期范围、数据读取、HTML 报告渲染
  issue_extract/       Skill 1：问题提取与分类
  issue_topology/      Skill 2：问题关系拓扑
  satisfaction_trend/  Skill 3：满意度变化
config/taxonomy.json   确认后的标签体系
scripts/               数据准备、模拟投放、安装 Claude Code Skill
```

## 七、分析结论示例（演示数据）

- **排队等位**是口碑最差的方面：提到排队的评论中 84% 是抱怨，但它对满意度的拉低很小，说明顾客对排队有心理预期
- **服务问题会连锁出现**：上菜速度与服务效率一起被抱怨的频率是随机情况的 4 倍（lift 4.06），与服务态度同属一个问题群
- **最伤满意度的问题**：食材新鲜（−0.82 分）、服务态度（−0.81 分）、性价比（−0.74 分）
- **异常下跌**：2026-07-02 满意度 3.47（前 28 天基线 3.80），当天「菜品选择」相关抱怨约为平时的 3 倍

## 八、设计取舍

- **大模型只做擅长的事**：理解文本、写结论；所有数字由 SQL 计算，防止编造
- **成本分层**：便宜模型做批量提取（全部 1.25 万条约 2–3 美元），强模型做推理与对话
- **统计方法**：lift 排除「高频标签和谁都相关」的假象；异常检测同时要求统计显著和最小跌幅，减少误报
- **稳健性**：入库去重、提取失败自动重试并在下次补齐、轮询加锁、单个 Skill 失败不影响整体
