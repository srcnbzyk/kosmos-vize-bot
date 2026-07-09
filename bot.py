import base64
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta
from enum import Enum

from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad
from curl_cffi import requests
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("kosmos-bot")


class ApplicationType(Enum):
    INDIVIDUAL = 1  # Bireysel
    FAMILY = 2  # Aile


class AppointmentTypeId(Enum):
    STANDARD = 16  # Standart
    VIP = 18  # VIP
    EEA_AB_SPOUSE = 2339  # EEA / AB esi
    BT = 2472  # Yeni tip (frontend: appointmentTypeBT)


API_URL = (
    "https://api.kosmosvize.com.tr/api/AppointmentLayouts/GetAppointmentHourQoutaInfo"
)

# Frontend ile ayni AES anahtari (index-CvVM7A5V.js)
QUOTA_AES_KEY = bytes.fromhex("6152c3b6c39c40392f2ac2bd26372624c2a35d5f3f2fc3a7")
QUOTA_AES_IV = b"0000000000000000"

# (faz suresi saniye, bildirim araligi saniye)
# Son faz sure=None => "Tamam." gelene kadar devam
REMINDER_PHASES = [
    (60, 10),  # ilk 1 dk: 10 sn'de bir
    (120, 30),  # sonraki 2 dk: 30 sn'de bir
    (420, 60),  # sonraki 7 dk: 1 dk'da bir
    (None, 60),  # sonrasinda da 1 dk'da bir, durdurulana kadar
]

STOP_PATTERN = re.compile(r"^tamam[.!]?$", re.IGNORECASE)
POLL_CHUNK_SECONDS = 2


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        logger.error("Missing required env var: %s", name)
        sys.exit(1)
    return value


def load_config():
    application_type_name = os.getenv("APPLICATION_TYPE", "INDIVIDUAL").strip().upper()
    appointment_type_name = os.getenv("APPOINTMENT_TYPE", "STANDARD").strip().upper()

    try:
        application_type = ApplicationType[application_type_name]
    except KeyError:
        logger.error(
            "Invalid APPLICATION_TYPE=%s (use: %s)",
            application_type_name,
            ", ".join(t.name for t in ApplicationType),
        )
        sys.exit(1)

    try:
        appointment_type = AppointmentTypeId[appointment_type_name]
    except KeyError:
        logger.error(
            "Invalid APPOINTMENT_TYPE=%s (use: %s)",
            appointment_type_name,
            ", ".join(t.name for t in AppointmentTypeId),
        )
        sys.exit(1)

    try:
        dealer_id = int(require_env("DEALER_ID"))
        days_ahead = int(os.getenv("DAYS_AHEAD", "30"))
        check_interval = int(os.getenv("CHECK_INTERVAL_SECONDS", "300"))
    except ValueError as exc:
        logger.error("Invalid numeric config: %s", exc)
        sys.exit(1)

    return {
        "telegram_bot_token": require_env("TELEGRAM_BOT_TOKEN"),
        "chat_id": require_env("TELEGRAM_CHAT_ID"),
        "nationality_number": require_env("NATIONALITY_NUMBER"),
        "dealer_id": dealer_id,
        "application_type": application_type,
        "appointment_type": appointment_type,
        "auth_token": os.getenv("AUTH_TOKEN", "").strip(),
        "recaptcha_token": os.getenv("RECAPTCHA_TOKEN", "").strip(),
        "days_ahead": days_ahead,
        "check_interval": check_interval,
    }


def build_headers(auth_token: str | None = None) -> dict:
    headers = {
        "accept": "application/json",
        "accept-language": "en-US,en;q=0.9",
        "origin": "https://basvuru.kosmosvize.com.tr",
        "referer": "https://basvuru.kosmosvize.com.tr/",
        "user-agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
    }
    if auth_token:
        headers["Authorization"] = f"Bearer {auth_token}"
    return headers


def decrypt_quota_payload(encrypted_b64: str) -> list:
    raw = base64.b64decode(encrypted_b64)
    cipher = AES.new(QUOTA_AES_KEY, AES.MODE_CBC, QUOTA_AES_IV)
    plain = unpad(cipher.decrypt(raw), AES.block_size).decode("utf-8")
    data = json.loads(plain)
    return data if isinstance(data, list) else []


