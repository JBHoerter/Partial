import unittest

from partial.attribution import advance, fingerprint, new_state, rebase, report, summarize


def h(text):
    return fingerprint(text.encode())


class AttributionTests(unittest.TestCase):
    def test_aggregate_weights_changed_lines_not_file_percentages(self):
        from partial.attribution import aggregate
        small = report(advance(new_state([]), h("ai\n"),
                               kind="agent", session_id="s1"), h("ai\n"))
        big = report(new_state([], h("u1\nu2\nu3\n")), h("u1\nu2\nu3\n"))
        self.assertEqual(aggregate([small, big])["agent_percentage"], 25.0)
        self.assertEqual(aggregate([small, big])["total_changed"], 4)

    def test_agent_replacement_counts_both_sides(self):
        state = advance(new_state(h("a\nb\n")), h("a\nc\n"),
                        kind="agent", session_id="s1")
        result = report(state, h("a\nc\n"))
        self.assertEqual(result["summary"], {
            "agent_added": 1, "agent_removed": 1,
            "human_added": 0, "human_removed": 0,
            "unknown_added": 0, "unknown_removed": 0,
            "total_changed": 2, "agent_percentage": 100.0,
            "coverage_percentage": 100.0})
        self.assertEqual([(x["side"], x["line"], x["session_id"])
                          for x in result["lines"]],
                         [("old", 2, "s1"), ("new", 2, "s1")])

    def test_preexisting_work_is_unknown(self):
        state = new_state(h("base\n"), h("base\nexisting\n"))
        state = advance(state, h("base\nexisting\nagent\n"),
                        kind="agent", session_id="s1")
        summary = report(state, h("base\nexisting\nagent\n"))["summary"]
        self.assertEqual(summary["agent_added"], 1)
        self.assertEqual(summary["unknown_added"], 1)
        self.assertEqual(summary["agent_percentage"], 50.0)

    def test_human_overwrites_agent_line(self):
        state = advance(new_state(h("a\nb\n")), h("a\nc\n"),
                        kind="agent", session_id="s1")
        state = advance(state, h("a\nd\n"), kind="human", evidence="external")
        summary = report(state, h("a\nd\n"))["summary"]
        self.assertEqual(summary["agent_added"], 0)
        self.assertEqual(summary["agent_removed"], 1)
        self.assertEqual(summary["human_added"], 1)
        self.assertEqual(summary["agent_percentage"], 50.0)

    def test_reverted_change_is_not_counted(self):
        state = advance(new_state(h("a\n")), h("b\n"),
                        kind="agent", session_id="s1")
        state = advance(state, h("a\n"), kind="human", evidence="external")
        result = report(state, h("a\n"))
        self.assertEqual(result["lines"], [])
        self.assertIsNone(result["summary"]["agent_percentage"])

    def test_distinct_agents_keep_line_ownership(self):
        state = advance(new_state(h("base\n")), h("base\none\n"),
                        kind="agent", session_id="s1")
        state = advance(state, h("base\none\ntwo\n"),
                        kind="agent", session_id="s2")
        lines = report(state, h("base\none\ntwo\n"))["lines"]
        self.assertEqual([x["session_id"] for x in lines], ["s1", "s2"])

    def test_overlap_marks_changed_region_unknown(self):
        state = advance(new_state(h("a\n")), h("b\n"),
                        kind="agent", session_id="s1")
        state = advance(state, h("c\n"), kind="agent", session_id="s2",
                        expected_before=h("a\n"))
        result = report(state, h("c\n"))
        added = [x for x in result["lines"] if x["side"] == "new"]
        self.assertEqual(added[0]["kind"], "unknown")
        self.assertEqual(added[0]["evidence"], "overlap")

    def test_staged_earlier_snapshot_retains_agent_ownership(self):
        state = advance(new_state(h("a\n")), h("b\n"),
                        kind="agent", session_id="s1")
        state = advance(state, h("c\n"), kind="human", evidence="external")
        self.assertEqual(report(state, h("b\n"))["summary"]["agent_percentage"], 100.0)

    def test_unseen_partial_stage_is_unknown(self):
        state = advance(new_state(h("a\n")), h("b\n"),
                        kind="agent", session_id="s1")
        result = report(state, h("unseen\n"))["summary"]
        self.assertEqual(result["unknown_added"], 1)
        self.assertEqual(result["agent_added"], 0)

    def test_deletion_only(self):
        state = advance(new_state(h("a\nb\n")), h("a\n"),
                        kind="agent", session_id="s1")
        summary = report(state, h("a\n"))["summary"]
        self.assertEqual(summary["agent_removed"], 1)
        self.assertEqual(summary["total_changed"], 1)

    def test_rebase_preserves_uncommitted_agent_lines(self):
        state = advance(new_state(h("a\n")), h("a\nb\nc\n"),
                        kind="agent", session_id="s1")
        state = rebase(state, h("a\nb\n"))
        result = report(state, h("a\nb\nc\n"))
        self.assertEqual(result["summary"]["agent_added"], 1)
        self.assertEqual(result["summary"]["agent_removed"], 0)
        self.assertEqual(result["lines"][0]["line"], 3)
        self.assertEqual(result["lines"][0]["session_id"], "s1")

    def test_duplicate_blank_lines_and_newline_change(self):
        state = advance(new_state(h("a\n\n}\n}\n")),
                        h("a\n\nx\n}\n}\n"), kind="agent", session_id="s1")
        summary = report(state, state["current"])["summary"]
        self.assertEqual(summary["agent_added"], 1)
        self.assertEqual(summary["total_changed"], 1)
        state = advance(new_state(h("a")), h("a\n"),
                        kind="agent", session_id="s1")
        self.assertEqual(report(state, h("a\n"))["summary"]["total_changed"], 2)

    def test_state_not_mutated_and_bad_inputs_rejected(self):
        state = new_state(h("a\n"))
        advance(state, h("b\n"), kind="agent", session_id="s1")
        self.assertEqual(state["current"], h("a\n"))
        for content in (b"a\x00b", b"x" * (1024 * 1024 + 1)):
            with self.assertRaises(ValueError):
                fingerprint(content)
        with self.assertRaises(ValueError):
            advance(state, h("b\n"), kind="agent")
        row = {"line": 1, "side": "new", "kind": "unknown",
               "session_id": None, "evidence": "unobserved"}
        with self.assertRaises(ValueError):
            summarize([row, row])


if __name__ == "__main__":
    unittest.main()
