import json
import tempfile
import unittest
import zipfile
from datetime import date
from pathlib import Path

from tds_filer import builder, checks, spec
from tds_filer.challan import Challan, parse_challan
from tds_filer.config import Config, Quarter, TdsError

ROOT = Path(__file__).resolve().parents[1]

CHALLAN_TEXT = """INCOME TAX DEPARTMENT
Challan Receipt
TAN : ABCD12345E
Minor Head : TDS/TCS Payable by Taxpayer (200)
Amount (in Rs.) : ₹ 31,200
CIN : 26070100000001SBIN
Date of Deposit : 01-Jul-2026
BSR code : 0001234
Challan No : 00042
1057 ₹ 30,000 ₹ 0 ₹ 1,200 ₹ 31,200
A Tax ₹ 30,000
B Surcharge ₹ 0
C Education Cess ₹ 1,200
D Interest ₹ 0
E Penalty ₹ 0
F Fee under section 427 ₹ 0
G Others ₹ 0
Total (A+B+C+D+E+F+G) ₹ 31,200
"""


def make_cfg(tmp: Path, rrr=None) -> Config:
    data = json.loads((ROOT / "src" / "tds_filer" / "tds_config.example.json").read_text())
    data["filing_root"] = str(tmp)
    data["rrr"] = rrr or {}
    p = tmp / "cfg.json"
    p.write_text(json.dumps(data))
    return Config.load(str(p))


def make_challans(qt: Quarter) -> list[Challan]:
    out = []
    for i, (y, m) in enumerate(qt.months):
        out.append(Challan(file=f"c{i}.pdf", cin=f"CIN{i}", tan="ABCD12345E", bsr="0001234",
                           challan_no=f"0000{i + 1}", date=f"01{m:02d}{y}", tax=30000, surcharge=0,
                           cess=1200, interest=0, penalty=0, fee=0, others=0, total=31200,
                           minor_head="200", section_code="1057"))
    return out


def make_spec() -> spec.Spec:
    s = spec.Spec()
    for kind, n in builder.COUNTS.items():
        s.records[kind] = {i: spec.FieldSpec(i, f"f{i}", "", None, False, False, "") for i in range(1, n + 1)}
    s.records["CD"][24] = spec.FieldSpec(24, "Interest Allocation", "INTEGER", 15, True, False, "")
    s.records["DD"][16] = spec.FieldSpec(16, "Nature of Remittance", "INTEGER", 2, False, False, "")
    s.records["DD"][28] = spec.FieldSpec(28, "Rate", "DECIMAL", 7, True, False, "")
    s.records["BH"][31] = spec.FieldSpec(31, "Filler", "NA", 0, False, True, "No value should be specified")
    s.remittance = {"1057": {"27", "99"}}
    s.countries = {"15": "AUSTRALIA"}
    s.states = {"15": "KARNATAKA"}
    return s


class QuarterTest(unittest.TestCase):
    def test_fields(self):
        q = Quarter.parse("2026-27", "Q2")
        self.assertEqual((q.fy_field, q.ay_field, q.key), ("202627", "202728", "2026-27/Q2"))
        self.assertEqual((q.start, q.end), (date(2026, 7, 1), date(2026, 9, 30)))
        self.assertEqual(q.due_date, date(2026, 10, 31))
        self.assertEqual(q.folder_label(), "2026-Q3")
        self.assertEqual(q.zip_name, "2026_2027_144RQ2.zip")
        self.assertEqual(q.previous(), Quarter(2026, 1))

    def test_q4_and_q1(self):
        q4 = Quarter.parse("2026-27", "4")
        self.assertEqual((q4.start, q4.due_date, q4.folder_label()), (date(2027, 1, 1), date(2027, 5, 31), "2027-Q1"))
        self.assertEqual(Quarter(2026, 1).previous(), Quarter(2025, 4))
        self.assertEqual(Quarter(2026, 1).folder_label(), "2026-Q2")

    def test_bad_input(self):
        for fy, q in (("2026-28", "Q1"), ("26-27", "Q1"), ("2026-27", "Q5")):
            with self.assertRaises(TdsError):
                Quarter.parse(fy, q)


class ChallanTest(unittest.TestCase):
    def test_parse(self):
        c = parse_challan(CHALLAN_TEXT)
        self.assertEqual((c.bsr, c.challan_no, c.date, c.tax, c.cess, c.total, c.minor_head, c.section_code),
                         ("0001234", "00042", "01072026", 30000, 1200, 31200, "200", "1057"))

    def test_mismatched_total(self):
        with self.assertRaises(TdsError):
            parse_challan(CHALLAN_TEXT.replace("C Education Cess ₹ 1,200", "C Education Cess ₹ 1,100"))


class BuildAndCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.qt = Quarter(2026, 2)

    def build(self, rrr=None):
        cfg = make_cfg(self.tmp, rrr)
        ch = make_challans(self.qt)
        return cfg, ch, builder.build(cfg, self.qt, ch, date(2026, 10, 31))

    def run_checks(self, cfg, ch, text):
        r = checks.Report()
        checks.check_challans(r, cfg, self.qt, ch)
        checks.check_file(r, cfg, self.qt, make_spec(), text, ch)
        return r

    def test_structure(self):
        cfg, ch, text = self.build({"2026-27/Q1": "123456789012345"})
        recs = [l.split("^") for l in text.splitlines()]
        self.assertEqual([r[1] for r in recs], ["FH", "BH", "CD", "DD", "CD", "DD", "CD", "DD"])
        for r in recs:
            self.assertEqual(len(r), builder.COUNTS[r[1]])
        bh = recs[1]
        self.assertEqual((bh[8], bh[51], bh[46], bh[17]), ("123456789012345", "Y", "93600.00", "Q2"))
        cd = recs[2]
        self.assertEqual((cd[23], cd[24], cd[11]), ("0.00", "0.00", "31200.00"))
        dd = recs[3]
        self.assertEqual((dd[15], dd[19], dd[23], dd[27]), ("99", "100000.00", "31200.00", "31.2000"))
        self.assertFalse(self.run_checks(cfg, ch, text).errors)

    def test_no_previous_rrr(self):
        cfg, ch, text = self.build()
        self.assertEqual(text.splitlines()[1].split("^")[51], "N")
        r = self.run_checks(cfg, ch, text)
        self.assertFalse(r.errors)
        self.assertTrue(any(f.area == "previous" for f in r.warnings))

    def test_missing_previous_when_history_exists_is_error(self):
        cfg, ch, text = self.build({"2025-26/Q4": "123456789012345"})
        self.assertTrue(any(f.area == "previous" for f in self.run_checks(cfg, ch, text).errors))

    def test_detects_bad_remittance(self):
        cfg, ch, text = self.build()
        bad = text.replace("^1057^99^", "^1057^08^")
        errs = self.run_checks(cfg, ch, bad).errors
        self.assertEqual(sum("nature of remittance 08" in f.msg for f in errs), 3)

    def test_detects_integer_allocation(self):
        cfg, ch, text = self.build()
        bad = text.replace("^200^0.00^0.00^", "^200^0^0^")
        self.assertTrue(self.run_checks(cfg, ch, bad).errors)

    def test_detects_total_and_rate_mismatch(self):
        cfg, ch, text = self.build()
        bad = text.replace("^100000.00^", "^70000.00^")
        msgs = [f.msg for f in self.run_checks(cfg, ch, bad).errors]
        self.assertTrue(any("TDS 31200.00 ==" in m for m in msgs))

    def test_detects_missing_month_and_tds_mismatch(self):
        cfg = make_cfg(self.tmp)
        ch = make_challans(self.qt)[:2]
        ch[0].tax = 29800
        r = checks.Report()
        checks.check_challans(r, cfg, self.qt, ch)
        msgs = " ".join(f.msg for f in r.errors)
        self.assertIn("missing", msgs)
        self.assertIn("tax+surcharge+cess 31000", msgs)

    def test_detects_wrong_field_count_and_nonempty_filler(self):
        cfg, ch, text = self.build()
        lines = text.splitlines()
        bh = lines[1].split("^")
        bh[30] = "X"
        lines[1] = "^".join(bh)
        lines[2] += "^"
        errs = [f.msg for f in self.run_checks(cfg, ch, "\n".join(lines) + "\n").errors]
        self.assertTrue(any("must be empty" in m for m in errs))
        self.assertTrue(any("31 fields" in m for m in errs))


