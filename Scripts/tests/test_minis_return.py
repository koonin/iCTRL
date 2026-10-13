"""Validate the one-shot return RPC without controlling a real phone."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

SCRIPT = Path(__file__).parents[1] / "minis-return-to-session.sh"


class ReturnToSessionTests(unittest.TestCase):
    def run_case(self, url, exit_code=0):
        with tempfile.TemporaryDirectory() as directory:
            stub = Path(directory) / "sh"
            stub.write_text("#!" + sys.executable + "\n" +
                            "import json,os,sys\nprint(json.dumps(sys.argv[1:]))\n" +
                            "sys.exit(int(os.environ['RETURN_TEST_EXIT']))\n")
            stub.chmod(0o755)
            env = dict(os.environ, PATH=directory, RETURN_TEST_EXIT=str(exit_code))
            env.pop("MINIS_RETURN_URL", None)
            if url is not None:
                env["MINIS_RETURN_URL"] = url
            return subprocess.run(["/bin/sh", str(SCRIPT)], env=env,
                                  capture_output=True, text=True, timeout=5)

    def test_exact_origin_is_opened_once(self):
        url = "minis://sessions/C7212E33-E957-4C2A-8738-F37C683EC725"
        result = self.run_case(url)
        self.assertEqual(result.returncode, 0)
        args = json.loads(result.stdout)
        self.assertTrue(args[0].endswith("/.ictrl/bin/ictrl.sh"))
        self.assertEqual(args[1:3], ["call", "open_url"])
        self.assertEqual(json.loads(args[3]), {"url": url})

    def test_missing_or_invalid_link_never_calls_client(self):
        for url in [None, "", "minis://", "https://example.com", "minis://sessions/", "minis://sessions/a/b", 'minis://sessions/a"', "minis://sessions/a?send=x"]:
            with self.subTest(url=url):
                result = self.run_case(url)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, "")

    def test_rpc_failure_is_preserved_without_retry(self):
        result = self.run_case("minis://sessions/chat-A", exit_code=1)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(len(result.stdout.splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
