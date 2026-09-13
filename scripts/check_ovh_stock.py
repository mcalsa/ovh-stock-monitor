#!/usr/bin/env python3
"""Check one OVHcloud VPS location and notify on a restock transition."""

from __future__ import annotations

import json
import logging
import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

LOGGER = logging.getLogger("ovh-stock-monitor")
CONNECT_TIMEOUT_SECONDS = 5
READ_TIMEOUT_SECONDS = 20
REQUEST_TIMEOUT = (CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS)
AVAILABLE_STATUS = "available"


class MonitorError(RuntimeError):
    """A safe, user-facing monitor failure."""


class ResponseFormatError(MonitorError):
    """OVH returned an unexpected response structure."""


class StateError(MonitorError):
    """The cached state could not be read safely."""


class TelegramError(MonitorError):
    """Telegram did not accept the notification."""


@dataclass(frozen=True)
class Config:
    api_url: str
    subsidiary: str
    plan_code: str
    plan_label: str
    operating_system: str
    datacenter: str
    location_code: str
    location_label: str
    configurator_url: str
    state_file: Path
    telegram_bot_token: str | None
    telegram_chat_id: str | None

    @classmethod
    def from_environment(cls) -> Config:
        return cls(
            api_url=os.getenv(
                "OVH_API_URL",
                "https://ca.api.ovh.com/v1/vps/order/rule/datacenter",
            ),
            subsidiary=os.getenv("OVH_SUBSIDIARY", "WE"),
            plan_code=os.getenv("OVH_PLAN_CODE", "vps-2027-model2"),
            plan_label=os.getenv("OVH_PLAN_LABEL", "VPS Model 2"),
            operating_system=os.getenv("OVH_OS", "Ubuntu 25.04"),
            datacenter=os.getenv("OVH_DATACENTER", "DE"),
            location_code=os.getenv("OVH_LOCATION_CODE", "eu-west-lim"),
            location_label=os.getenv("OVH_LOCATION_LABEL", "Germany - Limburg"),
            configurator_url=os.getenv(
                "OVH_CONFIGURATOR_URL",
                "https://www.ovhcloud.com/en/vps/configurator/"
                "?planCode=vps-2027-model2.LZ&brick=VPS%2BModel%2B2"
                "&pricing=upfront12&processor=%20&vcore=4__vCore"
                "&storage=75__SSD__NVMe",
            ),
            state_file=Path(os.getenv("STATE_FILE", ".state/ovh-stock.json")),
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN"),
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID"),
        )

    def target_identity(self) -> dict[str, str]:
        return {
            "subsidiary": self.subsidiary,
            "plan_code": self.plan_code,
            "operating_system": self.operating_system,
            "datacenter": self.datacenter,
            "location_code": self.location_code,
        }


@dataclass(frozen=True)
class StockObservation:
    available: bool
    status: str
    linux_status: str
    datacenter: str
    location_code: str


@dataclass(frozen=True)
class StoredState:
    schema_version: int
    target: dict[str, str]
    available: bool
    status: str
    linux_status: str
    updated_at: str


@dataclass(frozen=True)
class ProcessResult:
    state_changed: bool
    notified: bool


def utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def build_ovh_session() -> requests.Session:
    """Return a session that retries transient, idempotent OVH requests."""
    retry = Retry(
        total=3,
        connect=3,
        read=3,
        status=3,
        other=0,
        backoff_factor=1,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session = requests.Session()
    session.mount("https://", adapter)
    session.headers.update(
        {
            "Accept": "application/json",
            "User-Agent": "ovh-stock-monitor/1.0 "
            "(+https://github.com/egeerend/ovh-stock-monitor)",
        }
    )
    return session


def _required_string(item: Mapping[str, Any], key: str) -> str:
    value = item.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ResponseFormatError(f"OVH datacenter entry has no valid {key!r} field")
    return value.strip()


def parse_stock_response(payload: Any, config: Config) -> StockObservation:
    """Select and validate the target datacenter from OVH's JSON response."""
    if not isinstance(payload, Mapping):
        raise ResponseFormatError("OVH response root is not a JSON object")

    datacenters = payload.get("datacenters")
    if not isinstance(datacenters, list):
        raise ResponseFormatError("OVH response has no datacenters array")

    matches: list[Mapping[str, Any]] = []
    for item in datacenters:
        if not isinstance(item, Mapping):
            raise ResponseFormatError("OVH datacenters contains a non-object entry")
        datacenter = item.get("datacenter")
        location_code = item.get("code")
        if (
            isinstance(datacenter, str)
            and isinstance(location_code, str)
            and datacenter.casefold() == config.datacenter.casefold()
            and location_code.casefold() == config.location_code.casefold()
        ):
            matches.append(item)

    if not matches:
        raise ResponseFormatError(
            "Target location was not present in OVH response: "
            f"datacenter={config.datacenter!r}, code={config.location_code!r}"
        )
    if len(matches) != 1:
        raise ResponseFormatError("OVH response contains duplicate target locations")

    match = matches[0]
    status = _required_string(match, "status").casefold()
    linux_status = _required_string(match, "linuxStatus").casefold()

    # The configurator enables an Ubuntu selection only when both signals are
    # available. Requiring both avoids a false positive from a partial stock state.
    available = status == AVAILABLE_STATUS and linux_status == AVAILABLE_STATUS
    return StockObservation(
        available=available,
        status=status,
        linux_status=linux_status,
        datacenter=_required_string(match, "datacenter"),
        location_code=_required_string(match, "code"),
    )


def fetch_stock(session: requests.Session, config: Config) -> StockObservation:
    params = {
        "ovhSubsidiary": config.subsidiary,
        "os": config.operating_system,
        "planCode": config.plan_code,
    }
    try:
        response = session.get(
            config.api_url,
            params=params,
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException:
        raise MonitorError("OVH stock request failed after retries") from None

    if not 200 <= response.status_code < 300:
        raise MonitorError(f"OVH stock endpoint returned HTTP {response.status_code}")
    try:
        payload = response.json()
    except (requests.exceptions.JSONDecodeError, ValueError):
        raise ResponseFormatError("OVH response is not valid JSON") from None
    return parse_stock_response(payload, config)


def load_state(path: Path) -> StoredState | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise TypeError("state root is not an object")
        target = payload["target"]
        if not isinstance(target, dict) or not all(
            isinstance(key, str) and isinstance(value, str)
            for key, value in target.items()
        ):
            raise ValueError("target is invalid")
        available = payload["available"]
        if not isinstance(available, bool):
            raise TypeError("available is not boolean")
        schema_version = int(payload["schema_version"])
        if schema_version != 1:
            raise ValueError(f"unsupported schema version {schema_version}")
        return StoredState(
            schema_version=schema_version,
            target=target,
            available=available,
            status=str(payload["status"]),
            linux_status=str(payload["linux_status"]),
            updated_at=str(payload["updated_at"]),
        )
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise StateError(f"Cached state is invalid: {exc}") from None


def write_state(path: Path, state: StoredState) -> None:
    """Atomically replace the state file on the current runner."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(f"{path.suffix}.tmp")
    serialized = json.dumps(asdict(state), indent=2, sort_keys=True) + "\n"
    try:
        temporary_path.write_text(serialized, encoding="utf-8")
        os.replace(temporary_path, path)
    except OSError as exc:
        raise StateError(f"Could not persist state: {exc}") from None


def build_notification(config: Config, checked_at: str) -> str:
    return "\n".join(
        (
            "OVH VPS STOCK AVAILABLE",
            f"Location: {config.location_label}",
            f"Plan: {config.plan_label} ({config.plan_code})",
            f"Checked at (UTC): {checked_at}",
            f"Configurator: {config.configurator_url}",
        )
    )


def send_telegram(
    session: requests.Session,
    bot_token: str | None,
    chat_id: str | None,
    message: str,
) -> None:
    if not bot_token or not chat_id:
        raise TelegramError(
            "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required for notification"
        )

    # Never log this URL: it contains the bot token.
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    try:
        response = session.post(
            url,
            json={
                "chat_id": chat_id,
                "text": message,
                "disable_web_page_preview": True,
            },
            timeout=REQUEST_TIMEOUT,
        )
    except requests.RequestException:
        raise TelegramError("Telegram request failed") from None

    if not 200 <= response.status_code < 300:
        raise TelegramError(f"Telegram endpoint returned HTTP {response.status_code}")
    try:
        payload = response.json()
    except (requests.exceptions.JSONDecodeError, ValueError):
        raise TelegramError("Telegram response is not valid JSON") from None
    if not isinstance(payload, Mapping) or payload.get("ok") is not True:
        raise TelegramError("Telegram did not accept the notification")


def process_observation(
    config: Config,
    observation: StockObservation,
    checked_at: str,
    notifier: Callable[[str], None],
) -> ProcessResult:
    """Apply transition rules and persist state only when its value changes."""
    try:
        previous = load_state(config.state_file)
    except StateError as exc:
        LOGGER.warning("%s; reinitializing without sending an alert", exc)
        previous = None

    if previous is not None and previous.target != config.target_identity():
        LOGGER.warning(
            "Cached state belongs to a different target; "
            "reinitializing without sending an alert"
        )
        previous = None

    if previous is not None and previous.available == observation.available:
        return ProcessResult(state_changed=False, notified=False)

    should_notify = (
        previous is not None
        and previous.available is False
        and observation.available is True
    )
    if should_notify:
        # Persist available only after Telegram acknowledges the message. If this
        # fails, the next scheduled run retries instead of losing the notification.
        notifier(build_notification(config, checked_at))

    new_state = StoredState(
        schema_version=1,
        target=config.target_identity(),
        available=observation.available,
        status=observation.status,
        linux_status=observation.linux_status,
        updated_at=checked_at,
    )
    write_state(config.state_file, new_state)
    return ProcessResult(state_changed=True, notified=should_notify)


def write_github_outputs(result: ProcessResult) -> None:
    output_path = os.getenv("GITHUB_OUTPUT")
    if not output_path:
        return
    try:
        with open(output_path, "a", encoding="utf-8") as output_file:
            output_file.write(
                f"state_changed={str(result.state_changed).lower()}\n"
                f"notified={str(result.notified).lower()}\n"
            )
    except OSError as exc:
        raise MonitorError(f"Could not write GitHub Actions outputs: {exc}") from None


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    config = Config.from_environment()
    checked_at = utc_now()

    try:
        with build_ovh_session() as ovh_session:
            observation = fetch_stock(ovh_session, config)
        LOGGER.info(
            "OVH result for %s (%s): status=%s linuxStatus=%s available=%s",
            config.location_label,
            config.location_code,
            observation.status,
            observation.linux_status,
            observation.available,
        )

        with requests.Session() as telegram_session:
            result = process_observation(
                config,
                observation,
                checked_at,
                lambda message: send_telegram(
                    telegram_session,
                    config.telegram_bot_token,
                    config.telegram_chat_id,
                    message,
                ),
            )
        write_github_outputs(result)
    except MonitorError as exc:
        LOGGER.error("Stock check failed: %s", exc)
        return 1

    if result.notified:
        LOGGER.info("Restock transition detected; Telegram notification sent")
    elif result.state_changed:
        LOGGER.info("Persistent state initialized or updated; no alert required")
    else:
        LOGGER.info("Stock state is unchanged; no alert sent")
    return 0


if __name__ == "__main__":
    sys.exit(main())
