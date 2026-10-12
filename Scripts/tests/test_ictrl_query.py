"""Regressions reproduced from the OpenMinis flight-search session."""

import base64
from contextlib import redirect_stderr, redirect_stdout
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location("ictrl_query", Path(__file__).parents[1] / "ictrl_query.py")
adapter = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(adapter)


class QueryTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def invoke(self, method, reply, returncode=0, stderr="", params="{}", exception=None):
        process = subprocess.CompletedProcess([], returncode, json.dumps(reply, ensure_ascii=False), stderr)
        out, err = io.StringIO(), io.StringIO()
        with patch.object(adapter.subprocess, "run", return_value=process, side_effect=exception) as run:
            with redirect_stdout(out), redirect_stderr(err):
                rc = adapter.main([method, params, "--state-dir", str(self.root / "state"),
                                   "--attachments-dir", str(self.root / "images")])
        return rc, json.loads(out.getvalue()), err.getvalue(), run

    def test_full_page_and_ocr_are_single_json_with_no_lost_fields(self):
        source = "Other, label: '父节点'\n  Button, label: '查询', id: 'search'\n" * 1500
        result = {"source": source, "hint": "Read immediately", "channel": {"recommended": "ax"},
                  "keyboard": {"visible": True}, "truncated": True, "foreground_app": "ctrip.com"}
        rc, output, diagnostics, _ = self.invoke("page_source", {"result": result}, stderr="diagnostic\n")
        self.assertEqual(rc, 0)
        self.assertEqual(output["result"], result)
        self.assertEqual(diagnostics, "diagnostic\n")
        saved = json.loads(Path(output["client"]["snapshot_file"]).read_text())
        self.assertEqual(saved["result"]["source"], source)
        items = [{"label": "￥485", "center_x": 100, "center_y": 200}]
        rc, output, _, _ = self.invoke("screen_text", {"result": {"items": items, "source": "ocr"}})
        self.assertEqual(rc, 0)
        self.assertEqual(output["result"]["items"], items)

    def test_find_and_failed_page_never_overwrite_last_successful_page(self):
        _, first, _, _ = self.invoke("page_source", {"result": {"source": "first page"}})
        self.invoke("find_element", {"result": {"ref": "el_1"}})
        self.invoke("tap", {"result": {"settle": {"stable": True}}})
        self.invoke("page_source", {"error": {"code": -32000, "message": "not authorized"}})
        last = json.loads((self.root / "state/ictrl-last.json").read_text())
        self.assertEqual(last["result"]["source"], "first page")
        _, second, _, _ = self.invoke("page_source", {"result": {"source": "second page"}})
        self.assertNotEqual(first["client"]["snapshot_file"], second["client"]["snapshot_file"])
        self.assertEqual(json.loads(Path(first["client"]["snapshot_file"]).read_text())["result"]["source"], "first page")
        self.assertEqual(json.loads((self.root / "state/ictrl-last.json").read_text())["result"]["source"], "second page")

    def test_rpc_error_is_nonzero_even_when_static_cli_exits_zero(self):
        error = {"code": -32602, "message": "No match", "data": {"hint": "Read the page"}}
        rc, output, _, run = self.invoke("find_element", {"error": error})
        self.assertEqual(rc, 1)
        self.assertEqual(output["error"], error)
        self.assertFalse(output["client"]["rpc_succeeded"])
        run.assert_called_once()

    def test_process_failure_cannot_become_a_successful_page(self):
        rc, output, _, _ = self.invoke("page_source", {"result": {"source": "incomplete"}}, returncode=7)
        self.assertEqual(rc, 7)
        self.assertFalse(output["client"]["rpc_succeeded"])
        self.assertFalse((self.root / "state/ictrl-last.json").exists())

    def test_png_screenshot_is_saved_without_base64_in_stdout(self):
        image = b"\x89PNG\r\n\x1a\nfixture"
        rc, output, _, _ = self.invoke("screenshot", {"result": {
            "data": base64.b64encode(image).decode(), "format": "png", "scale": 2,
            "pixel_width": 804, "point_width": 402}})
        self.assertEqual(rc, 0)
        result = output["result"]
        path = Path(result["saved_image"])
        self.assertEqual(path.suffix, ".png")
        self.assertEqual(path.read_bytes(), image)
        self.assertNotIn("data", result)
        self.assertEqual(result["scale"], 2)

    def test_invalid_params_do_not_call_phone(self):
        for params in ("[]", "{broken", '{"x":NaN}'):
            with self.subTest(params=params):
                rc, output, _, run = self.invoke("tap", {}, params=params)
                self.assertEqual(rc, 2)
                self.assertEqual(output["error"]["data"]["completion"], "not_started")
                run.assert_not_called()

    def test_timeout_and_malformed_reply_never_retry_or_claim_no_action(self):
        rc, output, _, run = self.invoke("tap", {}, exception=subprocess.TimeoutExpired("client", 30))
        self.assertEqual(rc, 1)
        self.assertEqual(output["error"]["data"]["completion"], "unknown")
        run.assert_called_once()
        for reply in ([], {"unrelated": True}, {"result": {}, "error": {"message": "bad"}}):
            with self.subTest(reply=reply):
                rc, output, _, run = self.invoke("tap", reply)
                self.assertEqual(rc, 1)
                self.assertEqual(output["error"]["data"]["completion"], "unknown")
                run.assert_called_once()

    def test_snapshot_disk_failure_does_not_repeat_successful_action(self):
        with patch.object(adapter, "atomic_write", side_effect=OSError("disk full")):
            rc, output, _, run = self.invoke("tap", {"result": {"settle": {"stable": True}}})
        self.assertEqual(rc, 0)
        self.assertTrue(output["client"]["rpc_succeeded"])
        self.assertTrue(output["client"]["storage_warnings"])
        run.assert_called_once()


if __name__ == "__main__":
    unittest.main()
