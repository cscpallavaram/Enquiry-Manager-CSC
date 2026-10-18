"""
WhatsApp Business Cloud API (Meta) helper.

Credentials are read from environment variables — never hardcode them.
Failed sends are logged and never block the main enquiry workflow.
"""

import os
import re
import logging
import requests

logger = logging.getLogger("whatsapp")

# ---------------------------------------------------------------------------
# Config from environment
# ---------------------------------------------------------------------------
WHATSAPP_TOKEN = os.environ.get("WHATSAPP_TOKEN", "")
WHATSAPP_PHONE_NUMBER_ID = os.environ.get("WHATSAPP_PHONE_NUMBER_ID", "")
WHATSAPP_VERIFY_TOKEN = os.environ.get("WHATSAPP_VERIFY_TOKEN", "enquiry_verify_token")
WHATSAPP_API_VERSION = os.environ.get("WHATSAPP_API_VERSION", "v21.0")
# Default country code when a local number is stored without + / country prefix
WHATSAPP_DEFAULT_COUNTRY = os.environ.get("WHATSAPP_DEFAULT_COUNTRY", "91")

# Template names must match templates approved in Meta Business Manager
TEMPLATE_NEW_ENQUIRY = os.environ.get("WHATSAPP_TEMPLATE_NEW", "enquiry_ack")
TEMPLATE_STATUS_UPDATE = os.environ.get("WHATSAPP_TEMPLATE_STATUS", "enquiry_status_update")

API_BASE = f"https://graph.facebook.com/{WHATSAPP_API_VERSION}"


def is_configured() -> bool:
    """Return True if required credentials are present."""
    return bool(WHATSAPP_TOKEN and WHATSAPP_PHONE_NUMBER_ID)


def normalize_phone(raw: str, default_country: str = None) -> str | None:
    """
    Normalize a phone number to digits-only international format (no +).
    WhatsApp Cloud API expects e.g. 919876543210.

    - Strips spaces, dashes, parentheses
    - Removes leading +
    - If number is 10 digits (typical IN mobile), prefixes default country code
    - Returns None if the result is not a plausible mobile number
    """
    if not raw:
        return None
    country = default_country or WHATSAPP_DEFAULT_COUNTRY
    digits = re.sub(r"[^\d]", "", str(raw).strip())
    if not digits:
        return None
    # Already has country code (11–15 digits)
    if len(digits) >= 11 and len(digits) <= 15:
        return digits
    # Local 10-digit mobile → prepend country
    if len(digits) == 10 and country:
        return f"{country}{digits}"
    # 11–12 digits starting with country already, or other short forms
    if len(digits) >= 8:
        return digits
    return None


def _find_phone_in_data(data: dict) -> str | None:
    """
    Best-effort lookup of a phone field in enquiry JSON data.
    Checks common key names used in CSV imports.
    """
    if not data:
        return None
    preferred = (
        "phone", "mobile", "mobi", "mobileno", "mobile_no", "phone_number",
        "contact", "contact_no", "whatsapp", "cell", "cellphone",
    )
    lower_map = {str(k).lower().replace(" ", "_"): v for k, v in data.items()}
    for key in preferred:
        val = lower_map.get(key)
        if val and str(val).strip():
            return str(val).strip()
    # Fallback: any key containing 'phone' or 'mobi'
    for key, val in lower_map.items():
        if val and ("phone" in key or "mobi" in key or "mobile" in key):
            return str(val).strip()
    return None


def get_enquiry_phone(enquiry) -> str | None:
    """Extract and normalize phone from an Enquiry model instance."""
    raw = _find_phone_in_data(enquiry.get_data())
    return normalize_phone(raw) if raw else None


