"""汇总 report_stage_probe.py 的输出：每个 (省, 市, 阶段) 抽了几条、几条挂着报告全本。"""
import collections
import csv
import sys

rows = list(csv.DictReader(open(sys.argv[1], encoding="utf-8"), delimiter="\t"))
agg = collections.defaultdict(lambda: [0, 0, 0, 0, 0])
bad = 0
for r in rows:
    if not (r.get("全部附件数") or "").isdigit():
        bad += 1     # 列被挤歪的行，宁可丢掉也不让它污染计数
        continue
    k = (r["省"], r["市"], r["阶段"])
    a = agg[k]
    a[0] += 1
    a[1] += int(r["官方报告附件数"]) > 0
    a[2] += int(r["非官方报告附件数"]) > 0
    a[3] += int(r["全部附件数"]) > 0
    a[4] += bool(r["错误"])
if bad:
    print(f"（跳过 {bad} 行列数不齐的记录）")
order = {"受理": 0, "拟审批": 1, "审批决定": 2}
print(f"{'省':<4}{'市':<7}{'阶段':<6}{'抽样':>4}{'官方挂报告':>8}{'外链报告':>6}{'有任何附件':>8}{'打不开':>5}")
for (p, c, s), (n, off, ext, anyatt, err) in sorted(agg.items(), key=lambda x: (x[0][0], x[0][1], order.get(x[0][2], 9))):
    print(f"{p:<4}{c:<7}{s:<6}{n:>4}{off:>8}{ext:>6}{anyatt:>8}{err:>5}")
tot = collections.defaultdict(lambda: [0, 0])
for (p, c, s), v in agg.items():
    tot[(p, s)][0] += v[0]
    tot[(p, s)][1] += v[1]
print()
for (p, s), (n, off) in sorted(tot.items(), key=lambda x: (x[0][0], order.get(x[0][1], 9))):
    print(f"{p} {s:<6} 合计 {off}/{n} = {off / n * 100:.0f}% 挂官方报告" if n else "")
