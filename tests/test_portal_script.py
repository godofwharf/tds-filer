import tempfile
import unittest
from datetime import date
from pathlib import Path

from tds_filer import portal, portal_script as ps
from tds_filer.config import Quarter

from test_tds import make_cfg


class PortalScriptHelpersTest(unittest.TestCase):
    qt = Quarter(2026, 2)

    def test_parse_rrr(self):
        self.assertEqual(ps.parse_rrr("Download Receipt × RRR Number 123456789012345 Cancel Download"),
                         "123456789012345")
        with self.assertRaises(ps.StepError):
            ps.parse_rrr("Download Receipt Cancel")

    def test_card_regex(self):
        import re
        rx = ps.card_regex(self.qt)
        self.assertTrue(re.search(rx, "T.Y.2026-27 (Q2) View Details Filing Date"))
        self.assertFalse(re.search(rx, "T.Y.2026-27 (Q1) View Details"))

    def test_parse_row_and_plan(self):
        rows = ["26100100000001SBIN ref 2026-27 x 31200 01-Oct-2026 10:00:00",
                "26090100000002SBIN ref 2026-27 x 31200 01-Sep-2026 10:23:34",
                "garbage",
                "26070100000003SBIN ref 2026-27 x 31200 01-Jul-2026 09:00:00",
                "26060100000004SBIN ref 2026-27 x 31200 01-Jun-2026 09:00:00"]
        self.assertEqual(ps.parse_row(rows[1]), ("26090100000002SBIN", date(2026, 9, 1)))
        self.assertIsNone(ps.parse_row(rows[2]))
        todo, stop = ps.plan_page(rows, self.qt)
        self.assertEqual([i for i, _, _ in todo], [1, 3])
        self.assertTrue(stop)
        self.assertFalse(ps.plan_page(rows[:2], self.qt)[1])

    def test_act_label(self):
        self.assertIn("2025", ps.act_label(self.qt))
        self.assertIn("1961", ps.act_label(Quarter(2025, 4)))

    def test_fallback_task_single_step(self):
        with tempfile.TemporaryDirectory() as t:
            cfg = make_cfg(Path(t))
            for name in ps.ORDER:
                task = ps.fallback_task(cfg, self.qt, name)
                self.assertIn("Do only this", task)
                self.assertTrue(task.rstrip().endswith("finish with done."))
                self.assertNotIn("wait_for_manual_login and wait", task)
                self.assertNotIn("1. 2.", task)
                self.assertEqual(task.count("Finish with done"), 0)

    def test_scripted_clicks_guarded(self):
        ck = lambda label: portal.check_action("click", {"index": 0}, label)
        self.assertIsNotNone(ck("File Income Tax Forms"))
        self.assertIsNotNone(ck("Proceed to Payment"))
        self.assertIsNone(ck("Download Receipt"))
        self.assertIsNone(ck("Download Challan File"))
        self.assertIsNone(ck("View Filed Forms"))


if __name__ == "__main__":
    unittest.main()
