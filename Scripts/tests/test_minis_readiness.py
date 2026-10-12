"""Exercise the real readiness script with fake transport and a fast clock."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


SCRIPT = Path(__file__).parents[1] / "minis-ensure-ictrl.sh"


class ReadinessTests(unittest.TestCase):
    def run_case(self, ready_after, opener=True):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ["curl", "date", "sleep"] + (["apple-open"] if opener else []):
                path = root / name
                path.write_text("#!" + sys.executable + "\n" + '''
import json, os, pathlib, sys
root = pathlib.Path(os.environ['READINESS_TEST_ROOT'])
name = pathlib.Path(sys.argv[0]).name
counter = root / (name + '.count')
n = int(counter.read_text()) + 1 if counter.exists() else 1
counter.write_text(str(n))
if name == 'curl':
    sys.exit(0 if n >= int(os.environ['READINESS_READY_AFTER']) else 7)
if name == 'date': print(n * 6)
if name == 'apple-open':
    (root / 'opened-url').write_text(sys.argv[1])
    print(json.dumps({'opened': True}))
''')
                path.chmod(0o755)
            env = dict(os.environ, PATH=str(root), READINESS_TEST_ROOT=str(root),
                       READINESS_READY_AFTER=str(ready_after))
            result = subprocess.run(["/bin/sh", str(SCRIPT)], env=env,
                                    capture_output=True, text=True, timeout=10)
            counts = {p.stem: int(p.read_text()) for p in root.glob('*.count')}
            url = (root / 'opened-url').read_text() if (root / 'opened-url').exists() else None
            return result, counts, url

    def test_ready_service_needs_no_wake_or_wait(self):
        result, counts, _ = self.run_case(1)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(json.loads(result.stdout), {'ready': True, 'wake_requested': False})
        self.assertEqual(counts, {'curl': 1})

    def test_one_wake_then_probe_until_ready(self):
        result, counts, url = self.run_case(3)
        self.assertEqual(result.returncode, 0)
        self.assertTrue(json.loads(result.stdout.splitlines()[-1])['ready'])
        self.assertEqual(counts['apple-open'], 1)
        self.assertEqual(url, 'ictrl://start?callback=minis%3A%2F%2F')

    def test_opened_does_not_mean_ready_and_wait_is_bounded(self):
        result, counts, _ = self.run_case(999)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(json.loads(result.stdout.splitlines()[-1])['ready'])
        self.assertEqual(counts['apple-open'], 1)
        self.assertLess(counts['curl'], 10)

    def test_no_opener_reports_runtime_requirement(self):
        result, counts, _ = self.run_case(999, opener=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(json.loads(result.stdout)['ready'])
        self.assertNotIn('sleep', counts)


if __name__ == '__main__':
    unittest.main()
