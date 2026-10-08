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


class MasterDataTest(unittest.TestCase):
    """主数据表的参照文件：入库快照与维表，无 raw/ 也要能跑。"""

    def test_zone_district_derived_matches_legacy(self):
        """ZONE_DISTRICT 改成从 refs/zones_*.csv 派生，必须与原字面量逐条相等。"""
        for prov, legacy in fcs._ZONE_DISTRICT_LEGACY.items():
            self.assertEqual(fcs.load_zone_district(prov), legacy, prov)

    def test_zone_district_keys_still_produced(self):
        """ZONE_DISTRICT 的键是清洗产物的键。

        改了 zone_in() 之后某个键不再产生时，那些单位只会悄悄掉进「无区县代码」清单，
        不报错也不告警。这条断言把静默失效变成红灯。
        """
        rows = read_csv(ROOT / "江苏省环评审批机构.csv")
        produced = {(r["城市"], r["地区"]) for r in rows}
        missing = sorted(set(fcs.ZONE_DISTRICT["320000"]) - produced)
        self.assertEqual(missing, [], f"这些映射键已不再由清洗产生：{missing}")

    def test_area_tree_snapshot_matches_raw(self):
        """入库快照必须与运行时抓的地区树一致，否则下游区划码会悄悄漂移。"""
        if not HAS_RAW:
            self.skipTest(f"没有原始记录 {RAW}")
        snap = fcs.load_tree("320000", ROOT / "refs" / "__absent__")
        live = fcs.load_tree("320000", RAW)
        self.assertEqual(snap, live)

    def test_zone_registry_covers_all_zone_keys(self):
        """开发区维表要盖住清洗产出的每一个开发区/园区键，否则主表会漏挂片区。"""
        tree = fcs.load_tree("320000")
        cities = {m["名称"] for m in tree["市"]}
        shorts = {(m["名称"], q["简称"]) for m in tree["市"] for q in m.get("区县", [])}
        cores = {(c, fcs.core(s)) for c, s in shorts}
        zones = {(r["city"], r["source_label"])
                 for r in read_csv(ROOT / "refs" / "zones_320000.csv")}
        missing = set()
        for r in read_csv(ROOT / "江苏省环评单位.csv"):
            key = (r["城市"], r["地区"])
            if r["地区"] in ("市本级", "省级", "国家") or key in shorts or key in cores:
                continue
            if r["城市"] in cities and key not in zones:
                missing.add(key)
        self.assertEqual(sorted(missing), [], f"维表缺这些片区键：{sorted(missing)}")


class NoticeResolveTest(unittest.TestCase):
    """受理公示的解析规则。用例取自南京 300 条 / 常州栏目的真实写法，离线跑。

    规则顺序是硬要求：先判辐射再判地区。南京市局栏目 300 条里，标题括号有三种语义
    混着——发文机关 166、业务类别（辐射）79、项目属地 34——只取括号会造出一个
    叫「辐射」的区县。
    """

    @classmethod
    def setUpClass(cls):
        import resolve_notice as rn
        cls.rn = rn
        cls.tree = fcs.load_tree("320000")
        cls.places = cj.Places(cls.tree)
        cls.city = {c["名称"]: c for c in cls.tree["市"]}

    def cls_(self, city, title, src, locs, org=""):
        return self.rn.classify(title, src, locs, self.city[city], self.places, org)

    def test_bracket_is_publisher(self):
        """括号=发文机关：来源解析出的区与括号一致。"""
        r = self.cls_("南京市",
                      "南京市生态环境局关于2026年9月23日建设项目环境影响评价文件 受理情况的公示（玄武）",
                      "南京市玄武生态环境局", ["江苏省南京市玄武区龙蟠路9号兴隆大厦负一、三、四层"])
        self.assertEqual(r["bracket_meaning"], "发文机关")
        self.assertEqual(r["approval_district"], "玄武")
        self.assertEqual(r["project_districts"], ["玄武"])

    def test_bracket_is_project_location(self):
        """括号=项目属地：市局代发，审批地是市本级、项目地是高淳。高淳 29 条全是这种。"""
        r = self.cls_("南京市",
                      "南京市生态环境局关于2025年8月1日建设项目环境影响评价文件受理情况的公示（高淳）",
                      "南京市生态环境局", ["南京市高淳区古柏街道"])
        self.assertEqual(r["bracket_meaning"], "项目属地")
        self.assertEqual(r["approval_district"], "市本级")
        self.assertEqual(r["project_districts"], ["高淳"])

    def test_radiation_is_not_a_district(self):
        """括号=业务类别：辐射必须先判，否则会被当成区县名。"""
        r = self.cls_("南京市",
                      "2026年09月17日南京市生态环境局受理建设项目环评文件情况公示(辐射)",
                      "南京市生态环境局", ["南京江北新区浦滨路"])
        self.assertTrue(r["radiation"])
        self.assertEqual(r["bracket_meaning"], "业务类别")
        self.assertEqual(r["project_districts"], ["江北新区"])

    def test_transformer_project_counts_as_radiation(self):
        """输变电/千伏是辐射类最常见的写法，标题没写「辐射」也要认出来。"""
        r = self.cls_("南京市",
                      "南京市生态环境局关于220千伏送出工程环境影响评价文件受理情况的公示",
                      "南京市生态环境局", [])
        self.assertTrue(r["radiation"])

    def test_channel_org_fills_placeless_source(self):
        """常州页面的「来源：生态环境局」不含地名，只能靠栏目归属机构补全。"""
        bare = self.cls_("常州市", "关于2026年9月18日建设项目生态环境影响评价文件受理情况的公示",
                         "生态环境局", ["经开区"])
        self.assertEqual(bare["approval_district"], "")
        filled = self.cls_("常州市", "关于2026年9月18日建设项目生态环境影响评价文件受理情况的公示",
                           "生态环境局", ["经开区"], org="常州市生态环境局")
        self.assertEqual(filled["approval_district"], "市本级")
        self.assertEqual(filled["bracket_meaning"], "无")

    def test_approval_and_project_district_differ(self):
        """常州整栏都是市局审批、项目散在各区县，两者必须分开存。"""
        r = self.cls_("常州市", "关于2026年9月10日建设项目生态环境影响评价文件受理情况的公示",
                      "生态环境局", ["溧阳市天目湖镇", "金坛区尧塘街道"], org="常州市生态环境局")
        self.assertEqual(r["approval_district"], "市本级")
        self.assertEqual(r["project_districts"], ["溧阳", "金坛"])


