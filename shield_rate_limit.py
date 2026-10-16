import hashlib
import hmac
import os
import secrets
from datetime import datetime, timedelta, timezone

from database import get_connection

MAX_FAILURES = 5
WINDOW_MINUTES = 10
BLOCK_MINUTES = 15


def get_login_identity(request):
    """Proxy dogrulanana kadar dogrudan baglanti adresini kullan."""
    return request.client.host if request.client else "unknown"


def hash_login_identity(identity):
    secret = os.getenv("IK_SESSION_SECRET", "")
    if not secret:
        raise RuntimeError("IK_SESSION_SECRET tanimlanmamis.")

    return hmac.new(
        secret.encode("utf-8"),
        identity.encode("utf-8"),
        hashlib.sha256
    ).hexdigest()


def record_failed_login(ip_hash):
    connection = get_connection()
    try:
        cursor = connection.cursor()
        cursor.execute(
            """INSERT INTO ik_login_attempts
               (id, ip_hash, attempted_at)
               VALUES (?, ?, ?)""",
            (
                secrets.token_hex(16),
                ip_hash,
                datetime.now(timezone.utc).isoformat()
            )
        )
        connection.commit()
    finally:
        connection.close()

def is_login_blocked(ip_hash):
    """Son 10 dakikadaki 5 hatali giristen sonra 15 dakika engelle."""
    now = datetime.now(timezone.utc)
    cutoff = (now - timedelta(minutes=WINDOW_MINUTES + BLOCK_MINUTES)).isoformat()

    connection = get_connection()
    try:
        cursor = connection.cursor()
        cursor.execute(
            """SELECT attempted_at
               FROM ik_login_attempts
               WHERE ip_hash = ? AND attempted_at >= ?
               ORDER BY attempted_at ASC""",
            (ip_hash, cutoff)
        )
        attempts = [
            datetime.fromisoformat(row[0])
            for row in cursor.fetchall()
        ]
    finally:
        connection.close()

    for i in range(len(attempts) - MAX_FAILURES + 1):
        group = attempts[i:i + MAX_FAILURES]

        if group[-1] - group[0] <= timedelta(minutes=WINDOW_MINUTES):
            if now < group[-1] + timedelta(minutes=BLOCK_MINUTES):
                return True

    return False

def get_database_type():
    """Aktif veritabani turunu belirle."""
    from database import DATABASE_URL
    return "postgresql" if DATABASE_URL else "sqlite"

def lock_login_transaction(connection, cursor, ip_hash):
    """Ayni kaynagin giris kontrollerini siraya al."""
    if get_database_type() == "postgresql":
        lock_key = int.from_bytes(
            bytes.fromhex(ip_hash)[:8],
            byteorder="big",
            signed=True
        )
        cursor.execute(
            "SELECT pg_advisory_xact_lock(?)",
            (lock_key,)
        )
    else:
        cursor.execute("BEGIN IMMEDIATE")

def count_recent_failures(cursor, ip_hash, now):
    """Son 10 dakikadaki basarisiz giris sayisi."""
    cutoff = (now - timedelta(minutes=WINDOW_MINUTES)).isoformat()

    cursor.execute(
        """SELECT COUNT(*)
           FROM ik_login_attempts
           WHERE ip_hash = ? AND attempted_at >= ?""",
        (ip_hash, cutoff)
    )

    return cursor.fetchone()[0]

def check_login_limit(ip_hash):
    """Giris engelini veritabani kilidi altinda kontrol et."""
    connection = get_connection()
    try:
        cursor = connection.cursor()
        lock_login_transaction(connection, cursor, ip_hash)

        now = datetime.now(timezone.utc)
        cutoff = (
            now - timedelta(minutes=WINDOW_MINUTES + BLOCK_MINUTES)
        ).isoformat()

        cursor.execute(
            """SELECT attempted_at
               FROM ik_login_attempts
               WHERE ip_hash = ? AND attempted_at >= ?
               ORDER BY attempted_at ASC""",
            (ip_hash, cutoff)
        )

        attempts = [
            datetime.fromisoformat(row[0])
            for row in cursor.fetchall()
        ]

        blocked = any(
            attempts[i + MAX_FAILURES - 1] - attempts[i]
            <= timedelta(minutes=WINDOW_MINUTES)
            and now < attempts[i + MAX_FAILURES - 1]
            + timedelta(minutes=BLOCK_MINUTES)
            for i in range(len(attempts) - MAX_FAILURES + 1)
        )

        connection.commit()
        return blocked
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

def register_failed_login_atomically(ip_hash):
    """Kilidi tutarken engeli kontrol et ve hatali girisi kaydet."""
    connection = get_connection()
    try:
        cursor = connection.cursor()
        lock_login_transaction(connection, cursor, ip_hash)

        now = datetime.now(timezone.utc)
        cutoff = (
            now - timedelta(minutes=WINDOW_MINUTES + BLOCK_MINUTES)
        ).isoformat()

        cursor.execute(
            """SELECT attempted_at
               FROM ik_login_attempts
               WHERE ip_hash = ? AND attempted_at >= ?
               ORDER BY attempted_at ASC""",
            (ip_hash, cutoff)
        )

        attempts = [
            datetime.fromisoformat(row[0])
            for row in cursor.fetchall()
        ]

        blocked = any(
            attempts[i + MAX_FAILURES - 1] - attempts[i]
            <= timedelta(minutes=WINDOW_MINUTES)
            and now < attempts[i + MAX_FAILURES - 1]
            + timedelta(minutes=BLOCK_MINUTES)
            for i in range(len(attempts) - MAX_FAILURES + 1)
        )

        if blocked:
            connection.commit()
            return True

        cursor.execute(
            """INSERT INTO ik_login_attempts
               (id, ip_hash, attempted_at)
               VALUES (?, ?, ?)""",
            (secrets.token_hex(16), ip_hash, now.isoformat())
        )

        connection.commit()
        return False

    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
