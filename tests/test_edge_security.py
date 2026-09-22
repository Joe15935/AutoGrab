"""Guard the extension's actual installed capability boundary."""
import json
from pathlib import Path
import unittest

from autograb.edge_install import identity

ROOT = Path(__file__).resolve().parents[1]
ORIGINS = ["https://bandwagonhost.com/*", "https://www.dmit.io/*", "https://app.vmiss.com/*",
           "https://v.ps/*", "https://vps.hosting/*", "https://www.apple.com/*", "https://www.apple.com.cn/*"]


class EdgeInstalledCapabilityTests(unittest.TestCase):
    def test_actual_manifest_has_only_reviewed_capabilities(self):
        manifest = json.loads((ROOT / "edge-extension/manifest.json").read_text())
        self.assertEqual(manifest["manifest_version"], 3)
        self.assertEqual(set(manifest["permissions"]), {"nativeMessaging", "storage", "tabs", "scripting"})
        self.assertEqual(manifest["host_permissions"], ORIGINS)
        for forbidden in ("optional_permissions", "optional_host_permissions", "externally_connectable", "web_accessible_resources", "devtools_page"):
            self.assertNotIn(forbidden, manifest)
        self.assertEqual(manifest["content_security_policy"]["extension_pages"], "script-src 'self'; object-src 'none'")

    def test_actual_scripts_only_inject_in_official_top_frame(self):
        manifest = json.loads((ROOT / "edge-extension/manifest.json").read_text())
        self.assertEqual(len(manifest["content_scripts"]), 5)
        self.assertEqual([host for script in manifest["content_scripts"] for host in script["matches"]], ORIGINS)
        for script in manifest["content_scripts"]:
            self.assertIs(script["all_frames"], False)
            self.assertFalse(script.get("match_about_blank", False))
            self.assertFalse(script.get("match_origin_as_fallback", False))
            self.assertEqual(script.get("world", "ISOLATED"), "ISOLATED")
            self.assertEqual(script["js"][-1], "content.js")
            self.assertTrue((ROOT / "edge-extension" / script["js"][0]).is_file())

    def test_public_extension_identity_matches_native_host_configuration(self):
        configured = json.loads((ROOT / "config/edge-identity.json").read_text())
        self.assertEqual(identity(ROOT), configured["extension_id"])
        self.assertEqual(identity(ROOT), "eddoiocaihhammnclkhmmnafjhjilfnc")
