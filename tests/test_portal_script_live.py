"""Live test of the scripted portal steps against a local synthetic fixture (real headless Chromium).

Run with: TDS_LIVE=1 uv run python -m unittest tests.test_portal_script_live
"""

import asyncio
import os
import tempfile
import threading
import unittest
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

FIXTURE = Path(__file__).parent / "fixtures" / "portal"


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/dl/"):
            name = self.path[4:]
            body = b"%PDF-1.4 synthetic" if name.endswith(".pdf") else b"synthetic csi"
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def log_message(self, *a):
        pass


@unittest.skipUnless(os.environ.get("TDS_LIVE"), "set TDS_LIVE=1 (needs Chromium)")
class ScriptedPortalLiveTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(FIXTURE)))
        cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def run_steps(self, query="", what=("rrr", "challans", "csi")):
        import browser_use as bu
        from tds_filer import portal_script as ps
        from tds_filer.config import Quarter
        qdir = Path(tempfile.mkdtemp())
        qt = Quarter(2026, 2)
        ctx = {"rrr": {}, "tan": "ABCD12345E"}
        out, blocked = {}, []

        async def main():
            b = bu.Browser(headless=True, allowed_domains=["127.0.0.1"], downloads_path=str(qdir),
                           accept_downloads=True, keep_alive=True, user_data_dir=tempfile.mkdtemp())
            await b.start()
            try:
                page = await b.get_current_page() or await b.new_page()
                d = ps.Driver(page, qdir, qdir / "guard.jsonl", blocked)
                await page.goto(f"http://127.0.0.1:{self.port}/index.html{query}#/dashboard")
                await d.wait_for(ps.SEL["efile"], timeout=20)
                for name in what:
                    out[name] = await ps.STEPS[name][0](d, qt, ctx)
            finally:
                await b.kill()

        asyncio.run(main())
        return qdir, ctx, out, blocked

    def test_all_steps(self):
        qdir, ctx, out, blocked = self.run_steps(query="?per=3")
        self.assertEqual(ctx["rrr"]["2026-27/Q1"], "123456789012345")
        self.assertEqual(out["rrr"]["ack"], "ACK_123456789012345.pdf")
        self.assertEqual(sorted(out["challans"]["downloaded"]),
                         [f"1111010000000{i}SBIN_ChallanReceipt.pdf" for i in (2, 3, 4)])
        self.assertTrue((qdir / "ABCD12345E011126.csi").exists())
        self.assertEqual(blocked, [])

    def test_rrr_after_epay_needs_dashboard(self):
        # portal quirk: View Filed Forms does not render when entered from e-Pay Tax; script goes via Dashboard
        _, ctx, _, _ = self.run_steps(query="?slowforms=1", what=("challans", "rrr"))
        self.assertEqual(ctx["rrr"]["2026-27/Q1"], "123456789012345")

    def test_run_with_fallback(self):
        # run(): rrr via script; csi step broken -> agent fallback (faked) is used and the check re-run
        from unittest import mock
        from tds_filer import portal_script as ps
        from tds_filer.config import Quarter
        from test_tds import make_cfg
        tmp = Path(tempfile.mkdtemp())
        cfg, qt, qdir, logs = make_cfg(tmp), Quarter(2026, 2), tmp / "q", tmp / "logs"
        qdir.mkdir(), logs.mkdir()
        calls = []

        async def broken(d, qt, ctx):
            raise ps.StepError("selector moved")

        async def fake_agent(bu, cfg, qt, name, ctx, browser, llm, blocked, log_dir):
            calls.append(name)
            (qdir / f"{ctx['tan']}011126.csi").write_text("synthetic")

        overrides = dict(headless=True, allowed_domains=["127.0.0.1"], user_data_dir=str(tmp / "prof"))
        with mock.patch.object(ps, "BROWSER_OVERRIDES", overrides), \
                mock.patch.object(ps, "LOGIN_URL_OVERRIDE", f"http://127.0.0.1:{self.port}/index.html#/dashboard"), \
                mock.patch.dict(ps.STEPS, {"csi": (broken, ps.check_csi)}), \
                mock.patch.object(ps, "_agent_step", fake_agent):
            ctx, steps, blocked = asyncio.run(ps.run(cfg, qt, qdir, ["csi", "rrr"], object(), logs))
        by = {s.name: s for s in steps}
        self.assertEqual([s.name for s in steps], ["rrr", "csi"])
        self.assertTrue(by["rrr"].ok and by["rrr"].via == "script")
        self.assertTrue(by["csi"].ok and by["csi"].via == "agent")
        self.assertIn("selector moved", by["csi"].error)
        self.assertEqual(calls, ["csi"])
        self.assertEqual(ctx["rrr"]["2026-27/Q1"], "123456789012345")
        self.assertTrue((logs / "script_steps.jsonl").exists())
        self.assertEqual(blocked, [])


if __name__ == "__main__":
    unittest.main()
