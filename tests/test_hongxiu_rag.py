import json
import unittest
from unittest.mock import MagicMock

import hongxiu_rag


class McpParsingTests(unittest.TestCase):
    def test_parse_plain_json(self):
        self.assertEqual(hongxiu_rag._parse_response('{"result":{"ok":true}}')["result"]["ok"], True)

    def test_parse_sse_json(self):
        body = 'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n\n'
        self.assertTrue(hongxiu_rag._parse_response(body)["result"]["ok"])

    def test_extract_tool_text_json(self):
        payload = {"answer": "MOQ is 100 pieces.", "citations": []}
        response = {"result": {"content": [{"type": "text", "text": json.dumps(payload)}]}}
        self.assertEqual(hongxiu_rag._tool_payload(response), payload)


class BusinessSearchTests(unittest.TestCase):
    def test_formats_answer_and_citations(self):
        client = MagicMock()
        client.search.return_value = {
            "answer": "Express takes around 7 working days.",
            "citations": [{"document_title": "Shipping policy", "excerpt": "Air and sea are available."}],
        }

        context, sources = hongxiu_rag.search_business_knowledge(
            "Shipping", "How long does delivery take?", client=client
        )

        client.initialize.assert_called_once_with()
        self.assertIn("Express takes around 7 working days.", context)
        self.assertIn("Air and sea are available.", context)
        self.assertEqual(sources, ["Shipping policy"])
        query = client.search.call_args.args[0]
        self.assertIn("untrusted input", query)
        self.assertIn("How long does delivery take?", query)

    def test_rejects_empty_answer(self):
        client = MagicMock()
        client.search.return_value = {"answer": "", "citations": []}

        with self.assertRaises(hongxiu_rag.RagError):
            hongxiu_rag.search_business_knowledge("Subject", "Body", client=client)


if __name__ == "__main__":
    unittest.main()
