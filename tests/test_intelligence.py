import unittest
from autograb.core.errors import AutoGrabError
from autograb.intelligence import parse_nodeseek_rss


class IntelligenceTests(unittest.TestCase):
    def test_forum_claims_only_produce_unverified_readonly_official_candidates(self):
        data = b'''<rss><channel><item><title>Limited offer available now</title>
        <link>https://www.nodeseek.com/post-123-1</link><description><![CDATA[
        <a href="https://app.vmiss.com/store/us-los-angeles-bgp">official</a>
        https://app.vmiss.com.evil/store/x https://vps.hosting/?cmd=cart&amp;action=add&amp;id=148
        https://v.ps/products/cloud-kvm-vps/]]></description></item></channel></rss>'''
        signal, = parse_nodeseek_rss(data)
        self.assertEqual(signal["authority"], "UNVERIFIED")
        self.assertEqual([item["provider"] for item in signal["official_candidates"]], ["vmiss", "vps"])
        self.assertTrue(all(item["official_verification_required"] for item in signal["official_candidates"]))
        self.assertNotIn("availability", signal)
        with self.assertRaises(AutoGrabError):
            parse_nodeseek_rss(b'<!DOCTYPE rss [<!ENTITY x "bad">]><rss><channel/></rss>')
