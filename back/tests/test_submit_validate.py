"""접수 입력 검증이 스펙 7.1의 요청 상한을 지키는지 확인한다. AWS 호출은 하지 않는다."""

import importlib.util
import os
from pathlib import Path
import unittest

os.environ.setdefault("AWS_DEFAULT_REGION", "ap-northeast-2")
os.environ.setdefault("TABLE_NAME", "unit-test")
spec = importlib.util.spec_from_file_location("submit", Path(__file__).resolve().parents[1] / "lambda/submit/lambda.py")
submit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(submit)


def doc(reviews):
    return {"schema_version": "1.1", "site": "coupang", "product": {"product_id": "123"},
            "collection": {"by_rating": []}, "reviews": reviews}


class ValidateTests(unittest.TestCase):
    def test_valid_document_passes(self):
        self.assertIsNone(submit.validate(doc([{"review_id": "a", "rating": 5, "body": "좋아요"}])))

    def test_limits_are_enforced(self):
        cases = {
            "schema": {"schema_version": "1.0"},
            "empty": doc([]),
            "rating": doc([{"review_id": "a", "rating": 6, "body": "x"}]),
            "body": doc([{"review_id": "a", "rating": 5, "body": "x" * 2001}]),
            "total": doc([{"review_id": str(i), "rating": 1 + i % 5, "body": "x" * 2000} for i in range(51)]),
            "per rating": doc([{"review_id": str(i), "rating": 5, "body": "x"} for i in range(101)]),
            "blank": doc([{"review_id": "a", "rating": 5, "body": "  "}]),
        }
        for name, d in cases.items():
            with self.subTest(name):
                self.assertIsNotNone(submit.validate(d))

    def test_missing_client_header_is_400(self):
        out = submit.lambda_handler({"headers": {}, "body": "{}"}, None)
        self.assertEqual(out["statusCode"], 400)


if __name__ == "__main__":
    unittest.main()
