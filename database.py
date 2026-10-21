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
    confirmed_count INTEGER NOT NULL DEFAULT 0 CHECK (confirmed_count >= 0),
    waiting_count INTEGER NOT NULL DEFAULT 0 CHECK (waiting_count >= 0),
    FOREIGN KEY (organizer_id) REFERENCES User (id)
);

CREATE TABLE IF NOT EXISTS Registration (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    student_id INTEGER NOT NULL,
    activity_id INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('confirmed', 'waiting', 'cancelled')),
    queue_order INTEGER NOT NULL DEFAULT 0,
    registered_at TEXT NOT NULL,
    request_token TEXT,
    UNIQUE (student_id, activity_id),
    FOREIGN KEY (student_id) REFERENCES User (id),
    FOREIGN KEY (activity_id) REFERENCES Activity (id)
);
"""

INDEXES = """
CREATE INDEX IF NOT EXISTS idx_registration_activity_status
    ON Registration (activity_id, status);
CREATE INDEX IF NOT EXISTS idx_registration_student
    ON Registration (student_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_registration_request_token
    ON Registration (request_token) WHERE request_token IS NOT NULL;
"""

TEST_TEACHER_USERNAME = "teacher"
TEST_TEACHER_PASSWORD = "teacher123"
TEST_ADMIN_USERNAME = "admin"
TEST_ADMIN_PASSWORD = "admin123"

# SQLite 在并发写入时默认只等待 5 秒（旧版本甚至立即报 database is locked），
# 峰值期多个请求同时抢写锁很容易踩到超时。显式放宽并把日志模式切到 WAL，
# 让读操作不再阻塞写操作。
BUSY_TIMEOUT_MS = 15000


@contextmanager
def get_connection(database_path: str | Path):
    """Open and safely close a SQLite connection."""
    connection = sqlite3.connect(database_path, timeout=BUSY_TIMEOUT_MS / 1000)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    connection.execute("PRAGMA synchronous = NORMAL")
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


def _enable_wal(connection) -> None:
    connection.execute("PRAGMA journal_mode = WAL")


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


def _upgrade_activity_counters(connection) -> None:
    """V3.0：为 Activity 增加已确认 / 候补冗余计数，并按 Registration 回填。"""
    columns = _table_columns(connection, "Activity")
    if "confirmed_count" not in columns:
        connection.execute(
            "ALTER TABLE Activity ADD COLUMN confirmed_count INTEGER NOT NULL DEFAULT 0"
        )
    if "waiting_count" not in columns:
        connection.execute(
            "ALTER TABLE Activity ADD COLUMN waiting_count INTEGER NOT NULL DEFAULT 0"
        )
    connection.execute(
        """
        UPDATE Activity SET
            confirmed_count = (
                SELECT COUNT(*) FROM Registration
                WHERE Registration.activity_id = Activity.id
                  AND Registration.status = 'confirmed'
            ),
            waiting_count = (
                SELECT COUNT(*) FROM Registration
                WHERE Registration.activity_id = Activity.id
                  AND Registration.status = 'waiting'
            )
        """
    )


def _upgrade_registration_token(connection) -> None:
    """V3.0：为 Registration 增加幂等提交令牌列。"""
    if "request_token" not in _table_columns(connection, "Registration"):
        connection.execute("ALTER TABLE Registration ADD COLUMN request_token TEXT")


def init_db(database_path: str | Path) -> None:
    """Create the database, migrate older schemas when needed, and seed test accounts."""
    database_path = Path(database_path)
    database_path.parent.mkdir(parents=True, exist_ok=True)

    with get_connection(database_path) as connection:
        _enable_wal(connection)
        connection.executescript(SCHEMA)

        if "queue_order" not in _table_columns(connection, "Registration"):
            _upgrade_registration_table(connection)

        if "account_status" not in _table_columns(connection, "User"):
            connection.execute(
                "ALTER TABLE User ADD COLUMN account_status TEXT NOT NULL DEFAULT 'normal'"
            )

        _upgrade_registration_token(connection)
        _upgrade_activity_counters(connection)
        connection.executescript(INDEXES)

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
