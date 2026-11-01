import json
import os
import tempfile
import unittest
from unittest.mock import patch

import inquiry


class InquiryCardTests(unittest.TestCase):
    def test_card_is_read_only_and_has_summary_first(self):
        card = inquiry.build_card(
            mailbox="zoho",
            from_="Gabby Rosen <gabby@example.com>",
            email="gabby@example.com",
            subject="ODM inquiry",
            date="2026-11-01 10:23",
            summary="美国品牌询问 1000 件 OEM",
            original="Hi, we need 1000 pcs.",
            translation="你好，我们需要 1000 件。",
            inquiry_id="INQ-20261101-102300",
        )
        elements = card["body"]["elements"]
        self.assertIn("美国品牌询问 1000 件 OEM", elements[0]["content"])
        self.assertNotIn("action", json.dumps(card))
        self.assertNotIn("button", json.dumps(card))
        panels = [e["header"]["title"]["content"] for e in elements if e["tag"] == "collapsible_panel"]
        self.assertEqual(panels, ["📄 原文", "🌐 译文"])
        inner = [e["elements"][0]["tag"] for e in elements if e["tag"] == "collapsible_panel"]
        self.assertEqual(inner, ["div", "div"])
        self.assertFalse(card["config"]["wide_screen_mode"])

    def test_markdown_injection_is_neutralised(self):
        card = inquiry.build_card(
            mailbox="zoho",
            from_="<at id=all></at>",
            email="x@example.com",
            subject="<at id=all></at>",
            date="2026-11-01 10:23",
            summary="hi",
            original="body",
            translation="",
            inquiry_id="INQ-1",
        )
        meta = card["body"]["elements"][1]["content"]
        self.assertNotIn("<at", meta)


class NewInquiryFlowTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(prefix="inquiry-", suffix=".json")
        os.close(fd)
        os.unlink(self.path)
        self.original_state = inquiry.STATE_FILE
        inquiry.STATE_FILE = self.path

    def tearDown(self):
        inquiry.STATE_FILE = self.original_state
        if os.path.exists(self.path):
            os.unlink(self.path)

    def _send(self, verdict, confidence=0.9):
        result = {
            "is_inquiry": verdict,
            "confidence": confidence,
            "reason": "test",
            "summary": "摘要",
            "translation": "翻译",
        }
        with patch.object(inquiry, "is_configured", return_value=True), \
                patch.object(inquiry, "judge", return_value=(result, None)), \
                patch.object(inquiry, "_post", return_value=True) as post:
            sent = inquiry.notify_if_new_inquiry(
                mailbox="zoho", folder="收件箱", from_="A <a@example.com>",
                email="a@example.com", subject="hello", body="body", date="2026-11-01",
            )
        return sent, post

    def test_first_inquiry_is_forwarded_once(self):
        sent, post = self._send(True)
        self.assertTrue(sent)
        post.assert_called_once()
        sent_again, post_again = self._send(True)
        self.assertFalse(sent_again)
        post_again.assert_not_called()

    def test_low_confidence_is_not_forwarded(self):
        sent, post = self._send(True, confidence=0.2)
        self.assertFalse(sent)
        post.assert_not_called()

    def test_non_inquiry_is_remembered_without_sending(self):
        sent, post = self._send(False)
        self.assertFalse(sent)
        post.assert_not_called()
        self.assertTrue(inquiry.already_screened("A@Example.com"))


if __name__ == "__main__":
    unittest.main()