def send_template_message(
    to_phone: str,
    template_name: str,
    language_code: str = "en",
    body_params: list | None = None,
) -> dict:
    """
    Send an approved template message (required for business-initiated chats).

    body_params: list of strings for {{1}}, {{2}}, … in the template body.

    Returns dict: {"ok": bool, "message_id": str|None, "error": str|None, "raw": dict}
    """
    if not is_configured():
        return {"ok": False, "message_id": None, "error": "WhatsApp not configured (missing env vars)", "raw": {}}

    phone = normalize_phone(to_phone)
    if not phone:
        return {"ok": False, "message_id": None, "error": f"Invalid phone number: {to_phone!r}", "raw": {}}

    url = f"{API_BASE}/{WHATSAPP_PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    components = []
    if body_params:
        components.append({
            "type": "body",
            "parameters": [{"type": "text", "text": str(p)} for p in body_params],
        })

    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": language_code},
        },
    }
    if components:
        payload["template"]["components"] = components

    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=15)
        data = resp.json() if resp.content else {}
        if resp.status_code in (200, 201):
            msg_id = None
            try:
                msg_id = data["messages"][0]["id"]
            except (KeyError, IndexError, TypeError):
                pass
            logger.info("WhatsApp template sent to %s template=%s id=%s", phone, template_name, msg_id)
            return {"ok": True, "message_id": msg_id, "error": None, "raw": data}
        err = data.get("error", {})
        err_msg = err.get("message") or data.get("message") or resp.text[:300]
        logger.error("WhatsApp API error %s: %s", resp.status_code, err_msg)
        return {"ok": False, "message_id": None, "error": err_msg, "raw": data}
    except requests.RequestException as exc:
        logger.exception("WhatsApp request failed: %s", exc)
        return {"ok": False, "message_id": None, "error": str(exc), "raw": {}}


def send_text_message(to_phone: str, body: str) -> dict:
    """
    Send a free-form text message.
    Only allowed within the 24-hour customer service window after the user
    messaged you, or for testing with numbers added as test recipients.
    Prefer templates for business-initiated outreach.
    """
    if not is_configured():
        return {"ok": False, "message_id": None, "error": "WhatsApp not configured", "raw": {}}

    phone = normalize_phone(to_phone)
    if not phone:
        return {"ok": False, "message_id": None, "error": f"Invalid phone: {to_phone!r}", "raw": {}}

    url = f"{API_BASE}/{WHATSAPP_PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": phone,
        "type": "text",
        "text": {"preview_url": False, "body": body[:4096]},
    }
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=15)
        data = resp.json() if resp.content else {}
        if resp.status_code in (200, 201):
            msg_id = None
            try:
                msg_id = data["messages"][0]["id"]
            except (KeyError, IndexError, TypeError):
                pass
            logger.info("WhatsApp text sent to %s id=%s", phone, msg_id)
            return {"ok": True, "message_id": msg_id, "error": None, "raw": data}
        err = data.get("error", {})
        err_msg = err.get("message") or resp.text[:300]
        logger.error("WhatsApp API error %s: %s", resp.status_code, err_msg)
        return {"ok": False, "message_id": None, "error": err_msg, "raw": data}
    except requests.RequestException as exc:
        logger.exception("WhatsApp request failed: %s", exc)
        return {"ok": False, "message_id": None, "error": str(exc), "raw": {}}


def notify_new_enquiry(enquiry) -> dict:
    """
    Send acknowledgement template when a new enquiry is created.
    Non-blocking: caller should ignore failure and continue.
    Template body params: {{1}} = student/name, {{2}} = enquiry id
    """
    phone = get_enquiry_phone(enquiry)
    if not phone:
        return {"ok": False, "message_id": None, "error": "No phone number on enquiry", "raw": {}}

    data = enquiry.get_data()
    name = (
        data.get("name")
        or data.get("student_name")
        or data.get("fname")
        or data.get("student")
        or "Student"
    )
    return send_template_message(
        to_phone=phone,
        template_name=TEMPLATE_NEW_ENQUIRY,
        body_params=[str(name), str(enquiry.id)],
    )


def notify_status_update(enquiry, old_status: str = None) -> dict:
    """
    Send status-update template when enquiry status changes.
    Template body params: {{1}} = name, {{2}} = new status, {{3}} = enquiry id
    """
    phone = get_enquiry_phone(enquiry)
    if not phone:
        return {"ok": False, "message_id": None, "error": "No phone number on enquiry", "raw": {}}

    data = enquiry.get_data()
    name = (
        data.get("name")
        or data.get("student_name")
        or data.get("fname")
        or data.get("student")
        or "Student"
    )
    status_label = (enquiry.status or "").replace("_", " ").title()
    return send_template_message(
        to_phone=phone,
        template_name=TEMPLATE_STATUS_UPDATE,
        body_params=[str(name), status_label, str(enquiry.id)],
    )