def parse_quota_response(payload) -> list:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, str):
        if not payload.strip():
            return []
        return decrypt_quota_payload(payload)
    return []


def get_appointment_hour_quota_info(
    nationality_number,
    dealer_id,
    date,
    application_type,
    appointment_type,
    auth_token="",
    recaptcha_token="",
    only_available=True,
):
    query_params = {
        "nationalityNumber": nationality_number,
        "dealerId": dealer_id,
        "date": date,
        "appointmentTypeId": appointment_type.value,
        "onlyAvailable": str(only_available).lower(),
        "applicationType": application_type.value,
    }
    if recaptcha_token:
        query_params["recaptchaToken"] = recaptcha_token

    try:
        response = requests.get(
            API_URL,
            headers=build_headers(auth_token),
            params=query_params,
            timeout=30,
            impersonate="chrome",
        )
    except requests.RequestsError as exc:
        raise RuntimeError(f"API request failed: {exc}") from exc

    if response.status_code == 401:
        raise PermissionError(
            "API 401 Unauthorized. AUTH_TOKEN (Bearer) gerekli olabilir."
        )
    if response.status_code == 403:
        raise PermissionError("API 403 Forbidden. IP/WAF veya captcha engeli olabilir.")
    if response.status_code >= 400:
        raise RuntimeError(
            f"API HTTP {response.status_code}: {(response.text or '')[:300]}"
        )

    if not response.content:
        return []

    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(
            f"API JSON parse hatasi: {(response.text or '')[:300]}"
        ) from exc

    try:
        return parse_quota_response(payload)
    except Exception as exc:
        raise RuntimeError(f"API yanit cozumleme hatasi: {exc}") from exc


def send_telegram_message(token: str, chat_id: str, message: str) -> bool:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        response = requests.post(
            url,
            json={"chat_id": chat_id, "text": message},
            timeout=30,
            impersonate="chrome",
        )
        if response.status_code >= 400:
            logger.error(
                "Telegram HTTP %s: %s",
                response.status_code,
                (response.text or "")[:300],
            )
            return False
        return True
    except requests.RequestsError as exc:
        logger.error("Telegram request failed: %s", exc)
        return False
    except Exception as exc:
        logger.error("Telegram unexpected error: %s", exc)
        return False


def is_stop_message(text: str | None) -> bool:
    if not text:
        return False
    return bool(STOP_PATTERN.match(text.strip()))


def get_telegram_updates(token: str, offset: int | None = None) -> tuple[list, int | None]:
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    params = {"timeout": 0, "allowed_updates": ["message"]}
    if offset is not None:
        params["offset"] = offset

    try:
        response = requests.get(url, params=params, timeout=30, impersonate="chrome")
    except requests.RequestsError as exc:
        logger.error("Telegram getUpdates failed: %s", exc)
        return [], offset

    if response.status_code >= 400:
        logger.error(
            "Telegram getUpdates HTTP %s: %s",
            response.status_code,
            (response.text or "")[:300],
        )
        return [], offset

    try:
        payload = response.json()
    except ValueError:
        logger.error("Telegram getUpdates JSON parse hatasi")
        return [], offset

    if not payload.get("ok"):
        logger.error("Telegram getUpdates not ok: %s", payload)
        return [], offset

    updates = payload.get("result") or []
    next_offset = offset
    for update in updates:
        update_id = update.get("update_id")
        if isinstance(update_id, int):
            next_offset = update_id + 1
    return updates, next_offset


def chat_requested_stop(token: str, chat_id: str, offset: int | None) -> tuple[bool, int | None]:
    updates, next_offset = get_telegram_updates(token, offset)
    for update in updates:
        message = update.get("message") or {}
        msg_chat = message.get("chat") or {}
        if str(msg_chat.get("id")) != str(chat_id):
            continue
        if is_stop_message(message.get("text")):
            return True, next_offset
    return False, next_offset


def wait_or_stop(token: str, chat_id: str, seconds: float, offset: int | None) -> tuple[bool, int | None]:
    """Belirtilen sure kadar bekle; arada 'Tamam.' gelirse True don."""
    deadline = time.monotonic() + max(0.0, seconds)
    while True:
        stopped, offset = chat_requested_stop(token, chat_id, offset)
        if stopped:
            return True, offset

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False, offset

        time.sleep(min(POLL_CHUNK_SECONDS, remaining))


