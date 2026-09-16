# 环评数据工作流

## 本地开发、自测与合并

首次配置已完成。换电脑或重新克隆后，先准备 Python 3.10+ 虚拟环境并安装开发依赖，再安装本仓库钩子：

```bash
uv venv .venv
uv pip install --python .venv/bin/python -r requirements-dev.txt
.venv/bin/python scripts/dev.py setup
```

日常流程（把 `add-source` 换成这次功能的名称）：

```bash
# 1. 从 main 创建 feature/add-source，要求工作区干净
.venv/bin/python scripts/dev.py start add-source

# 2. 修改后提交，自动 lint；检查的是实际暂存内容
git add <本次修改的文件>
git commit -m "描述本次修改"

# 3. 合并到 main，自动检查合并后的暂存快照
git switch main
.venv/bin/python scripts/dev.py merge feature/add-source
```

可随时运行 `.venv/bin/python scripts/dev.py check`，一次执行 lint 和测试；`lint`、`test` 可分别运行。目前包含南京原有33项离线回归测试和6项真实Git流程测试，后者在临时仓库验证提交拦截、成功合并、测试失败回滚、普通merge拦截、冲突回滚和主分支直接提交拦截。测试不请求i-ESG，也不覆盖正式工作簿。苏州代码参与lint，尚未配置专门的苏州业务测试。

- 提交：Ruff检查暂存快照的所有Python源码，拦截语法、未定义变量和部分明显逻辑错误；暂未强制历史代码的格式/行长风格。
- 合并：强制生成合并提交；其提交钩子执行lint和全部测试，失败即阻止提交。推荐的 `dev.py merge` 还会自动撤销失败或冲突的合并，保留功能分支。
- 普通 `git merge`：已配置禁用默认快进并安装合并钩子；失败可能留下待处理的合并状态，可用 `git merge --abort` 撤销。
- 主分支：钩子阻止直接提交到main（首次初始化除外），新功能必须在功能分支提交后合并。
- 新增离线回归测试放进 `tests/test_*.py`，下次合并自动纳入。修改测试或钩子本身也应认真审查。

这是本地防误操作流程。`--no-verify`、显式 `--ff-only` 或关闭/修改钩子可以绕过本地检查；不要把它视为服务器分支保护。需要不可绕过的团队规则时，再配置远端CI及保护分支。

Git只管理代码与文档；缓存、虚拟环境、数据、账号环境文件和产出目录已忽略，现有本地文件保留。

## 南京单位官网及环评入口（当前任务）

一条命令生成最终工作簿：

```bash
cd /Users/fish/环艾
.venv/bin/python nanjing_departments_workflow.py --limit 0
```

唯一入口为 `nanjing_departments_workflow.py`，包含采集修复、缓存校验、字段拆分去重、官方入口补全、URL检测、名称归并、Excel输出和33项回归测试。
默认读取已成功取得的7070条受理缓存，不需要令牌，也不会消耗i-ESG额度。
输出保留用户指定的 `南京市审批单位_官网及环评入口_前10条测试.xlsx` 文件名，内容已更新为120个原始单位全量表，主页、环评入口、审批入口三列均已补齐。
项目状态、已修复问题与操作边界统一维护在 [AGENTS.md](AGENTS.md)。

常用操作：

```bash
# 完全离线重建
.venv/bin/python nanjing_departments_workflow.py --limit 0 --offline

# 回归测试，不访问网站或改写正式文件
.venv/bin/python nanjing_departments_workflow.py --self-test

# 需要重新采集时才使用，隐藏输入本人账号令牌
.venv/bin/python nanjing_departments_workflow.py --refresh --prompt-token --limit 20
```

默认仅使用受理模块1。当前保留20条逐项核验入口，并为剩余名称增加补充规则：南京区县按辖区生态环境公开栏目补齐，开发区按开发区主页和辖区/专题环评栏目补齐，省级或外地名称按对应官方生态环境入口或主管部门主页补齐。`--limit 0` 会把120个原始单位全部写入工作簿。Python依赖为 `httpx pycryptodome`，Excel导出使用Codex自带Node和 `@oai/artifact-tool`。原始分页缓存需要保留。

## 江苏省环评报告 / 批复采集（苏州试点，独立工作流）

从各级审批机关的公开网站抓取建设项目的**环评报告书（表）**和**批复**，统一转成 PDF，按项目归并。

