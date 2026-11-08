"""수집 실패 전에 확보한 리뷰와 분석 범위 안내가 일치하는지 확인한다."""

import importlib.util
from pathlib import Path
import unittest


spec = importlib.util.spec_from_file_location(
    "analysis_run", Path(__file__).resolve().parents[1] / "lambda/analyze/run.py"
)
analysis = importlib.util.module_from_spec(spec)
spec.loader.exec_module(analysis)


def document(counts, failed):
    return {
        "collection": {
            "by_rating": [
                {"rating": rating, "status": "FAILED" if rating in failed else "OK",
                 "collected": count, "excluded": {}}
                for rating, count in counts.items()
            ],
            "failed_ratings": failed,
        },
        "reviews": [
            {"review_id": f"{rating}-{i}", "rating": rating, "body": "리뷰 본문"}
            for rating, count in counts.items() for i in range(count)
        ],
    }


class CoverageTests(unittest.TestCase):
    def test_partial_reviews_are_analyzed_and_described_as_included(self):
        data = document({5: 3, 1: 2}, [1])
        index = analysis.build_index(data)
        coverage, limitations = analysis.grade.coverage(data, index)

        self.assertEqual(len(index), 5)
        self.assertEqual(coverage["analyzed"], 5)
        self.assertEqual(coverage["failed_ratings"], [1])
        self.assertIn("1점 리뷰는 수집을 끝내지 못했지만 확보한 2건은 분석에 포함했어요.", limitations)
        self.assertNotIn("1점 리뷰는 가져오지 못해 빠졌어요.", limitations)

    def test_failed_rating_without_reviews_is_described_as_missing(self):
        data = document({5: 3, 1: 0}, [1])
        coverage, limitations = analysis.grade.coverage(data, analysis.build_index(data))

        self.assertEqual(coverage["analyzed"], 3)
        self.assertIn("1점 리뷰는 수집하지 못해 분석에 포함하지 못했어요.", limitations)

    def test_mixed_failures_are_described_separately(self):
        data = document({5: 3, 2: 0, 1: 2}, [1, 2])
        _, limitations = analysis.grade.coverage(data, analysis.build_index(data))

        self.assertIn("1점 리뷰는 수집을 끝내지 못했지만 확보한 2건은 분석에 포함했어요.", limitations)
        self.assertIn("2점 리뷰는 수집하지 못해 분석에 포함하지 못했어요.", limitations)

    def test_successful_collection_has_no_failure_warning(self):
        data = document({5: 3, 1: 2}, [])
        coverage, limitations = analysis.grade.coverage(data, analysis.build_index(data))

        self.assertEqual(coverage["failed_ratings"], [])
        self.assertFalse(any("수집" in line for line in limitations))


if __name__ == "__main__":
    unittest.main()
