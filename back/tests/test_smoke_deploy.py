"""배포 검증이 장애나 잘못된 TTL을 성공으로 처리하지 않는지 확인한다."""

import importlib.util
from pathlib import Path
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError


spec = importlib.util.spec_from_file_location("smoke_deploy", Path(__file__).resolve().parents[1] / "scripts/smoke_deploy.py")
smoke = importlib.util.module_from_spec(spec)
spec.loader.exec_module(smoke)


class SmokeDeployTests(unittest.TestCase):
    def test_placeholder_501_is_an_expected_connection_result(self):
        error = HTTPError("https://example.test", 501, "Not implemented", {}, None)
        with patch.object(smoke, "urlopen", side_effect=error):
            smoke.check_http("https://example.test", "POST", {400, 501}, b"{}")

    def test_gateway_failure_cannot_pass(self):
        error = HTTPError("https://example.test", 502, "Bad gateway", {}, None)
        with patch.object(smoke, "urlopen", side_effect=error), self.assertRaises(RuntimeError):
            smoke.check_http("https://example.test", "POST", {400, 501}, b"{}")

    def test_network_failure_cannot_pass(self):
        with patch.object(smoke, "urlopen", side_effect=TimeoutError), self.assertRaises(TimeoutError):
            smoke.check_http("https://example.test", "GET", {404, 501})

    def test_ttl_enabled_or_enabling_is_accepted(self):
        for status in ("ENABLING", "ENABLED"):
            with self.subTest(status=status):
                client = Mock()
                client.describe_time_to_live.return_value = {"TimeToLiveDescription": {
                    "AttributeName": "expiresAt", "TimeToLiveStatus": status}}
                smoke.check_ttl(client, "trustlens-reviews")

    def test_disabled_or_missing_ttl_cannot_pass(self):
        for ttl in ({}, {"TimeToLiveStatus": "DISABLED"},
                    {"TimeToLiveStatus": "ENABLED", "AttributeName": "wrongAttribute"}):
            with self.subTest(ttl=ttl):
                client = Mock()
                client.describe_time_to_live.return_value = {"TimeToLiveDescription": ttl}
                with self.assertRaises(RuntimeError):
                    smoke.check_ttl(client, "trustlens-reviews")


if __name__ == "__main__":
    unittest.main()
