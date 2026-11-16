from database import get_connection
import secrets
import string

def next_candidate_number(cursor):
    cursor.execute("""
        UPDATE candidate_sequence SET last_number = last_number + 1
        WHERE id = 1 RETURNING last_number
    """)
    return f"ERN-{cursor.fetchone()['last_number']:03d}"


def generate_candidate_number(cursor=None):
    if cursor is not None:
        return next_candidate_number(cursor)
    connection = get_connection()
    try:
        number = next_candidate_number(connection.cursor())
        connection.commit()
        return number

    except Exception:
        connection.rollback()
        raise

    finally:
        connection.close()

def generate_security_code(length=4):
    characters = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(characters) for _ in range(length))


def create_interview_identity(cursor=None):
    candidate_number = generate_candidate_number(cursor)
    security_code = generate_security_code()

    interview_id = f"{candidate_number}-{security_code}"
    interview_link = f"https://ernasa.ai/interview/{interview_id}"

    return {
        "candidate_number": candidate_number,
        "security_code": security_code,
        "interview_id": interview_id,
        "interview_link": interview_link
    }
