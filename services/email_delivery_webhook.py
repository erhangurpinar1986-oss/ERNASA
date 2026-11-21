"""Verified Resend delivery webhooks for interview invitations.

No external calls; signature is checked before parsing or writing.
"""
import base64
import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timezone

from database import get_connection


EVENT_STATES = {
    "email.sent": "sent",
    "email.delivered": "delivered",
    "email.bounced": "bounced",
    "email.complained": "complained",
    "email.delivery_delayed": "delayed",
}
RANK = {"sent": 1, "delayed": 2, "delivered": 3, "bounced": 4, "complained": 5}


def verify_signature(body, headers, *, secret=None, now=None):
    secret = secret if secret is not None else os.getenv("ERNASA_RESEND_WEBHOOK_SECRET", "")
    if not secret or not secret.startswith("whsec_"):
        return False
    msg_id = headers.get("svix-id", "")
    timestamp = headers.get("svix-timestamp", "")
    signature = headers.get("svix-signature", "")
    if not msg_id or not timestamp or not signature:
        return False
    try:
        stamp = int(timestamp)
        if abs((time.time() if now is None else now) - stamp) > 300:
            return False
        key = base64.b64decode(secret[6:], validate=True)
        payload = msg_id.encode() + b"." + timestamp.encode() + b"." + body
        digest = base64.b64encode(hmac.new(key, payload, hashlib.sha256).digest()).decode()
        return any(hmac.compare_digest(digest, item[3:]) for item in signature.split(" ") if item.startswith("v1,"))
    except (ValueError, UnicodeError):
        return False


def accept_event(body, headers):
    """Persist authenticated event, never trust caller-supplied candidate identity."""
    if not verify_signature(body, headers):
        raise ValueError("invalid webhook signature")
    try:
        event = json.loads(body)
    except (ValueError, UnicodeError):
        raise ValueError("invalid webhook body") from None
    if not isinstance(event, dict):
        raise ValueError("invalid webhook body")
    kind = event.get("type")
    if kind not in EVENT_STATES:
        return "ignored"
    data = event.get("data")
    provider_id = data.get("email_id") if isinstance(data, dict) else None
    if not isinstance(provider_id, str) or not provider_id or len(provider_id) > 255:
        raise ValueError("invalid provider message id")
    event_id = headers["svix-id"]
    if len(event_id) > 255:
        raise ValueError("invalid webhook id")
    stamp = datetime.now(timezone.utc).isoformat()
    connection = get_connection()
    try:
        cursor = connection.cursor()
        cursor.execute("""INSERT INTO ik_email_delivery_events
            (event_id,provider_message_id,event_type,received_at)
            VALUES (?,?,?,?) ON CONFLICT(event_id) DO NOTHING""",
            (event_id, provider_id, kind, stamp))
        if not cursor.rowcount:
            connection.commit()
            return "duplicate"
        cursor.execute("""SELECT delivery_status FROM ik_email_delivery_states
            WHERE provider_message_id=?""", (provider_id,))
        previous = cursor.fetchone()
        old = previous["delivery_status"] if previous else None
        new = EVENT_STATES[kind]
        if old is None or RANK[new] >= RANK.get(old, 0):
            cursor.execute("""INSERT INTO ik_email_delivery_states
                (provider_message_id,delivery_status,updated_at)
                SELECT provider_message_id,?,? FROM ik_invitation_email_sends
                WHERE provider_message_id=?
                ON CONFLICT(provider_message_id) DO UPDATE SET
                delivery_status=excluded.delivery_status,updated_at=excluded.updated_at""",
                (new,stamp,provider_id))
        connection.commit()
        return "accepted"
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def reconcile_provider_events(cursor, provider_id):
    """Apply signed events that arrived before the send record was committed.

    Call inside the transaction that stores the provider message id. This
    preserves the strongest known delivery state without reprocessing events.
    """
    if not provider_id:
        return None
    cursor.execute("""SELECT event_type FROM ik_email_delivery_events
        WHERE provider_message_id=?""", (provider_id,))
    states = [EVENT_STATES[row["event_type"]] for row in cursor.fetchall()
              if row["event_type"] in EVENT_STATES]
    if not states:
        return None
    new = max(states, key=lambda state: RANK[state])
    cursor.execute("""SELECT delivery_status FROM ik_email_delivery_states
        WHERE provider_message_id=?""", (provider_id,))
    previous = cursor.fetchone()
    if previous and RANK.get(previous["delivery_status"], 0) > RANK[new]:
        return previous["delivery_status"]
    stamp = datetime.now(timezone.utc).isoformat()
    cursor.execute("""INSERT INTO ik_email_delivery_states
        (provider_message_id,delivery_status,updated_at)
        SELECT provider_message_id,?,? FROM ik_invitation_email_sends
        WHERE provider_message_id=?
        ON CONFLICT(provider_message_id) DO UPDATE SET
        delivery_status=excluded.delivery_status,updated_at=excluded.updated_at""",
        (new,stamp,provider_id))
    return new
