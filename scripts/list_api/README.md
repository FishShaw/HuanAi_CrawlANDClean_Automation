# 列表页取链接的四套接口

`verify_notice_parsing.py` 的 `CHANNELS` 只会按静态 URL 翻页。列表由 JS 渲染的市，
先用这里的脚本导出 `URL\t标题` 文件，再 `--links-file` 喂给它。

| 脚本 | CMS | 用在 | 翻页参数 | 坑 |
|---|---|---|---|---|
| `jpaas.py` | jpaas 建站 | 泰州、扬州 | `paramJson={"pageNo":N,"pageSize":"15"}` | 裸 `pageNo`/`page`/`currentPage` 全部无效，必须包进 `paramJson` |
| `dahan.py` | 大汉版通 | 盐城、阜宁、东台 | `page=N` + `perpage` | `startrecord` 被服务端忽略；`perpage` 超过 ~300 会被截断，压到 100 |
| `truecms.py` | truecms | 南通 | `startrecord`/`endrecord` | `perpage=100` 直接回空，上限 50；响应是 jsonp 包 XML |
| `xuzhou.py` | 自研 | 徐州 | POST `pageIndex`/`pageSize` | **https 整站 403，http 正常**；浏览器也一样被挡 |

用法都是 `列表页/接口 URL → stdout 的 URL\t标题`，例如：

```bash
.venv/bin/python scripts/list_api/dahan.py 'https://jsychb.yancheng.gov.cn/col/col17849/index.html' > /tmp/yc.txt
.venv/bin/python scripts/verify_notice_parsing.py --city 盐城市 --links-file /tmp/yc.txt --projects 江苏省环评受理数据_样本.csv
```

标题为空也没关系（东台的列表不带 title 属性），详情页里能取回全名。