class ReportAttachmentTest(unittest.TestCase):
    """report_stage_probe.is_report：公示附件是不是环评报告正文。

    用例全部取自 2026-10-08 浙江、安徽实际抓到的附件名。这条规则决定
    「某省是不是只有受理阶段拿得到全本」的结论，误收批文会把审批决定阶段虚报成有报告。
    """

    @classmethod
    def setUpClass(cls):
        sys.path.insert(0, str(ROOT / "scripts"))
        from report_stage_probe import is_report
        cls.is_report = staticmethod(is_report)

    def test_decision_documents_are_not_reports(self):
        """审查意见、批文文号、「…的函」文件名里都带报告书全名，只看「报告书」会全部误收。"""
        for name in ("湖环建〔2026〕36号-关于浙江新盈电子材料有限公司年产730吨光刻胶原料项目环境影响报告书的审查意见.pdf",
                     "关于温州振先环保科技有限公司污水处理中心资源化利用技改项目环境影响报告表审批意见的函.pdf",
                     "台环建（新）〔2026〕25号关于台州市椒江海门橡胶三厂建设项目生态环境影响报告表的许可决定书.pdf",
                     "杭环钱评批〔2026〕58号-浙江明日特灵新材料有限公司年产3万吨新材料共混改性项目.docx"):
            self.assertFalse(self.is_report(name, 0), name)

    def test_side_materials_are_not_reports(self):
        for name in ("建设项目概况、主要环境影响及预防或减轻不良影响的对策或措施.docx",
                     "环境影响评价公众参与说明.pdf"):
            self.assertFalse(self.is_report(name, 0), name)

    def test_report_naming_variants(self):
        """各市对全本的叫法：公示稿、环评报告、环评文本、项目名直接当文件名，公参说明和报告打包。"""
        for name in ("吴兴区埭溪镇共富羊场建设项目报告书公示稿及公众参与说明.zip",
                     "环评报告-金华市乙顺再生资源回收有限公司-公示稿.pdf",
                     "【环评文本】安徽氟瑞星化工有限公司年产5万吨化学纯氢氟酸扩产项目.docx",
                     "马鞍山中粮生物化学有限公司5000吨／年β-环糊精项目.pdf",
                     "公示-浙江明日特灵新材料有限公司年产3万吨新材料共混改性项目生态环境影响报告表.pdf",
                     "（公示文本）浙江奋斗实业有限公司.pdf"):
            self.assertTrue(self.is_report(name, 0), name)

    def test_unnamed_attachment_needs_page_count(self):
        """宁波附件只叫「附件1」：审批决定的是 8 页扫描批文（1.6MB），受理的是 456 页报告书。
        只凭大小会把批文判成报告，所以大文件还要数页。"""
        self.assertFalse(self.is_report("附件1", 1_600_000, lambda: 8))
        self.assertTrue(self.is_report("附件1", 35_400_000, lambda: 456))
        self.assertFalse(self.is_report("附件1", 900_000, lambda: 456))   # 太小的根本不去数
