# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 这个分支是什么

从青绿数据（i-esg.com）抓环评受理列表的「受理单位」列，清洗成各省的环评审批机构清单，再填飞书比对表、查官网入口。
江苏已完整跑通，**换省照 `docs/新省份操作手册.md` 走，代码不用改**。抓取和清洗永远是两个脚本。

```
crawl_jiangsu_eia.py --tree-only → trees/<省>.json → gen_city_scripts.py → cities/crawl_*.py, cities/clean_*.py
cities/crawl_<市>.py → raw/<市>.json → cities/clean_<市>.py → by_city/<市>.csv      逐市核对
raw/<省前两位>*.json → merge_jiangsu.py --province → <省>环评单位.csv（完整，带职能列）
                                                   + <省>环评审批机构.csv（交付口径）
<省>环评审批机构.csv → fill_compare_sheet.py → 飞书比对子表（一单位一行）
entries/<市>.csv → build_entries_xlsx.py → <省>环评单位_官网及公示入口.xlsx（目前写死江苏）
```

文件名带 `jiangsu` 是历史原因，引擎按 `--province` 通用。`cities/` 是生成的薄封装，**改规则只改
`crawl_jiangsu_eia.py` / `clean_jiangsu_eia.py`**，不要改封装。`crawl_nanjing_eia.py` / `clean_nanjing_eia.py`
是南京专用的旧版，只作回归参照，不再扩展。main 分支上的苏州采集工作流、`nanjing_departments_workflow.py`
在本分支被有意删除，需要时去 main 看。

## 深入文档

| 文档 | 内容 |
|---|---|
| `docs/新省份操作手册.md` | 换省八步、检查点、与需求方确认过的口径、还没通用的地方 |
| `docs/江苏试点记录.md` | 江苏各市数据量、飞书比对结果、入口核实结果、与 main 工作流的口径差异 |

## 命令

`.venv` 是指向主检出 `/Users/fish/环艾/.venv` 的符号链接，依赖 `httpx`、`pycryptodome`（走代理加 `socksio`，或 `--no-proxy`）。

```bash
.venv/bin/python cities/crawl_320500_苏州市.py --prompt-token   # 逐市抓，可续跑
.venv/bin/python merge_jiangsu.py --province 320000            # 全省合并
.venv/bin/python merge_jiangsu.py --field export               # 受理/监督单位字段口径
.venv/bin/python fill_compare_sheet.py --url <飞书> --sheet-id <id>   # 先出计划，加 --run 才写
.venv/bin/python scripts/dev.py check                     # lint + 离线回归测试
```

## 分支保护与改动流程

远端 `origin` = https://github.com/FishShaw/HuanAi_CrawlANDCleanForQingLv （私有）。
`main` 和 `feature/nanjing-eia-depts`（本流水线的主干）在 GitHub 上都开了分支保护：
**必须走 PR，禁止直推、强推和删除分支，管理员同样受限**（需要审批数 0，可以自己合自己的 PR）。
本地还有一层钩子：主干上的直接提交会被 pre-commit 拦掉。

```bash
.venv/bin/python scripts/dev.py start add-zhejiang     # 从主干开 eia/add-zhejiang，要求工作区干净
git add <文件> && git commit -m "..."                   # 钩子对暂存快照跑 ruff
.venv/bin/python scripts/dev.py check                  # 推之前自己先跑 lint + 离线回归
git push -u origin eia/add-zhejiang
gh pr create --base feature/nanjing-eia-depts --fill    # 在 GitHub 上合并，然后 git pull 回来
```

- **不要在本地合并主干再推**：本地合并提交推不上去（保护要求 PR），只能在 GitHub 上合。
  `scripts/dev.py merge` 只在没有远端时才有意义，留着作为离线兜底。
- 钩子在 `.githooks/`，靠 `core.hooksPath=.githooks`；新 worktree / 克隆后跑一次 `scripts/dev.py setup`。
- 测试在 `tests/test_pipeline.py`，离线（不请求 i-ESG、不写飞书），`raw/` 从当前检出读取；没有 `raw/` 时依赖它的测试跳过。
  **GitHub 上没有 CI**，PR 不会自动跑测试，合并前自己跑 `dev.py check`。
- **改清洗规则导致江苏 CSV 变化时**，测试会失败：确认差异都能逐条解释后，把新的 CSV 和测试里的数字一起提交。
- 本地钩子能被 `--no-verify` 绕过，GitHub 的保护不能——远端才是真正的闸门。

## 接口（逆向得到，站点无公开文档）

所有数据走 `POST https://www.i-esg.com/environment/ep-query/doAction`，请求体 `{"data": "<密文>"}`：
AES-128-**ECB**、PKCS7、密钥 `DaoGuangJianYing`，明文是按参数名排序的表单串，真实路径在 `url` 参数里；响应明文 JSON。

