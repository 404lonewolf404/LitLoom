import unittest

from literature_desk.embeddings import cosine_similarity, reciprocal_rank_fusion, split_text, validate_embedding_response


class EmbeddingHelperTests(unittest.TestCase):
    def test_chunk_offsets_cover_original_text_with_overlap(self):
        text = ("第一段 food volume estimation。\n" * 90) + "末尾公式 x = y"
        chunks = split_text(text, chunk_chars=320, overlap=40)
        self.assertGreater(len(chunks), 1)
        for chunk, start, end in chunks:
            self.assertEqual(text[start:end], chunk)
        self.assertLessEqual(chunks[0][1], 1)
        self.assertTrue(any("末尾公式" in chunk for chunk, _, _ in chunks))

    def test_embedding_response_is_sorted_by_index_and_validated(self):
        vectors, dimensions = validate_embedding_response({"data": [
            {"index": 1, "embedding": [0, 1]},
            {"index": 0, "embedding": [1, 0]},
        ]}, 2)
        self.assertEqual(dimensions, 2)
        self.assertEqual(vectors, [[1.0, 0.0], [0.0, 1.0]])
        with self.assertRaisesRegex(ValueError, "全为零"):
            validate_embedding_response({"data": [{"index": 0, "embedding": [0, 0]}]}, 1)
        with self.assertRaisesRegex(ValueError, "数量不匹配"):
            validate_embedding_response({"data": []}, 1)

    def test_cosine_and_rank_fusion_preserve_distinct_evidence_lanes(self):
        self.assertAlmostEqual(cosine_similarity([1, 0], [0.5, 0.5]), 2 ** -0.5)
        fused = reciprocal_rank_fusion(
            [{"attachment_id": "a", "page_number": 1, "retrieval_method": "bm25_all_terms", "lexical_rank": 1}],
            [{"attachment_id": "a", "page_number": 1, "retrieval_method": "semantic", "semantic_score": 0.9},
             {"attachment_id": "a", "page_number": 2, "retrieval_method": "semantic", "semantic_score": 0.8}],
        )
        self.assertEqual(fused[0]["page_number"], 1)
        self.assertEqual(fused[0]["retrieval_method"], "hybrid")
        self.assertEqual(fused[1]["retrieval_method"], "semantic")


if __name__ == "__main__":
    unittest.main()
