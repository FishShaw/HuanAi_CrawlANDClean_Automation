# 列表页取链接

`collect.py` 是入口：给一个受理栏目地址，它按页面指纹认出建站系统，把整栏的
`URL\t标题` 打到 stdout。认不出就当静态页，拿第 2 页去试一串候选 URL 模板。

```bash
.venv/bin/python scripts/list_api/collect.py '<栏目URL>' > /tmp/x.tsv
grep -P '\t.*受理' /tmp/x.tsv > /tmp/sel.tsv
.venv/bin/python scripts/verify_notice_parsing.py --city 盐城市 --links-file /tmp/sel.tsv \
    --entry-url '<栏目URL>' --org '盐城市阜宁生态环境局' --projects 江苏省环评受理数据_样本.csv
```

`verify_notice_parsing.py` 的 `CHANNELS` 只登记了 13 个市的市局栏目；区县门户和省厅栏目
靠 `--entry-url` + `--org` 传进来，`--city` 只用来选地区树上下文。

## 五种家族和各自的坑

| 家族 | 用在 | 翻页 | 坑 |
|---|---|---|---|
| `jpaas` | 泰州、扬州及其区县 | `paramJson={"pageNo":N,"pageSize":"15"}` | 裸 `pageNo`/`page`/`currentPage` 全部无效 |
| `dahan` | 盐城系、省厅 | `page` + `perpage` | `startrecord` 被忽略；`perpage` 超 300 会截断，压到 100 |
| `xxgk` | 盐都 | POST `search.jsp` 的 `currpage` | 不按栏目过滤，要按 `art_<栏目号>_` 收窄 |
| `truecms` | 南通系、连云港徐圩 | `startrecord`/`endrecord` | 每站 `perpage` 上限不同，要先协商；如皋不带 Referer 回 404 |
| `ewb` | 徐州系 | POST `pageIndex` | **https 整站 403，只有 http 通**；`siteGuid` 在 `/js/webBuilderCommon.js` |

静态页还有两条约束，少一条就会抓错：

- **翻页只收入口自己路径前缀下的链接**。常州新北的 `/class/XXXX/2` 返回的是首页新闻，
  沭阳的 `xxgk_list_N.shtml` 会串到统计局、城管局的公开目录去。
- **前缀取入口 URL 自己的，不取页面上占多数的**。虎丘那页顶上挂着「批准项目公告」的最新条目，
  按多数算会把入口所属的 `hjbh` 栏目整个滤掉。

壳页会自动跟进同域 iframe（常州钟楼、常州开发区的栏目页只是个 iframe 壳）。

单家族的旧脚本 `jpaas.py` / `dahan.py` / `truecms.py` / `xuzhou.py` 留着作对照，
新活一律走 `collect.py`。
