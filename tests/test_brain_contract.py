import unittest

from partial.brain_contract import cosine, evidence_packet, fts_query, fuse, validate_answer


class BrainContractTests(unittest.TestCase):
    def test_usage_cumulative_not_double_counted(self):
        from partial.brain_contract import usage_totals
        def event(scope, value, eid):
            return {"id": eid, "kind": "usage", "data": {
                "usage_scope": scope, "usage": {"input_tokens": value,
                "output_tokens": 1}, "usage_id": eid}}
        rows = [event("cumulative", 10, "c1"), event("delta", 5, "d1"),
                event("cumulative", 20, "c2")]
        result = usage_totals(rows)
        self.assertEqual(result["input_tokens"], 20)
        self.assertEqual(result["basis"], "latest-cumulative")
        rows = [event("delta", 10, "same"), event("delta", 10, "same"),
                event("delta", 5, "other")]
        self.assertEqual(usage_totals(rows)["input_tokens"], 15)
        self.assertIsNone(usage_totals([])["input_tokens"])
        self.assertIsNone(usage_totals(rows)["cached_input_tokens"])
        rows.append({"kind": "usage", "data": {"usage": {"input_tokens": 99}}})
        self.assertFalse(usage_totals(rows)["complete"])

    def test_query_is_quoted_literal_tokens(self):
        self.assertEqual(fts_query('why "auth" OR token*'),
                         '"why" OR "auth" OR "OR" OR "token"')
        with self.assertRaises(ValueError):
            fts_query("***")

    def test_cosine_validates_and_compares(self):
        self.assertAlmostEqual(cosine([1, 0], [1, 0]), 1)
        self.assertAlmostEqual(cosine([1, 0], [0, 1]), 0)
        self.assertAlmostEqual(cosine([1, 0], [-1, 0]), -1)
        for candidate in ([0, 0], [1], [float('nan'), 1], [True, 1]):
            with self.assertRaises(ValueError):
                cosine([1, 0], candidate)

    def test_fusion_preserves_union_and_deduplicates(self):
        a, b, c = ({"id": name} for name in "abc")
        result = fuse([a, b, a], [b, c])
        self.assertEqual([x["id"] for x in result], ["b", "a", "c"])
        self.assertAlmostEqual(result[0]["retrieval_score"], 1 / 62 + 1 / 61)
        self.assertAlmostEqual(result[1]["retrieval_score"], 1 / 61)

    def test_packet_explicitly_marks_truncation(self):
        result = evidence_packet([{"id": "a", "text": "x" * 49000},
                                  {"id": "b", "text": "not included"}])
        self.assertEqual(len(result), 1)
        self.assertEqual(len(result[0]["text"]), 48000)
        self.assertTrue(result[0]["truncated"])

    def test_answer_rejects_unknown_citations(self):
        answer = {"answer": "Recorded change [a]", "citations": ["a"],
                  "uncertainties": []}
        self.assertEqual(validate_answer(answer, [{"id": "a"}]), answer)
        with self.assertRaises(ValueError):
            validate_answer(answer, [{"id": "b"}])
        with self.assertRaises(ValueError):
            validate_answer({**answer, "extra": "x"}, [{"id": "a"}])

    def test_review_types_and_citations(self):
        finding = {"severity": "medium", "title": "Issue",
                   "description": "Evidence [a]", "path": "a.py", "line": 1,
                   "citations": ["a"]}
        result = {"findings": [finding], "summary": "One finding",
                  "uncertainties": []}
        self.assertEqual(validate_answer(result, [{"id": "a"}], review=True), result)
        bad = {**result, "findings": [{**finding, "line": True}]}
        with self.assertRaises(ValueError):
            validate_answer(bad, [{"id": "a"}], review=True)
        uncited = {**result, "findings": [{**finding, "citations": []}]}
        with self.assertRaises(ValueError):
            validate_answer(uncited, [{"id": "a"}], review=True)


if __name__ == "__main__":
    unittest.main()
