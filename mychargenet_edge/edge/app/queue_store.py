import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4


class QueueStore:
    def __init__(self, state_dir: str, retention_days: int = 30) -> None:
        path = Path(state_dir)
        path.mkdir(parents=True, exist_ok=True)
        self.database_path = path / "queue.sqlite3"
        self.retention_days = retention_days
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS queue_events (
                    event_id TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    created_at TEXT NOT NULL
                )
                """
            )

    def enqueue(self, event_type: str, payload: dict | None = None) -> str:
        event_id = str(uuid4())
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO queue_events(event_id,event_type,occurred_at,payload_json,created_at) VALUES(?,?,?,?,?)",
                (event_id, event_type, now, json.dumps(payload or {}, separators=(",", ":")), now),
            )
        return event_id

    def batch(self, limit: int = 50) -> list[dict]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT event_id,event_type,occurred_at,payload_json FROM queue_events ORDER BY created_at,event_id LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "event_id": row["event_id"],
                "event_type": row["event_type"],
                "occurred_at": row["occurred_at"],
                "payload": json.loads(row["payload_json"]),
            }
            for row in rows
        ]

    def acknowledge(self, event_ids: list[str]) -> None:
        if not event_ids:
            return
        placeholders = ",".join("?" for _ in event_ids)
        with self._connect() as connection:
            connection.execute(
                f"DELETE FROM queue_events WHERE event_id IN ({placeholders})",
                event_ids,
            )

    def count(self) -> int:
        with self._connect() as connection:
            return int(connection.execute("SELECT count(*) FROM queue_events").fetchone()[0])

    def purge_expired(self) -> int:
        cutoff = (datetime.now(UTC) - timedelta(days=self.retention_days)).isoformat()
        with self._connect() as connection:
            cursor = connection.execute("DELETE FROM queue_events WHERE created_at < ?", (cutoff,))
            return int(cursor.rowcount)
