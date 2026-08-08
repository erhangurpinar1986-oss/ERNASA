import sqlite3
from pathlib import Path
DATABASE_PATH = Path(__file__).resolve().parent / "ernasa.db"
def get_connection():
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    return connection
def initialize_database():
    connection = get_connection()
    cursor = connection.cursor()

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS interview_links (
            token TEXT PRIMARY KEY,
            name TEXT,
            phone TEXT,
            email TEXT,
            company TEXT,
            position TEXT,
            expires_at TEXT,
            status TEXT
        )
    """)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS candidate_sequence (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            last_number INTEGER NOT NULL
        )
    """)

    cursor.execute("""
        INSERT OR IGNORE INTO candidate_sequence (id, last_number)
        VALUES (1, 0)
    """)
    connection.commit()
    connection.close()