- 报告口径（业主 2026-09-15 确认）：批复公告附件 → 拟审批公示附件（含受理+拟审批合并公示）→ 受理公示版兜底；建设单位报批前全本公示（S0）不取。同阶段按附件名版本 报批稿/审批稿 → 送审稿 → 公示稿/征求意见稿/全本 → 未标，再按日期新。文件名里标注阶段和版本
- 不交付：告知承诺、辐射类（`sz_sthjj_gzcn`、`sz_sthjj_fs_sl`、`sz_sthjj_fs_pz` 三个栏目，以及标题含 告知承诺/承诺制/辐射 的公告所涉项目），`match` 时整个项目剔除
- 百度网盘：只记录链接和提取码（`baidupan_links.csv`），不自动下载
- 数据源登记在 `sources.yaml`，一行一个"审批机关 × 栏目"

## 准备

```bash
uv venv --python 3.12 .venv
```

```bash
uv pip install --python .venv/bin/python httpx selectolax pypdf img2pdf pyyaml
```

```bash
brew install --cask libreoffice
```

LibreOffice 用来把 doc/docx/wps 批复和正文型批复转成 PDF。

## 运行

全量（首次回溯全部历史，苏州约数小时，可以中断后重跑，已完成的不会重复）：

```bash
.venv/bin/python run.py all
```

每日增量（列表翻到没有新公告就停）：

```bash
.venv/bin/python run.py all --incremental
```

试跑：每个来源只翻 1 页、每个来源只处理 3 条：

```bash
.venv/bin/python run.py list --max-pages 1
```

```bash
.venv/bin/python run.py detail --limit 3 --per-source
```

分步命令：`list` → `detail` → `download` → `convert` → `match` → `export` → `coverage`，都可以用 `--sources a,b` 只跑部分来源。下载失败的附件用 `download --retry-failed` 重试。

## 输出

```
output/苏州市/
  按审批机关/{审批机关（区县）}/{项目名}/
      S1_报告表_受理公示版_2026-09-14.pdf
      S3_批复_苏环建〔2026〕85第145号.pdf
  projects.csv          项目总表（报告/批复路径、日期、文号、来源公告链接）
  baidupan_links.csv    百度网盘链接 + 提取码
  coverage.md           各来源公告数与日期范围、附件状态、按年份的报告/批复匹配率、未识别标题
data/
  eia.sqlite            公告、项目行、附件、网盘链接、项目
  raw/                  原始详情页（HTML/JSON）存档
  files/                原始附件（按 sha256 去重）与转换后的 PDF
  logs/run.log
```

## 阶段标签

| 标签 | 含义 | 取的文件 | 报告优先级 |
|-|-|-|-|
| S0 | 报批前全本公示 / 第二次公示 | 报告全文（仅存档） | 不取 |
| S1 | 受理公示 | 报告书（表）公示版 | 3（兜底，`report_is_fallback=1`） |
| S1S2 | 受理+拟审批合并公示（公告表里仍记 S1，`stage_reason='受理+拟审批合并公示'`） | 报告 | 2 |
| S2 | 拟审批公示 | 附件里的报告（多数没有） | 2 |
| S3 | 审批决定公告 / 批复 | 批复；随附报告 | 1 |

`S1S2` 只出现在 `projects.report_stage` 和导出文件名里。`projects.report_version` 记附件名版本字样。

规则在 `crawler/classify.py`。`coverage.md` 末尾会列出规则没识别出的标题，补规则后重跑 `detail` 即可。

## 新增数据源

1. 看列表页属于哪种模板：
   - `trs`：列表是 `xxx_list.shtml` / `xxx_list_N.shtml`，页面里有 `createPageHTML(...)`
   - `paged`：页码在查询参数里，如 `?page=N`
   - 其他（接口型、JS 渲染）：在 `crawler/adapters/` 下加一个模块，实现 `iter_pages()`，按需实现 `fetch_detail()` / `download()`，参考 `sipac.py`
2. 在 `sources.yaml` 加一行，写好 `link_pattern`（只匹配本栏目详情页）；栏目里混着非环评公告时加 `mixed: true`
3. 试跑：`run.py list --sources 新id --max-pages 1`，再 `run.py detail --sources 新id --limit 5`，看 `coverage.md`

## 已知限制

- 苏州市生态环境局列表深度有限：受理公示只到 2024-10，拟审批到 2023-01，批准公告到 2021-08。更早的报告需要从开发区站点、jshbgz.cn 全本公示等处补
- 站点有限流：默认每个域名至少间隔 3 秒，失败时整个域名退避（15s→45s→120s→300s），4xx 直接判失败
- rar/7z 暂不解压（记为 unsupported）；张家港的批复是正文图片，合并成 PDF 但没有 OCR 文字层
