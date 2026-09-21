"""离线回归：不请求 i-ESG、不写飞书。

原始记录 raw/ 不入库，路径由环境变量 EIA_RAW_DIR 指定（scripts/dev.py 会设成当前检出的 raw/）；
没有原始记录时，依赖它的测试跳过。
"""
import csv
import os
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import clean_jiangsu_eia as cj  # noqa: E402
import fill_compare_sheet as fcs  # noqa: E402

RAW = Path(os.environ.get("EIA_RAW_DIR") or ROOT / "raw")
HAS_RAW = bool(sorted(RAW.glob("32*.json")))


def read_csv(path):
    with open(path, encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


class RoleTest(unittest.TestCase):
    """审批机构口径（2026-09-17 与用户确认）。"""

    def test_roles(self):
        cases = {
            "扬州市邗江生态环境局": "生态环境",
            "经开区分局": "生态环境",               # 生态环境局派出分局的简写
            "淮安市生态环局": "生态环境",           # 源数据漏字
            "张家港市安环局": "生态环境",
            "镇江市生态环境和应急管理局": "生态环境",
            "无锡市大数据管理局": "行政审批",
            "泰州市姜堰区政务服务大厅": "行政审批",
            "江苏常州经济开发区管理委员会": "开发区管委会",
            "淮安工业园区": "开发区管委会",         # 园区名本身当发布主体
            "扬州市邗江区司法局": "其他部门",
            "泰州市水利局医药高新区分局": "其他部门",  # 前面带了别的局名，不是生态环境分局
            "安徽自贸试验区蚌埠片区改革创新局": "行政审批",   # 安徽自贸区/高新区承接审批的机构
            "定远县数据资源管理局": "行政审批",
            "驻合肥市政务服务管理局生态环境窗口": "生态环境",
            "安庆市公安局森林分局": "其他部门",
            "江苏省扬州市宝应县人民政府": "其他部门",
        }
        for name, role in cases.items():
            with self.subTest(name=name):
                self.assertEqual(cj.role_of(name), role)


class PlaceRuleTest(unittest.TestCase):
    """地区判定的几条规则，用一棵手写的小地区树，不依赖抓来的原始记录。"""

    TREE = {
        "编码": "340000", "名称": "安徽省", "简称": "安徽",
        "市": [
            {"编码": "340100", "名称": "合肥市", "简称": "合肥",
             "区县": [{"编码": "340111", "名称": "包河区", "简称": "包河"},
                     {"编码": "340124", "名称": "庐江县", "简称": "庐江"}]},
            {"编码": "340500", "名称": "马鞍山市", "简称": "马鞍山",
             "区县": [{"编码": "340523", "名称": "和县", "简称": "和"}]},
            {"编码": "340400", "名称": "淮南市", "简称": "淮南", "区县": []},
        ],
    }

    @classmethod
    def setUpClass(cls):
        cls.p = cj.Places(cls.TREE)

    def test_single_char_district_needs_suffix(self):
        """单字区县名（和县）必须连着县/区/市：否则「住房和城乡建设局」里的「和」会被当成和县。"""
        self.assertEqual(self.p.area_of("马鞍山市和县生态环境分局", "340500"), ("马鞍山市", "和"))
        self.assertEqual(self.p.area_of("马鞍山市住房和城乡建设局", "340500"), ("马鞍山市", "市本级"))

    def test_station_prefix_not_a_place(self):
        """「驻合肥市…」的驻字不能让地区前缀判定把它当成外地单位。"""
        name = "驻合肥市政务服务管理局生态环境"
        self.assertFalse(self.p.is_misplaced(name))
        self.assertEqual(self.p.area_of(name, "340100"), ("合肥市", "市本级"))
        self.assertTrue(self.p.is_misplaced("郑州市生态环境局"))

    def test_window_uses_district_field(self):
        """派驻窗口式写法没有地名，用记录的区县字段定位；裸名仍然认不出地区。"""
        self.assertEqual(self.p.area_of("行政服务中心环保局", "340100", "庐江"), ("合肥市", "庐江"))
        self.assertIsNone(self.p.area_of("行政服务中心环保局", "340100", ""))
        self.assertIsNone(self.p.area_of("生态环境分局", "340100", "庐江"))
        # 区县字段指向别的市（站点标错）时不认
        self.assertIsNone(self.p.area_of("行政服务中心环保局", "340400", "庐江"))

    def test_extra_zone(self):
        """地区树里没有的管理区，按省编码登记后才认（安徽毛集实验区）。"""
        self.assertEqual(self.p.area_of("毛集实验区环保局", "340400"), ("淮南市", "毛集实验区"))
        self.assertIsNone(cj.Places({**self.TREE, "编码": "999999"}).area_of("毛集实验区环保局", "340400"))


@unittest.skipUnless(HAS_RAW, f"没有原始记录 {RAW}")
class JiangsuRegressionTest(unittest.TestCase):
    """代码与入库的江苏交付物必须一致：改规则导致结果变化时，要同步更新 CSV 并能解释差异。"""

    @classmethod
    def setUpClass(cls):
        cls.expected = {n: (ROOT / n).read_bytes() for n in ("江苏省环评单位.csv", "江苏省环评审批机构.csv")}
        subprocess.run([sys.executable, "merge_jiangsu.py", "--province", "320000", "--raw-dir", str(RAW)],
                       cwd=ROOT, check=True, stdout=subprocess.DEVNULL)
        cls.units = read_csv(ROOT / "江苏省环评单位.csv")

    @classmethod
    def tearDownClass(cls):
        for name, data in cls.expected.items():  # 在工作区直接跑测试时也不留下改动
            (ROOT / name).write_bytes(data)

    def test_outputs_unchanged(self):
        for name, data in self.expected.items():
            with self.subTest(file=name):
                self.assertEqual((ROOT / name).read_bytes(), data, f"{name} 与入库版本不一致")

    def test_counts(self):
        self.assertEqual(len(self.units), 334)
        self.assertEqual(sum(1 for u in self.units if u["职能"] != "其他部门"), 244)
        self.assertEqual(sum(int(u["记录数"]) for u in self.units), 84168)

    def test_nanjing_reference(self):
        """南京专用版的 20 个单位，在全省结果里必须逐个同名出现（18 个南京 + 2 个省级/国家）。"""
        ref = {r["受理单位"] for r in read_csv(ROOT / "南京市环评受理单位.csv")}
        reps = {u["单位"] for u in self.units}
        self.assertEqual(len(ref), 20)
        self.assertEqual(ref - reps, set())
        self.assertEqual(sum(1 for u in self.units if u["城市"] == "南京市"), 18)

    def test_same_name_city_district(self):
        """淮安市 / 淮安区同名：不先剥市名前缀会把全市并成一条。"""
        huaian = [u for u in self.units if u["城市"] == "淮安市"]
        self.assertEqual(len(huaian), 32)
        self.assertLess(max(int(u["记录数"]) for u in huaian), 2000)


@unittest.skipUnless(HAS_RAW, f"没有原始记录 {RAW}")
class CompareSheetPlanTest(unittest.TestCase):
    """飞书比对表的布局计划（离线，用地区树模拟一张空白模板）。"""

    def test_plan(self):
        tree = fcs.load_tree("320000", RAW)
        rows = [["行政区划代码", "省", "市", "区县", *[""] * 8], [tree["编码"], "江苏", "", "", *[""] * 8]]
        for c in tree["市"]:
            rows.append([c["编码"], "江苏", c["简称"], "", *[""] * 8])
            rows += [[d["编码"], "江苏", c["简称"], d["简称"], *[""] * 8] for d in c["区县"]]
        _, codes, layout, unmatched, no_unit, not_in_tree = fcs.plan(rows, "320000", RAW)
        self.assertEqual(len(codes), 109)
        self.assertEqual(len(layout), 163)
        self.assertEqual(len(unmatched), 85)
        self.assertEqual(sorted(no_unit), sorted(["徐州鼓楼", "徐州云龙", "徐州泉山", "宿迁宿城"]))
        self.assertEqual(not_in_tree, [])
        # 同一代码下按记录数从多到少，新插的行 A–D 与首行一致
        first = {}
        for is_new, row in layout:
            if is_new:
                self.assertEqual(row[:4], first[row[0]][:4])
            else:
                first[row[0]] = row


if __name__ == "__main__":
    unittest.main()
