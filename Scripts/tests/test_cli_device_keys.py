"""Exercise a native CLI against a fake server; set ICTRL_TEST_CLI to its path."""
import base64
from http.server import BaseHTTPRequestHandler, HTTPServer
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest

spec = importlib.util.spec_from_file_location("rpc", Path(__file__).parents[1] / "ictrl_rpc.py")
rpc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(rpc)


@unittest.skipUnless(os.environ.get("ICTRL_TEST_CLI"), "Set ICTRL_TEST_CLI to a native CLI build")
class DeviceKeyTests(unittest.TestCase):
    def exercise(self, response, expected_status, bad_mac=False):
        key = bytes(range(32))  # Synthetic fixture only; never reads real keys.
        calls = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def send_json(self, value):
                body = json.dumps(value).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                calls.append(self.path)
                self.send_json({"device_id": "test-phone", "server_version": "1.98.12"})

            def do_POST(self):
                calls.append(self.path)
                envelope = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                nonce = bytes.fromhex(envelope["nonce"])
                ct = rpc._keystream_xor(rpc._hmac(key, rpc.LABEL_ENC_RESP + nonce),
                                       nonce, json.dumps(response).encode())
                tag = rpc._hmac(rpc._hmac(key, rpc.LABEL_MAC + nonce), b"resp" + nonce + ct)
                self.send_json({"ct": base64.b64encode(ct).decode(),
                                "tag": bytes(32).hex() if bad_mac else tag.hex()})

        server = HTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                store = Path(directory) / ".ictrl/token.json"
                store.parent.mkdir()
                address = "127.0.0.1:" + str(server.server_port)
                record = {"key": key.hex(), "tok": "01020304"}
                store.write_text(json.dumps({address: record}))
                env = dict(os.environ)
                env["HOME"] = directory

                def invoke():
                    return subprocess.run([os.environ["ICTRL_TEST_CLI"], "--host", address,
                                           "call", "device_info"], env=env, capture_output=True,
                                          text=True, timeout=5)

                result = invoke()
                self.assertEqual(result.returncode, expected_status, result.stderr)
                saved = json.loads(store.read_text())
                self.assertEqual(saved[address], record)
                if bad_mac:
                    self.assertNotIn("device:test-phone", saved)
                    self.assertEqual(result.stdout, "")
                else:
                    self.assertEqual(json.loads(result.stdout), response)
                    self.assertEqual(saved["device:test-phone"]["key"], key.hex())
                    # A later connection must work with only the device-key entry.
                    del saved[address]
                    store.write_text(json.dumps(saved))
                    result = invoke()
                    self.assertEqual(result.returncode, expected_status, result.stderr)
                    self.assertEqual(json.loads(result.stdout), response)
                self.assertNotIn("/pair", calls)
        finally:
            server.shutdown()
            server.server_close()
            worker.join()

    def test_legacy_key_migrates_and_is_reused(self):
        self.exercise({"jsonrpc": "2.0", "id": 1, "result": {"error": "UI text"}}, 0)

    def test_rpc_error_still_exits_nonzero_after_migration(self):
        self.exercise({"jsonrpc": "2.0", "id": 1, "error": {"code": -32000}}, 1)

    def test_unauthenticated_reply_cannot_migrate_key(self):
        self.exercise({"jsonrpc": "2.0", "id": 1, "result": {}}, 1, bad_mac=True)