def reminder_interval_at(elapsed: float) -> float:
    cursor = 0.0
    for duration, interval in REMINDER_PHASES:
        if duration is None:
            return float(interval)
        if elapsed < cursor + duration:
            return float(interval)
        cursor += duration
    return float(REMINDER_PHASES[-1][1])


def alert_until_acknowledged(config: dict, base_message: str, update_offset: int | None) -> int | None:
    token = config["telegram_bot_token"]
    chat_id = config["chat_id"]
    started = time.monotonic()
    reminder_count = 0

    intro = (
        f"{base_message}\n\n"
        "Hatirlatmalar basladi. Durdurmak icin bu sohbete 'Tamam.' yaz."
    )
    send_telegram_message(token, chat_id, intro)
    logger.info("Alert started; waiting for 'Tamam.' acknowledgment")

    while True:
        elapsed = time.monotonic() - started
        interval = reminder_interval_at(elapsed)
        stopped, update_offset = wait_or_stop(token, chat_id, interval, update_offset)
        if stopped:
            send_telegram_message(token, chat_id, "Tamam, hatirlatmalar durduruldu.")
            logger.info("Alert stopped by user acknowledgment")
            return update_offset

        reminder_count += 1
        elapsed = time.monotonic() - started
        reminder = (
            f"[Hatirlatma #{reminder_count}] {base_message}\n\n"
            "Durdurmak icin 'Tamam.' yaz."
        )
        send_telegram_message(token, chat_id, reminder)
        logger.info(
            "Sent reminder #%s (elapsed=%.0fs, phase_interval=%ss)",
            reminder_count,
            elapsed,
            reminder_interval_at(elapsed),
        )


def find_available_appointments(config: dict) -> list[tuple[str, list]]:
    found = []
    for i in range(config["days_ahead"]):
        date = (datetime.now() + timedelta(days=i)).strftime("%Y/%m/%d")
        try:
            result = get_appointment_hour_quota_info(
                config["nationality_number"],
                config["dealer_id"],
                date,
                config["application_type"],
                config["appointment_type"],
                auth_token=config["auth_token"],
                recaptcha_token=config["recaptcha_token"],
            )
        except PermissionError as exc:
            logger.error("%s (date=%s)", exc, date)
            break
        except Exception as exc:
            logger.exception("Quota check failed for %s: %s", date, exc)
            continue

        if result and isinstance(result, list) and len(result) > 0:
            logger.info("Availability found on %s: %s", date, result)
            found.append((date, result))
        else:
            logger.info("No availability on %s", date)
    return found


def main() -> None:
    config = load_config()
    logger.info(
        "Starting bot dealer_id=%s application=%s appointment=%s days=%s interval=%ss",
        config["dealer_id"],
        config["application_type"].name,
        config["appointment_type"].name,
        config["days_ahead"],
        config["check_interval"],
    )
    if not config["auth_token"]:
        logger.warning(
            "AUTH_TOKEN bos. Guncel API Bearer bekliyor; istekler 401 donebilir."
        )
    if not config["recaptcha_token"]:
        logger.warning(
            "RECAPTCHA_TOKEN bos. Frontend artik recaptchaToken gonderiyor."
        )

    # Eski bekleyen mesajlari atla; sadece bundan sonraki "Tamam." dinlensin.
    _, update_offset = get_telegram_updates(config["telegram_bot_token"], None)
    if update_offset is not None:
        logger.info("Telegram update offset initialized at %s", update_offset)

    while True:
        try:
            found = find_available_appointments(config)
            if found:
                lines = [
                    f"{date}: {slots}" for date, slots in found
                ]
                message = "Available appointments:\n" + "\n".join(lines)
                update_offset = alert_until_acknowledged(
                    config, message, update_offset
                )
            else:
                logger.info("No availability in scanned range")
        except Exception as exc:
            logger.exception("Unexpected loop error: %s", exc)

        logger.info("Sleeping %s seconds...", config["check_interval"])
        time.sleep(config["check_interval"])


if __name__ == "__main__":
    main()