class SpecTest(unittest.TestCase):
    def test_load_minimal_xlsx(self):
        rows = [
            ["TDS Statement (File Header Record)"], ["1", "Line Number", "NA", "INTEGER", "9", "M", "x"],
            ["TDS Statement (Batch Header Record)"], ["1", "Line Number", "INTEGER", "9", "M", "x"],
            ["2", "Filler 1", "NA", "NA", "0", "O", "No value should be specified"],
            ["TDS Statement (Challan Detail Record)"], ["1", "Line Number", "INTEGER", "9", "M", "x"],
            ["TDS Statement (Deductee Detail Record)"], ["1", "Line Number", "INTEGER", "9", "M", "x"],
            ["Nature of Remittance", "Code", "Section code."], ["Interest income", "27", "1057"], ["Other income", "99"],
            ["Annexure 10 - Country name"], ["Particulars", "Country code"], ["AUSTRALIA", "15"],
        ]
        strings, xml_rows = [], []
        for r in rows:
            cells = []
            for v in r:
                strings.append(v)
                cells.append(f'<c r="A1" t="s"><v>{len(strings) - 1}</v></c>')
            xml_rows.append(f"<row>{''.join(cells)}</row>")
        p = Path(tempfile.mkdtemp()) / "s.xlsx"
        with zipfile.ZipFile(p, "w") as z:
            z.writestr("xl/sharedStrings.xml", "<sst>" + "".join(f"<si><t>{s}</t></si>" for s in strings) + "</sst>")
            z.writestr("xl/worksheets/sheet1.xml", "<sheetData>" + "".join(xml_rows) + "</sheetData>")
        s = spec.load(p)
        self.assertEqual(s.count("BH"), 2)
        self.assertTrue(s.records["BH"][2].must_be_empty)
        self.assertTrue(s.records["FH"][1].mandatory)
        self.assertEqual(s.remittance["1057"], {"27", "99"})
        self.assertEqual(s.countries["15"], "AUSTRALIA")


class PortalTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.cfg = make_cfg(self.tmp)
        self.qt = Quarter(2026, 2)

    def test_validate_rrr(self):
        from tds_filer import portal
        self.assertEqual(portal.validate_rrr(" 123456789012345 "), "123456789012345")
        for bad in ("12345", "12345678901234X", "1234567890123456"):
            with self.assertRaises(TdsError):
                portal.validate_rrr(bad)

    def test_task_masks_tan_and_forbids_submit(self):
        from tds_filer import portal
        t = portal.build_task(self.cfg, self.qt, list(portal.WHAT))
        self.assertNotIn(self.cfg.deductor["tan"], t)
        self.assertIn("x_tan", t)
        self.assertIn("Never click Submit", t)
        self.assertIn("2026-27 quarter Q1", t)
        self.assertNotIn("View Filed Forms", portal.build_task(self.cfg, self.qt, ["csi"]))

    def _fetch(self, rrr, *extra):
        from unittest import mock
        from tds_filer import cli, portal
        res = portal.FetchResult(rrr={"2026-27/Q1": rrr} if rrr else {})
        with mock.patch.object(portal, "fetch", return_value=res) as m:
            rc = cli.main(["--config", str(self.cfg.path), "fetch", "2026-27", "Q2", "--what", "rrr", *extra])
        return rc, m

    def test_fetch_records_rrr(self):
        rc, m = self._fetch("123456789012345", "-y")
        self.assertEqual(rc, 0)
        self.assertEqual(m.call_args.args[3], ["rrr"])
        self.assertEqual(Config.load(str(self.cfg.path)).rrr(Quarter(2026, 1)), "123456789012345")

    def test_fetch_does_not_overwrite(self):
        self.cfg.set_rrr(Quarter(2026, 1), "111111111111111")
        self._fetch("123456789012345", "-y")
        self.assertEqual(Config.load(str(self.cfg.path)).rrr(Quarter(2026, 1)), "111111111111111")

    def test_fetch_bad_what(self):
        from tds_filer import cli
        self.assertEqual(cli.main(["--config", str(self.cfg.path), "fetch", "2026-27", "Q2", "--what", "tracess"]), 1)

    def test_pydantic_adapter(self):
        try:
            import browser_use  # noqa: F401
            import pydantic_ai  # noqa: F401
        except ImportError:
            self.skipTest("portal extra not installed")
        import asyncio
        from pydantic import BaseModel
        from pydantic_ai.models.test import TestModel
        from browser_use.llm.messages import AssistantMessage, ContentPartTextParam, SystemMessage, UserMessage
        from tds_filer import portal

        class Out(BaseModel):
            n: int

        msgs = [SystemMessage(content="sys"), UserMessage(content="a"), AssistantMessage(content="b"),
                UserMessage(content=[ContentPartTextParam(text="c")])]
        hist, prompt = portal.to_history(msgs)
        self.assertEqual(prompt, "c")
        self.assertEqual([type(m).__name__ for m in hist], ["ModelRequest", "ModelResponse"])
        llm = portal.PydanticAIChat("test:x", model=TestModel())
        self.assertIsInstance(asyncio.run(llm.ainvoke(msgs, Out)).completion, Out)
        with self.assertRaises(TdsError):
            portal.to_history(msgs[:3])


if __name__ == "__main__":
    unittest.main()