- `cityCode` 按市筛选即含其下所有区县，不要再传区县码；`moduleTypeCode` 1=受理、3=审批、5=验收；`pageSize` 500 可用。
- 地区树 `/ep-query/screen/area`（省 → 市 → 区县，带 code/name/shortName），所有地名白名单从它生成，**不要手写地名**。
- 单位字段取 `processDepartment`（公告发布单位）。`acceptanceMonitorDepartmentForExport` 是受理/监督单位组合值，口径不同，不要混用。
- 带地区筛选必须登录。令牌用 `--prompt-token` 或 `I_ESG_TOKEN` 传入，**不写进脚本、不落盘、不要求用户贴进对话**。
- 错误码 100000/100002 令牌失效、100006 当日额度用完、100007 权限不足——直接停，不要重试绕过。
- **`total` 封顶一万且不报错**，只给最近一万条（苏州实测）。「累计条数 == total」挡不住它，必须按
  `startNoticeDate`/`endNoticeDate` 递归二分，换省换市都要保留。
- 按市逐个抓、不传 provinceCode：深翻页没验证过且不可续跑。同一会话第二次全量拉取会被限流到请求挂起，默认间隔 1.5 秒。
- 翻页结束按累计条数与 `total` 比，`page`/`nextPage` 字段不可靠。

## 清洗规则（`clean_jiangsu_eia.py`）

1. **标错地区删**：`PLACE_PREFIX` 取开头纯中文省市前缀，不在本省地区树里的删（只认纯中文，否则「我局(市级)」会被误判）。
2. **不规范删**：代称、残句、错字（`TYPO`）、结尾不是机构后缀（`SUFFIX`）、截断且认不出地区的。
3. **归并**：键是 (市, 区域, 职能)。区域粒度「区县 / 开发区 / 市本级 / 省级 / 国家」；职能用窄口径 `ECO`/`ADMIN`，
   其余按去掉地名后的委办局名各自成组。内设科室（`SECTION`）并入所属局。
4. **代表名**：先看是否规范机构名（`PROPER`），再比公告日期——免得「昆山市生态环境部门」压过「苏州市昆山生态环境局」。

归并只依据写法和日期，**不代表机构法定更名**（江北新区、南京经开区把管委会和下属审批局并成了一条）。

**残缺写法按证据收，没证据不猜**：记录自带区县字段 `countryName` 是证据。名称没写开发区名的
（「港区行政审批局」）才用区县定位——港区那 46 条全是太仓，不是张家港；名称写了的不按区县拆
（江北新区横跨三个区）。截断名只在同市同区县唯一对应一组完整写法时回收，区县为空的删。

**市优先认名称里写明的本省城市**，认不出才用查询的市——否则南京鼓楼和徐州鼓楼会被并掉。
名称里的区县不属于查询市时全省找**唯一**命中（「灌南县环境保护局」→连云港），「鼓楼」这种多市同名的宁可不认。

### 审批机构口径（`role_of()`，只打「职能」标签，不参与归并）

交付用 生态环境 / 行政审批（含数据局、政务服务）/ 开发区管委会，去掉交通、水务、司法、人民政府等「其他部门」。
- **不要放宽 `ECO`/`ADMIN` 来实现口径**，那会改变归并结果；口径只改 `ROLE_*`。
- 行政审批局、数据局可以是真正的审批机关（相对集中行政许可），不能按「只有生态环境局审批」删。
- 「经开区分局」「工业园区分局」是生态环境局派出分局的简写（淮安 119 条），要认成生态环境；前面带别的局名的（「水利局医药高新区分局」）不算。
- 「生态局」「生态环局」是源数据漏字；张家港「安环局」是安全环保合署机构。

## 踩过的坑（换省必查）

- **市名和区县名相同**（淮安市 / 淮安区）：不先 `strip_city_prefix()`，全市单位会被判成淮安区并成一条
  （实测一条吃掉 3535 条）。全国这种「市辖同名区」很常见。
- **开发区归一只剥省名市名，不剥区县名**：否则「江宁开发区」（区级）和「南京经济技术开发区」（市级）变同一个键。
- 开发区名在全名里搜，头部会粘机构词（「生态环境局开发区分局」），要从最后一个机构字之后截断。
- 头部只剩一个字（「州经济开发区」）是截断残留，改用区县字段；「张家港市开发区」=「张家港开发区」。
- 同市开发区互相包含时合并方向**按条数**不按长短（4 条的「州国家高新区」不能吸并 582 条的「常州国家高新区」）。
- 开发区类型要写全：度假区、保税区、港区、示范区、工业园都有，漏了会被当不规范误删。
- `SUFFIX` 放行了「司」（生态环境部辐射源安全监管司），必须 `(?<!公)司$` 排除公司。
- **全省表不能拼 `by_city/` 的 CSV**：省厅、生态环境部在每个市都出现会重复计数，跨市错标也要全省一起看。

## 飞书比对表与官网入口

- `fill_compare_sheet.py` 只在表格是空白模板或已与本地一致时写；**有人手工改过就拒绝**，不要绕过去覆盖。
- 开发区挂到区县必须是需求方逐条确认过的，写进 `ZONE_DISTRICT`；没确认的列进「无区县代码」清单，不自己归。
- 飞书写入用 `lark-cli --as user`；`+cells-set` 写空值不会清掉原值。
- `entries/` 按 **(城市, 单位)** 索引（「高新区行政审批局」在南通、泰州、宿迁是三个不同单位）。
- 核验情况四种：已核实（打开栏目页看到环评条目）/ 未核实（只来自搜索）/ 访问受限（403）/ 无独立环评栏目。
  **看起来像不算已核实**，依据写进备注。各市体制差异很大，不要套用上一个市的模式。
