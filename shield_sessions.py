import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from database import get_connection

SESSION_MAX_HOURS = 8
SESSION_IDLE_MINUTES = 15


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_ik_session(username: str) -> str:
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)

    connection = get_connection()
    try:
        cursor = connection.cursor()
        cursor.execute(
            """INSERT INTO ik_sessions
            (token_hash, username, created_at, expires_at,
             revoked_at, last_activity_at)
            VALUES (?, ?, ?, ?, NULL, ?)""",
            (
                token_digest(token),
                username,
                now.isoformat(),
                (now + timedelta(hours=SESSION_MAX_HOURS)).isoformat(),
                now.isoformat()
            )
        )
        connection.commit()
    finally:
        connection.close()

    return token


def verify_ik_session(token: str) -> bool:
    if not token or len(token) > 256:
        return False

    now = datetime.now(timezone.utc)
    connection = get_connection()

    try:
        cursor = connection.cursor()
        cursor.execute(
            """SELECT expires_at, revoked_at, last_activity_at
               FROM ik_sessions WHERE token_hash = ?""",
            (token_digest(token),)
        )
        row = cursor.fetchone()

        if not row or row[1] is not None or not row[2]:
            return False

        expires = datetime.fromisoformat(row[0])
        last_activity = datetime.fromisoformat(row[2])

        if now >= expires or now - last_activity >= timedelta(
            minutes=SESSION_IDLE_MINUTES
        ):
            return False

        cursor.execute(
            """UPDATE ik_sessions
               SET last_activity_at = ?
               WHERE token_hash = ?
               AND revoked_at IS NULL
               AND expires_at > ?
               AND last_activity_at > ?
               AND last_activity_at <= ?""",
            (
                now.isoformat(),
                token_digest(token),
                now.isoformat(),
                (now - timedelta(minutes=SESSION_IDLE_MINUTES)).isoformat(),
                now.isoformat()
            )
        )
        updated = cursor.rowcount == 1
        connection.commit()
        return updated
    finally:
        connection.close()


def revoke_ik_session(token: str) -> None:
    if not token or len(token) > 256:
        return

    connection = get_connection()
    try:
        cursor = connection.cursor()
        cursor.execute(
            """UPDATE ik_sessions SET revoked_at = ?
               WHERE token_hash = ? AND revoked_at IS NULL""",
            (datetime.now(timezone.utc).isoformat(), token_digest(token))
        )
        connection.commit()
    finally:
        connection.close()
