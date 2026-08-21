from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ClaimRecord:
    id: int
    qq_user_id: str
    sub2api_user_id: int
    amount: float
    idempotency_key: str
    status: str


@dataclass(frozen=True)
class ClaimReservation:
    record: ClaimRecord
    can_attempt: bool


class ClaimStore:
    """Durable, local claim state for the group gift feature.

    This database belongs to the AstrBot plugin and deliberately does not add
    tables or fields to Sub2API. Unique constraints protect the two business
    identities even if multiple messages arrive at the same time.
    """

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=30,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 30000")
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS claims (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    qq_user_id TEXT NOT NULL UNIQUE,
                    sub2api_user_id INTEGER NOT NULL UNIQUE,
                    amount REAL NOT NULL CHECK (amount > 0),
                    idempotency_key TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL CHECK (
                        status IN ('pending', 'credited', 'failed')
                    ),
                    created_at TEXT NOT NULL,
                    completed_at TEXT,
                    error_code TEXT,
                    error_message TEXT
                )
                """
            )

    @staticmethod
    def _record(row: sqlite3.Row) -> ClaimRecord:
        return ClaimRecord(
            id=int(row["id"]),
            qq_user_id=str(row["qq_user_id"]),
            sub2api_user_id=int(row["sub2api_user_id"]),
            amount=float(row["amount"]),
            idempotency_key=str(row["idempotency_key"]),
            status=str(row["status"]),
        )

    def reserve(
        self,
        *,
        qq_user_id: str,
        sub2api_user_id: int,
        amount: float,
        idempotency_key: str,
        created_at: str,
    ) -> ClaimReservation:
        """Reserve a claim or return an existing claim for either identity."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            by_qq = connection.execute(
                "SELECT * FROM claims WHERE qq_user_id = ?",
                (qq_user_id,),
            ).fetchone()
            if by_qq is not None:
                record = self._record(by_qq)
                if record.sub2api_user_id != sub2api_user_id:
                    return ClaimReservation(record, False)
                if record.status == "failed":
                    connection.execute(
                        """
                        UPDATE claims
                        SET status = 'pending', error_code = NULL, error_message = NULL
                        WHERE id = ?
                        """,
                        (record.id,),
                    )
                    refreshed = connection.execute(
                        "SELECT * FROM claims WHERE id = ?", (record.id,)
                    ).fetchone()
                    assert refreshed is not None
                    return ClaimReservation(self._record(refreshed), True)
                return ClaimReservation(record, record.status == "pending")

            by_account = connection.execute(
                "SELECT * FROM claims WHERE sub2api_user_id = ?",
                (sub2api_user_id,),
            ).fetchone()
            if by_account is not None:
                return ClaimReservation(self._record(by_account), False)

            connection.execute(
                """
                INSERT INTO claims (
                    qq_user_id, sub2api_user_id, amount, idempotency_key,
                    status, created_at
                ) VALUES (?, ?, ?, ?, 'pending', ?)
                """,
                (
                    qq_user_id,
                    sub2api_user_id,
                    amount,
                    idempotency_key,
                    created_at,
                ),
            )
            row = connection.execute(
                "SELECT * FROM claims WHERE id = last_insert_rowid()"
            ).fetchone()
            assert row is not None
            return ClaimReservation(self._record(row), True)

    def mark_credited(self, claim_id: int, completed_at: str) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE claims
                SET status = 'credited', completed_at = ?, error_code = NULL,
                    error_message = NULL
                WHERE id = ?
                """,
                (completed_at, claim_id),
            )

    def mark_failed(
        self,
        claim_id: int,
        *,
        error_code: str,
        error_message: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE claims
                SET status = 'failed', error_code = ?, error_message = ?
                WHERE id = ?
                """,
                (error_code[:80], error_message[:500], claim_id),
            )
