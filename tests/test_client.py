import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from finam_trading_bot.client import FinamClient, FinamClientError, FinamHTTPError
from finam_trading_bot.safety import LiveTradingBlocked


class FinamClientTests(unittest.TestCase):
    @patch("finam_trading_bot.client.urlopen")
    def test_create_session_returns_token(self, urlopen_mock):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({"token": "jwt"}).encode()
        urlopen_mock.return_value = response

        assert FinamClient().create_session("secret") == "jwt"

    @patch("finam_trading_bot.client.urlopen")
    def test_session_details_returns_object(self, urlopen_mock):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({"accounts": []}).encode()
        urlopen_mock.return_value = response

        assert FinamClient().session_details("jwt") == {"accounts": []}

    @patch("finam_trading_bot.client.urlopen")
    def test_get_account_uses_bearer_token_and_encoded_account_path(self, urlopen_mock):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({"positions": []}).encode()
        urlopen_mock.return_value = response

        result = FinamClient().get_account("jwt", "DEMO-ACCOUNT")

        assert result == {"positions": []}
        request = urlopen_mock.call_args.args[0]
        assert request.full_url == "https://api.finam.ru/v1/accounts/DEMO-ACCOUNT"
        assert request.get_method() == "GET"
        assert request.get_header("Authorization") == "Bearer jwt"
        assert request.get_header("Accept") == "application/json"

    @patch("finam_trading_bot.client.urlopen")
    def test_account_history_methods_encode_limit_and_interval_query(self, urlopen_mock):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({"items": []}).encode()
        urlopen_mock.return_value = response
        client = FinamClient()

        client.trades("jwt", "DEMO-ACCOUNT", limit=10, start_time="2026-01-01T00:00:00Z")
        client.transactions("jwt", "DEMO-ACCOUNT", limit=5, end_time="2026-01-02T00:00:00Z")
        client.orders("jwt", "DEMO-ACCOUNT")

        urls = [call.args[0].full_url for call in urlopen_mock.call_args_list]
        assert urls == [
            "https://api.finam.ru/v1/accounts/DEMO-ACCOUNT/trades?limit=10&interval.start_time=2026-01-01T00%3A00%3A00Z",
            "https://api.finam.ru/v1/accounts/DEMO-ACCOUNT/transactions?limit=5&interval.end_time=2026-01-02T00%3A00%3A00Z",
            "https://api.finam.ru/v1/accounts/DEMO-ACCOUNT/orders",
        ]
        assert all(call.args[0].get_method() == "GET" for call in urlopen_mock.call_args_list)
        assert all(call.args[0].get_header("Authorization") == "Bearer jwt" for call in urlopen_mock.call_args_list)

    @patch("finam_trading_bot.client.urlopen")
    def test_market_data_methods_use_readonly_get_endpoints(self, urlopen_mock):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps(
            {"orderbook": {"rows": [1, 2, 3]}, "trades": [1, 2, 3], "items": []}
        ).encode()
        urlopen_mock.return_value = response
        client = FinamClient()

        client.last_quote("jwt", "SBER@MISX")
        order_book = client.order_book("jwt", "SBER@MISX", depth=2)
        client.bars(
            "jwt",
            "SBER@MISX",
            interval="INTRADAYCANDLE_TIMEFRAME_M1",
            start_time="2026-01-01T00:00:00Z",
            end_time="2026-01-01T01:00:00Z",
        )
        latest_trades = client.latest_trades("jwt", "SBER@MISX", limit=2)
        client.assets("jwt")

        urls = [call.args[0].full_url for call in urlopen_mock.call_args_list]
        assert urls == [
            "https://api.finam.ru/v1/instruments/SBER%40MISX/quotes/latest",
            "https://api.finam.ru/v1/instruments/SBER%40MISX/orderbook",
            "https://api.finam.ru/v1/instruments/SBER%40MISX/bars?timeframe=TIME_FRAME_M1&interval.start_time=2026-01-01T00%3A00%3A00Z&interval.end_time=2026-01-01T01%3A00%3A00Z",
            "https://api.finam.ru/v1/instruments/SBER%40MISX/trades/latest",
            "https://api.finam.ru/v1/assets",
        ]
        assert all(call.args[0].get_method() == "GET" for call in urlopen_mock.call_args_list)
        assert all("/v1/assets/" not in url for url in urls)
        assert order_book["orderbook"]["rows"] == [1, 2]
        assert latest_trades["trades"] == [1, 2]

    @patch("finam_trading_bot.client.urlopen")
    def test_asset_and_asset_params_use_readonly_get_endpoints(self, urlopen_mock):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({"asset": {"lot_size": {"value": "10"}}}).encode()
        urlopen_mock.return_value = response
        client = FinamClient()

        result = client.asset("jwt", "MTSS@MISX", account_id="DEMO-ACCOUNT")
        client.asset_params("jwt", "MTSS@MISX", account_id="DEMO-ACCOUNT")

        assert result == {"asset": {"lot_size": {"value": "10"}}}
        requests = [call.args[0] for call in urlopen_mock.call_args_list]
        assert [request.full_url for request in requests] == [
            "https://api.finam.ru/v1/assets/MTSS%40MISX?account_id=DEMO-ACCOUNT",
            "https://api.finam.ru/v1/assets/MTSS%40MISX/params?account_id=DEMO-ACCOUNT",
        ]
        assert all(request.get_method() == "GET" for request in requests)
        assert all(request.get_header("Authorization") == "Bearer jwt" for request in requests)

    @patch("finam_trading_bot.client.urlopen")
    @patch.dict(os.environ, {"TRADING_MODE": "live"}, clear=False)
    def test_mutating_methods_use_guardable_account_endpoints(self, urlopen_mock):
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({"order_id": "1"}).encode()
        urlopen_mock.return_value = response
        client = FinamClient()

        client.place_order("jwt", "TEST-ACCOUNT", {"symbol": "SBER@MISX"})
        client.place_sltp_order("jwt", "TEST-ACCOUNT", {"symbol": "SBER@MISX"})
        client.get_order("jwt", "TEST-ACCOUNT", "14181")
        client.cancel_order("jwt", "TEST-ACCOUNT", "14181")

        requests = [call.args[0] for call in urlopen_mock.call_args_list]
        assert [request.full_url for request in requests] == [
            "https://api.finam.ru/v1/accounts/TEST-ACCOUNT/orders",
            "https://api.finam.ru/v1/accounts/TEST-ACCOUNT/sltp-orders",
            "https://api.finam.ru/v1/accounts/TEST-ACCOUNT/orders/14181",
            "https://api.finam.ru/v1/accounts/TEST-ACCOUNT/orders/14181",
        ]
        assert [request.get_method() for request in requests] == ["POST", "POST", "GET", "DELETE"]
        assert all(request.get_header("Authorization") == "Bearer jwt" for request in requests)

    @patch("finam_trading_bot.client.urlopen")
    @patch.dict(os.environ, {"TRADING_MODE": "live"}, clear=False)
    def test_http_error_includes_response_body_for_broker_diagnostics(self, urlopen_mock):
        error = HTTPError(
            "https://api.finam.ru/v1/accounts/TEST-ACCOUNT/orders",
            400,
            "Bad Request",
            {},
            io.BytesIO(b'{"message":"invalid field limit_price is required"}'),
        )
        urlopen_mock.side_effect = error

        with self.assertRaises(FinamClientError) as context:
            FinamClient().place_order("jwt", "TEST-ACCOUNT", {"symbol": "SBER@MISX"})

        self.assertIn("Finam HTTP 400", str(context.exception))
        self.assertIn("limit_price is required", str(context.exception))

    @patch("finam_trading_bot.client.time.sleep")
    @patch("finam_trading_bot.client.urlopen")
    def test_get_retries_429_with_retry_after(self, urlopen_mock, sleep_mock):
        error = HTTPError(
            "https://api.finam.ru/v1/instruments/SBER%40MISX/bars",
            429,
            "Too Many Requests",
            {"Retry-After": "2"},
            io.BytesIO(b'{"code":8,"message":"Too Many Requests"}'),
        )
        response = MagicMock()
        response.__enter__.return_value.read.return_value = json.dumps({"bars": []}).encode()
        urlopen_mock.side_effect = [error, response]

        result = FinamClient().bars(
            "jwt",
            "SBER@MISX",
            interval="TIME_FRAME_H4",
            start_time="2026-01-01T00:00:00Z",
            end_time="2026-01-02T00:00:00Z",
        )

        self.assertEqual(result, {"bars": []})
        self.assertEqual(urlopen_mock.call_count, 2)
        self.assertGreaterEqual(sleep_mock.call_args.args[0], 2)

    @patch("finam_trading_bot.client.urlopen")
    @patch.dict(os.environ, {"TRADING_MODE": "live"}, clear=False)
    def test_post_does_not_retry_429_to_avoid_duplicate_broker_mutation(self, urlopen_mock):
        error = HTTPError(
            "https://api.finam.ru/v1/accounts/TEST-ARENA/orders",
            429,
            "Too Many Requests",
            {"Retry-After": "2"},
            io.BytesIO(b'{"code":8,"message":"Too Many Requests"}'),
        )
        urlopen_mock.side_effect = error

        with self.assertRaises(FinamHTTPError) as context:
            FinamClient().place_order("jwt", "TEST-ARENA", {"symbol": "SBER@MISX"})

        self.assertEqual(context.exception.status_code, 429)
        self.assertEqual(context.exception.retry_after_seconds, 2)
        self.assertEqual(urlopen_mock.call_count, 1)

    @patch("finam_trading_bot.client.urlopen")
    @patch.dict(os.environ, {}, clear=True)
    def test_mutating_methods_fail_closed_in_default_paper_mode(self, urlopen_mock):
        with self.assertRaises(LiveTradingBlocked):
            FinamClient().place_order("jwt", "REAL-ACCOUNT", {"symbol": "SBER@MISX"})
        urlopen_mock.assert_not_called()

    @patch("finam_trading_bot.client.urlopen")
    @patch.dict(os.environ, {"TRADING_MODE": "live"}, clear=True)
    def test_mutating_methods_reject_placeholder_account(self, urlopen_mock):
        with self.assertRaises(LiveTradingBlocked):
            FinamClient().place_order("jwt", "DEMO-ACCOUNT", {"symbol": "SBER@MISX"})
        urlopen_mock.assert_not_called()


if __name__ == "__main__":
    unittest.main()
