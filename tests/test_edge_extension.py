"""Run the dependency-free JavaScript controller/security suite from test.sh."""
import shutil
import subprocess
import unittest
from pathlib import Path


class EdgeExtensionTests(unittest.TestCase):
    def test_javascript_protocol_and_controller(self):
        root = Path(__file__).resolve().parents[1]
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node.js is required to verify the Edge Companion extension")
        files = sorted((root / "edge-extension" / "tests").glob("*.test.mjs"))
        self.assertTrue(files)
        result = subprocess.run([node, "--test", *map(str, files)], cwd=root, capture_output=True, text=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
