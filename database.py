import sqlite3
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent
DB_NAME = BASE_DIR / "p24tex.db"


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db() -> None:
    with get_connection() as conn:
        cursor = conn.cursor()

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS objects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                is_active INTEGER NOT NULL DEFAULT 1
            )
            """
        )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER UNIQUE NOT NULL,
                full_name TEXT,
                current_object_id INTEGER,
                language TEXT DEFAULT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                FOREIGN KEY (current_object_id) REFERENCES objects(id)
            )
            """
        )

        user_columns = {
            row["name"]
            for row in cursor.execute("PRAGMA table_info(users)").fetchall()
        }
        if "language" not in user_columns:
            cursor.execute("ALTER TABLE users ADD COLUMN language TEXT DEFAULT NULL")

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                operator_id INTEGER NOT NULL,
                object_id INTEGER NOT NULL,
                issue_type TEXT NOT NULL,
                description TEXT,
                group_message_id INTEGER,
                status TEXT NOT NULL DEFAULT 'OPEN',
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                resolved_at TIMESTAMP,
                FOREIGN KEY (operator_id) REFERENCES users(telegram_id),
                FOREIGN KEY (object_id) REFERENCES objects(id)
            )
            """
        )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS user_objects (
                user_id INTEGER NOT NULL,
                object_id INTEGER NOT NULL,
                PRIMARY KEY (user_id, object_id),
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (object_id) REFERENCES objects(id) ON DELETE CASCADE
            )
            """
        )

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS admins (
                telegram_id INTEGER PRIMARY KEY,
                full_name TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                language TEXT DEFAULT NULL,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )

        admin_columns = {
            row["name"]
            for row in cursor.execute("PRAGMA table_info(admins)").fetchall()
        }
        if "language" not in admin_columns:
            cursor.execute("ALTER TABLE admins ADD COLUMN language TEXT DEFAULT NULL")

        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS object_topics (
                object_id INTEGER PRIMARY KEY,
                group_id INTEGER NOT NULL,
                thread_id INTEGER NOT NULL,
                FOREIGN KEY (object_id) REFERENCES objects(id) ON DELETE CASCADE
            )
            """
        )

        initial_objects = ("Tsum", "Chorvoq darvoza")
        cursor.executemany(
            "INSERT OR IGNORE INTO objects (name) VALUES (?)",
            ((object_name,) for object_name in initial_objects),
        )

        tsum = cursor.execute(
            "SELECT id FROM objects WHERE name = ?",
            ("Tsum",),
        ).fetchone()
        if tsum is None:
            raise RuntimeError("Tsum obyekti yaratilmadi")

        cursor.execute(
            """
            INSERT OR IGNORE INTO users
                (telegram_id, full_name, current_object_id, language, is_active)
            VALUES (?, ?, ?, NULL, 1)
            """,
            (6405446853, "Admin / Operator", tsum["id"]),
        )

        cursor.execute(
            """
            INSERT OR IGNORE INTO user_objects (user_id, object_id)
            SELECT u.id, o.id
            FROM users u
            CROSS JOIN objects o
            WHERE u.is_active = 1 AND o.is_active = 1
              AND NOT EXISTS (
                  SELECT 1
                  FROM user_objects assigned
                  WHERE assigned.user_id = u.id
              )
            """
        )

    print(f"Baza yangilandi: {DB_NAME}")


if __name__ == "__main__":
    init_db()
