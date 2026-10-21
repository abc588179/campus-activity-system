import sqlite3
from contextlib import contextmanager
from pathlib import Path

from werkzeug.security import generate_password_hash


SCHEMA = """
CREATE TABLE IF NOT EXISTS User (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('student', 'teacher', 'admin')),
    account_status TEXT NOT NULL DEFAULT 'normal' CHECK (account_status IN ('normal', 'disabled'))
);

CREATE TABLE IF NOT EXISTS Activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    organizer_id INTEGER NOT NULL,
    max_participants INTEGER NOT NULL CHECK (max_participants > 0),
    end_time TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('open', 'closed')),
    FOREIGN KEY (organizer_id) REFERENCES User (id)
);

CREATE TABLE IF NOT EXISTS Registration (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER NOT NULL,
    activity_id INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('confirmed', 'waiting', 'cancelled')),
    queue_order INTEGER NOT NULL DEFAULT 0,
    registered_at TEXT NOT NULL,
    UNIQUE (student_id, activity_id),
    FOREIGN KEY (student_id) REFERENCES User (id),
    FOREIGN KEY (activity_id) REFERENCES Activity (id)
);
"""

TEST_TEACHER_USERNAME = "teacher"
TEST_TEACHER_PASSWORD = "teacher123"
TEST_ADMIN_USERNAME = "admin"
TEST_ADMIN_PASSWORD = "admin123"


@contextmanager
def get_connection(database_path: str | Path):
    """Open and safely close a SQLite connection."""
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _table_columns(connection, table_name: str) -> set[str]:
    return {row["name"] for row in connection.execute(f"PRAGMA table_info({table_name})")}


def _upgrade_registration_table(connection) -> None:
    """把 V1.0 的 Registration(status: active/cancelled) 迁移为 V2.0 的三态模型。

    SQLite 不支持直接修改 CHECK 约束，因此采用「新建表 -> 搬运数据 -> 重命名」的方式。
    V1.0 的 active 在 V2.0 中语义等价于 confirmed。
    """
    connection.commit()
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute(
        """
        CREATE TABLE Registration_v2 (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            student_id INTEGER NOT NULL,
            activity_id INTEGER NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('confirmed', 'waiting', 'cancelled')),
            queue_order INTEGER NOT NULL DEFAULT 0,
            registered_at TEXT NOT NULL,
            UNIQUE (student_id, activity_id),
            FOREIGN KEY (student_id) REFERENCES User (id),
            FOREIGN KEY (activity_id) REFERENCES Activity (id)
        )
        """
    )
    connection.execute(
        """
        INSERT INTO Registration_v2
            (id, student_id, activity_id, status, queue_order, registered_at)
        SELECT
            id,
            student_id,
            activity_id,
            CASE WHEN status = 'active' THEN 'confirmed' ELSE status END,
            0,
            registered_at
        FROM Registration
        """
    )
    connection.execute("DROP TABLE Registration")
    connection.execute("ALTER TABLE Registration_v2 RENAME TO Registration")
    connection.commit()
    connection.execute("PRAGMA foreign_keys = ON")


def init_db(database_path: str | Path) -> None:
    """Create the database, migrate the V1.0 schema when needed, and seed test accounts."""
    database_path = Path(database_path)
    database_path.parent.mkdir(parents=True, exist_ok=True)

    with get_connection(database_path) as connection:
        connection.executescript(SCHEMA)

        if "queue_order" not in _table_columns(connection, "Registration"):
            _upgrade_registration_table(connection)

        if "account_status" not in _table_columns(connection, "User"):
            connection.execute(
                "ALTER TABLE User ADD COLUMN account_status TEXT NOT NULL DEFAULT 'normal'"
            )

        _seed_account(connection, TEST_TEACHER_USERNAME, TEST_TEACHER_PASSWORD, "teacher")
        _seed_account(connection, TEST_ADMIN_USERNAME, TEST_ADMIN_PASSWORD, "admin")


def _seed_account(connection, username: str, password: str, role: str) -> None:
    existing = connection.execute(
        "SELECT id FROM User WHERE username = ?",
        (username,),
    ).fetchone()
    if existing is None:
        connection.execute(
            "INSERT INTO User (username, password, role) VALUES (?, ?, ?)",
            (username, generate_password_hash(password), role),
        )


if __name__ == "__main__":
    init_db(Path(__file__).parent / "instance" / "campus_activity.db")
