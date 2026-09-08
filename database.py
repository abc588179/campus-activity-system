import sqlite3
from pathlib import Path


SCHEMA = """
CREATE TABLE IF NOT EXISTS User (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('student', 'teacher'))
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
    status TEXT NOT NULL CHECK (status IN ('active', 'cancelled')),
    registered_at TEXT NOT NULL,
    UNIQUE (student_id, activity_id),
    FOREIGN KEY (student_id) REFERENCES User (id),
    FOREIGN KEY (activity_id) REFERENCES Activity (id)
);
"""


def get_connection(database_path: str | Path) -> sqlite3.Connection:
    """Open a SQLite connection with foreign-key checks enabled."""
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init_db(database_path: str | Path) -> None:
    """Create the V1.0 database and its three core tables if needed."""
    database_path = Path(database_path)
    database_path.parent.mkdir(parents=True, exist_ok=True)

    with get_connection(database_path) as connection:
        connection.executescript(SCHEMA)


if __name__ == "__main__":
    init_db(Path(__file__).parent / "instance" / "campus_activity.db")
