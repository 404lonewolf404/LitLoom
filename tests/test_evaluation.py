import unittest

from literature_desk.evaluation import evaluate_ranked_cases, mean_reciprocal_rank, precision_at_k, recall_at_k


class OfflineEvaluationTests(unittest.TestCase):
    def test_metrics_ignore_duplicate_candidate_ids(self):
        ranked = ["a", "a", "b", "c"]
        relevant = ["b", "c"]
        self.assertAlmostEqual(precision_at_k(ranked, relevant, 2), 0.5)
        self.assertAlmostEqual(recall_at_k(ranked, relevant, 3), 1.0)
        self.assertAlmostEqual(mean_reciprocal_rank(ranked, relevant), 0.5)

    def test_evaluator_reports_macro_metrics_and_unlabelled_cases(self):
        report = evaluate_ranked_cases([
            {"id": "one", "query": "q1", "ranked": ["a", "b"], "relevant": ["b"]},
            {"id": "two", "ranked": ["x"], "relevant": []},
        ], k_values=(1, 2))
        self.assertEqual(report["status"], "ready")
        self.assertEqual(report["case_count"], 1)
        self.assertEqual(report["excluded_count"], 1)
        self.assertEqual(report["metrics"]["precision_at_1"], 0.0)
        self.assertEqual(report["metrics"]["recall_at_2"], 1.0)


if __name__ == "__main__":
    unittest.main()
