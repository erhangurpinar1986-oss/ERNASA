from database import get_connection
import secrets
import string

def generate_candidate_number():
    connection = get_connection()
    cursor = connection.cursor()

    try:
        cursor.execute("""
            SELECT last_number
            FROM candidate_sequence
            WHERE id = 1
        """)

        row = cursor.fetchone()
        next_number = row["last_number"] + 1

        cursor.execute("""
            UPDATE candidate_sequence
            SET last_number = ?
            WHERE id = 1
        """, (next_number,))

        connection.commit()

        return f"ERN-{next_number:03d}"

    except Exception:
        connection.rollback()
        raise

    finally:
        connection.close()

def generate_security_code(length=4):
    characters = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(characters) for _ in range(length))


def create_interview_identity():
    candidate_number = generate_candidate_number()
    security_code = generate_security_code()

    interview_id = f"{candidate_number}-{security_code}"
    interview_link = f"https://ernasa.ai/interview/{interview_id}"

    return {
        "candidate_number": candidate_number,
        "security_code": security_code,
        "interview_id": interview_id,
        "interview_link": interview_link
    }
