from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import Mock
from uuid import uuid4

import requests

from scripts.check_ovh_stock import (
    Config,
    StockObservation,
    TelegramError,
    load_state,
    parse_stock_response,
    process_observation,
    send_telegram,
)


def make_config(state_file: Path) -> Config:
    return Config(
        api_url="https://ca.api.ovh.com/v1/vps/order/rule/datacenter",
        subsidiary="WE",
        plan_code="vps-2027-model2",
        plan_label="VPS Model 2",
        operating_system="Ubuntu 25.04",
        datacenter="DE",
        location_code="eu-west-lim",
        location_label="Germany - Limburg",
        configurator_url="https://www.ovhcloud.com/en/vps/configurator/",
        state_file=state_file,
        telegram_bot_token="test-token",
        telegram_chat_id="12345",
    )


def api_payload(status: str, linux_status: str) -> dict[str, object]:
    return {
        "datacenters": [
            {
                "datacenter": "BHS",
                "code": "ca-east-bhs",
                "status": "available",
                "linuxStatus": "available",
            },
            {
                "datacenter": "DE",
                "code": "eu-west-lim",
                "status": status,
                "linuxStatus": linux_status,
                "windowsStatus": "out-of-stock",
            },
        ]
    }


class ParserTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = make_config(Path("unused-state.json"))

    def test_available_example(self) -> None:
        result = parse_stock_response(
            api_payload("available", "available"), self.config
        )
        self.assertTrue(result.available)
        self.assertEqual(result.location_code, "eu-west-lim")

    def test_unavailable_example(self) -> None:
        result = parse_stock_response(
            api_payload("out-of-stock", "out-of-stock"), self.config
        )
        self.assertFalse(result.available)

    def test_partial_availability_is_not_a_false_positive(self) -> None:
        result = parse_stock_response(
            api_payload("available", "out-of-stock"), self.config
        )
        self.assertFalse(result.available)


class TransitionTests(unittest.TestCase):
    def setUp(self) -> None:
        test_state_directory = Path.cwd() / ".test-state"
        test_state_directory.mkdir(exist_ok=True)
        state_file = test_state_directory / f"{uuid4()}.json"
        self.addCleanup(state_file.unlink, missing_ok=True)
        self.config = make_config(state_file)
        self.unavailable = StockObservation(
            available=False,
            status="out-of-stock",
            linux_status="out-of-stock",
            datacenter="DE",
            location_code="eu-west-lim",
        )
        self.available = StockObservation(
            available=True,
            status="available",
            linux_status="available",
            datacenter="DE",
            location_code="eu-west-lim",
        )

    def test_first_available_observation_initializes_without_alert(self) -> None:
        notifier = Mock()
        result = process_observation(
            self.config, self.available, "2026-09-13T09:00:00Z", notifier
        )
        self.assertTrue(result.state_changed)
        self.assertFalse(result.notified)
        notifier.assert_not_called()

    def test_out_of_stock_to_available_sends_one_alert(self) -> None:
        notifier = Mock()
        process_observation(
            self.config, self.unavailable, "2026-09-13T09:00:00Z", notifier
        )
        result = process_observation(
            self.config, self.available, "2026-09-13T09:05:00Z", notifier
        )
        repeated = process_observation(
            self.config, self.available, "2026-09-13T09:10:00Z", notifier
        )

        self.assertTrue(result.notified)
        self.assertFalse(repeated.notified)
        notifier.assert_called_once()

    def test_restock_after_a_second_outage_sends_again(self) -> None:
        notifier = Mock()
        process_observation(
            self.config, self.unavailable, "2026-09-13T09:00:00Z", notifier
        )
        process_observation(
            self.config, self.available, "2026-09-13T09:05:00Z", notifier
        )
        process_observation(
            self.config, self.unavailable, "2026-09-13T09:10:00Z", notifier
        )
        process_observation(
            self.config, self.available, "2026-09-13T09:15:00Z", notifier
        )
        self.assertEqual(notifier.call_count, 2)

    def test_notification_failure_does_not_mark_stock_available(self) -> None:
        process_observation(
            self.config, self.unavailable, "2026-09-13T09:00:00Z", Mock()
        )
        notifier = Mock(side_effect=TelegramError("simulated failure"))
        with self.assertRaises(TelegramError):
            process_observation(
                self.config, self.available, "2026-09-13T09:05:00Z", notifier
            )
        self.assertFalse(load_state(self.config.state_file).available)


class TelegramTests(unittest.TestCase):
    def test_telegram_api_call_is_mocked(self) -> None:
        session = Mock(spec=requests.Session)
        response = Mock()
        response.status_code = 200
        response.json.return_value = {"ok": True, "result": {"message_id": 1}}
        session.post.return_value = response

        send_telegram(session, "secret-token", "12345", "test message")

        session.post.assert_called_once()
        _, kwargs = session.post.call_args
        self.assertEqual(kwargs["json"]["chat_id"], "12345")
        self.assertEqual(kwargs["json"]["text"], "test message")

    def test_telegram_rejection_raises_safe_error(self) -> None:
        session = Mock(spec=requests.Session)
        response = Mock()
        response.status_code = 401
        session.post.return_value = response

        with self.assertRaisesRegex(TelegramError, "HTTP 401"):
            send_telegram(session, "secret-token", "12345", "test message")


if __name__ == "__main__":
    unittest.main()
