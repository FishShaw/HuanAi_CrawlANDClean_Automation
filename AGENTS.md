# 本项目工作记忆

## 记忆维护约定

- 用户要求：今后本项目的持续记忆统一更新到本文件 `AGENTS.md`，不另建 `AGENT.md` 或独立记忆文档。
- `AGENTS.md` 是 Codex 自动读取的项目指令文件；本项目同时用它维护任务状态、已修复问题和操作边界。
- 用户运行说明放在 `README.md`。本文件记录长期记忆，避免重复维护多份记忆。
- 南京任务唯一入口为 `nanjing_departments_workflow.py`，不要恢复已删除的旧脚本或旧字段假设。
- 原始缓存是必要输入，保留；默认不请求 i-ESG；每次只交付最终 XLSX，临时导出文件自动清理。
- 苏州任务独立存在，南京任务修改和清理不得删除苏州代码、数据、文档和产出。

## 南京单位官网及环评入口

更新日期：2026-09-15。

## 一步运行

```bash
cd /Users/fish/环艾
.venv/bin/python nanjing_departments_workflow.py --limit 0
```

仅需要运行一个Python文件。默认流水线：

完整城市分页缓存 → 提取 acceptanceMonitorDepartmentForExport → 拆分组合值及精确去重 → 关联已核验的官方入口 → 检测公开URL → 按注明的名称映射归并 → 检查工作簿 → 原子替换最终XLSX。

固定默认输出（保留用户指定路径，文件名中的“前10条”不再代表本次测试数量）：

`/Users/fish/环艾/outputs/01a0a2a2-3f7d-7d53-8851-448cff380771/南京市审批单位_官网及环评入口_前10条测试.xlsx`

工作簿有两个表：

- `单位入口测试`：120个原始单位，保留原文、规范/关联名称、处理方式、主页、报告入口、审批入口、来源、三类HTTP检测结果及核验日期。2026-09-15 已按用户要求补齐三类URL列。
- `归并入口`：按当前映射得到43组，将所有原始写法、说明和来源保留在同一行。部分为辖区兜底入口或跨地区官网入口，不能把43组都当作逐条人工核验的独立法定机构。

## 本次全量单位重建结果

2026-09-15 已按当前规则运行：

```bash
.venv/bin/python nanjing_departments_workflow.py --limit 0
```

正式工作簿已原子替换到默认输出路径。此次重建使用完整受理城市缓存，不请求 i-ESG；缓存校验为7070条、7070条唯一内容、142页。

| 检查 | 结果 |
| --- | --- |
| 数据源 | 已保存的环评受理城市缓存，142页，7070条 |
| 原始非空组合值 | 305个 |
| 拆分后原始单位 | 120个 |
| 本次原名映射 | 120/120 |
| 官方主页或机构栏目 | 120条 |
| 环评报告书（表）入口 | 120条 |
| 环评审批决定入口 | 120条 |
| 不同待检测URL | 83个 |
| 本次直接HTTP取得内容 | 13个 |
| 其他URL状态 | 多数为HTTP 403或网络错误；如实写入检测列 |
| 内置回归测试 | 33项通过 |
| Excel校验 | ZIP有效；第1表121行含表头且E/F/G三列无空；第2表44行含表头 |

程序保留最早人工查证过的20条入口登记，并新增补充映射规则：南京区县名称关联南京市生态环境局派出局公开栏目，开发区名称关联对应开发区主页及辖区/专题环评栏目，外地或省级名称关联对应官方生态环境入口或主管部门主页。`--limit 0`会把120个原始单位全部写入并补齐三类URL，但补充入口中包含辖区兜底和跨地区入口，不能等同于每条机构法定职责逐项核验。

本次用官方网页检索确认了栏目及机构介绍，没有验证全部报告下载、栏目历史覆盖或所有浏览器的当前可访问性。部分单位使用上级官网/机构栏目。公安、消防、文物、发改、水务五条没有找到对应环评栏目，相关列留空。

## 常用命令

完全离线全量单位重建，保留登记的来源，访问检测列标记本次未检测：

```bash
.venv/bin/python nanjing_departments_workflow.py --limit 0 --offline
```

回归测试（不访问网站，不改正式文件）：

```bash
.venv/bin/python nanjing_departments_workflow.py --self-test
```

另存文件：

```bash
.venv/bin/python nanjing_departments_workflow.py --limit 20 --output /Users/fish/环艾/outputs/20条测试.xlsx
```

账号额度恢复后才需要在线刷新。以下命令会提示隐藏输入令牌：

```bash
.venv/bin/python nanjing_departments_workflow.py --refresh --prompt-token --limit 20
```

令牌来自本人登录网站后 Network 中列表 doAction 请求的 Authorization。也可通过环境变量 `I_ESG_TOKEN` 提供；不写入脚本、缓存或文档。官网URL检测使用另一个不带令牌的客户端。

默认只取受理模块1。`--modules 1,3,5`才要求三个模块；未取全任一模块即停止。原审批模块缓存仅有800条，不能冒充全量。

## 依赖与保留的数据

- Python 3.10以上；现有 `.venv` 已可用。新环境安装 `httpx pycryptodome`。
- Excel导出使用Codex自带Node和 `@oai/artifact-tool`，导出代码已嵌入Python文件，运行时自动建立临时环境并清理，不需要单独运行mjs。
- 脚本是单一入口，不是零依赖文件。迁移到另一台机器需准备同样的Excel运行库，通过 `EIA_NODE`、`EIA_NODE_MODULES` 指定位置。
- `.nanjing_departments_cache/` 是必要的原始输入与断点资料，不能当作无用副产物删除。默认读 `1-320100-50.json`，它可恢复此前7070条CSV与305条组合值CSV。
- 区县缓存和未完成的审批缓存继续保留，避免丢失已消耗账号额度获得的数据。正常重建不会读取未完成的审批模块。
- 在线区县最终复核成功后会写入与城市缓存哈希绑定的 `.scope.json`，保留额外补查记录，供后续离线重建使用。此次旧城市缓存没有此凭据，表中明确说明没有全区县最终复核凭据。

## 已知问题与修复记忆

1. 当前目标字段为 `acceptanceMonitorDepartmentForExport`。页面单位列曾用 `processDepartment`，两者不可混用。缺少目标字段或类型异常要停止。
2. 城市查询只传 `cityCode=320100`；区县查询只传 `countryCode`，不同时提交父级地区以免筛选扩成全省。先核对11区首批记录，再抓全城。
3. 地名兼容“江苏/江苏省”“南京/南京市”“玄武/玄武区”等已核实简称，拒绝无锡等其他城市，不接受未知区县为指定区县。
4. 接口没有公告级id。默认用规范化整行JSON的SHA-256，不把 projectId 当公告唯一ID。页内重复计入服务器原始总数，内容去重；跨页重复停止。
5. 结束分页按实际累计条数与total核对。`page`、`nextPage`在旧真实响应中是布尔值，末页也可能为true，不能依靠它们无限翻页。
6. 检查缺页、异常空页、总数变化、首尾页变动、地区不符和结构变化。线上缓存先校验首尾再复用；离线完整性不等于网站当前快照完整性。
7. 100006表示当日访问额度限制，100007表示权限不足。401/403/429直接停止，不换账号绕过。临时网络/服务错误最多重试3次，每次请求至少间隔1秒。
8. 多单位单元格按英文/中文逗号、顿号、分号和换行拆分，去首尾空白和空占位符，精确去重。旧名称和内设科室关联必须保留原文与依据，疑似错字必须注明。
9. 江北新区当前环评公告由数据局（政务服务管理办公室）发布。登记历史行政审批局的现行入口时不直接断言机构法定更名。
10. HTTP 403只表明本次程序访问受限。不得再声称“浏览器一定正常”。200还检查明显验证码/拦截页；成功取到内容也不能单独证明业务匹配。
11. 旧版COUNTIF通配符及空字符串计数出现错误。新版不再使用该汇总方式：空值写入真正空单元格，汇总按本次数据生成，导出后检查实际行数。
12. XLSX在同文件系统临时目录构建、检查后原子替换。失败保留已有正式文件。临时JSON、mjs、预览、inspect文件自动清理。
13. 在线补查的区县额外记录不能在Excel阶段丢失，已增加缓存绑定保存/重建回归测试。

## 清理范围

已被单入口替代的文件：

- `nanjing_eia_departments.py`（早期版本）
- `extract_nanjing_departments.py`、`test_extract_nanjing_departments.py`（成功采集逻辑与适用测试已迁入新文件）
- `build_department_url_test.mjs`（映射和导出逻辑已内嵌）
- `extract_nanjing_departments.md`（已删除，相关记忆迁入本文件）
- `南京市_环评受理_7070条完整明细.csv`、`acceptanceMonitorDepartmentForExport_去重.csv`（可从完整缓存重建）
- `output/南京市_环评审批单位/`内的早期CSV/TXT
- 本任务旧预览PNG、inspect结果、浏览器调试输出、旧脚本对应pyc，以及仅供旧导出器使用的根目录node_modules链接

苏州工作流 `run.py`、`crawler/`、`data/`、`sources.yaml`、相关文档及产出属于独立任务，保留。

## 本地 Git 开发流程（2026-09-16）

- 本目录已初始化本地Git，主分支 `main`。不配置远端、不上传缓存或产出。
- 新功能先执行 `.venv/bin/python scripts/dev.py start <功能名>`，从main建立 `feature/<功能名>`；要求工作区干净。
- `.githooks/pre-commit` 检查暂存快照的Ruff lint，阻止main直接提交；合并提交额外执行全部离线测试。
- `.githooks/pre-merge-commit` 保护普通非快进merge；本地配置 `merge.ff=false`。
- 推荐在main执行 `.venv/bin/python scripts/dev.py merge feature/<功能名>`，检查合并后的索引快照，失败或冲突自动abort，保留功能分支。
- `.venv/bin/python scripts/dev.py check` 运行lint、南京33项回归和 `tests/` 下测试；当前新增6项Git流程集成测试。苏州仅纳入lint，尚无专门业务回归。
- hooks随源码保存，但新克隆必须运行 `scripts/dev.py setup`；开发依赖见 `requirements-dev.txt`，Ruff固定为0.12.12。
- 本地钩子能被 `--no-verify`、显式快进或修改配置绕过，不宣称不可绕过的分支保护。
- 原始缓存、data/output/outputs、.venv、.env等已忽略，只是不纳入Git，不删除文件。
- 南京业务唯一运行入口仍是 `nanjing_departments_workflow.py`；`scripts/dev.py`仅负责开发检查及Git操作。
