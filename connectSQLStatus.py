"""Microsoft SQL Server backend matching the Oracle FATP status-table rules."""

import threading
from datetime import datetime

import pyodbc


class SqlStatusDB:
    def __init__(self, string_connect: str, table_name: str):
        self.string_connect = string_connect
        self.table = table_name
        self.last_error = None
        self._lock = threading.RLock()

    def close(self):
        pass

    def test_connection(self):
        with pyodbc.connect(self.string_connect, timeout=5) as conn:
            row = conn.cursor().execute("SELECT 1").fetchone()
            return bool(row and row[0] == 1)

    @staticmethod
    def _norm(value):
        if value is None:
            return None
        value = str(value).strip()
        return value or None

    def upsert_status(self, cfg: dict, status: str, now_dt: datetime = None,
                      error_code: str = None, error_text: str = None) -> bool:
        self.last_error = None
        try:
            with self._lock, pyodbc.connect(self.string_connect, timeout=5) as conn:
                cur = conn.cursor()
                line = cfg.get("Line")
                loc = str(cfg.get("Location")) if cfg.get("Location") is not None else None
                name = cfg.get("Machine_name")
                mtype = cfg.get("Type_machine") or cfg.get("MACHINE_TYPE")
                cat = cfg.get("Category")
                factory = cfg.get("FACTORY")
                ts = now_dt or datetime.now()
                new_status = self._norm(status)
                new_code = self._norm(error_code)
                new_label = self._norm(error_text)

                row = cur.execute(
                    f"""SELECT TOP 1 STATUS, ERROR_TYPE, ERROR_CODE, START_TIME
                          FROM {self.table}
                         WHERE LINE=? AND LOCATION=? AND MACHINE_NAME=? AND MACHINE_TYPE=?
                           AND ((CATEGORY=?) OR (CATEGORY IS NULL AND ? IS NULL))
                           AND END_TIME IS NULL
                           AND ((FACTORY=?) OR (FACTORY IS NULL AND ? IS NULL))
                         ORDER BY START_TIME DESC""",
                    line, loc, name, mtype, cat, cat, factory, factory,
                ).fetchone()

                if row is not None:
                    old_status = self._norm(row[0])
                    old_code = self._norm(row[2])
                    should_write = old_status != new_status
                    if not should_write and new_status in ("ERROR", "CUT"):
                        should_write = old_code != new_code
                    if not should_write:
                        conn.rollback()
                        return False
                    cur.execute(
                        f"""UPDATE {self.table}
                               SET END_TIME=?, [TIME]=DATEDIFF(SECOND, START_TIME, ?)
                             WHERE LINE=? AND LOCATION=? AND MACHINE_NAME=? AND MACHINE_TYPE=?
                               AND ((CATEGORY=?) OR (CATEGORY IS NULL AND ? IS NULL))
                               AND END_TIME IS NULL
                               AND ((FACTORY=?) OR (FACTORY IS NULL AND ? IS NULL))""",
                        ts, ts, line, loc, name, mtype, cat, cat, factory, factory,
                    )

                cur.execute(
                    f"""INSERT INTO {self.table}
                        (LINE, LOCATION, MACHINE_TYPE, MACHINE_NAME, CATEGORY, FACTORY,
                         STATUS, START_TIME, ERROR_TYPE, ERROR_CODE)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    line, loc, mtype, name, cat, factory, new_status, ts, new_label, new_code,
                )
                conn.commit()
                return True
        except Exception as exc:
            self.last_error = str(exc)
            return False

    def upsert_connect(self, table_name: str, lines, factory_by_line: dict,
                       ts: datetime = None):
        ts = ts or datetime.now()
        with self._lock, pyodbc.connect(self.string_connect, timeout=5) as conn:
            cur = conn.cursor()
            for line in lines:
                factory = factory_by_line.get(line)
                row = cur.execute(
                    f"""SELECT TOP 1 ID FROM {table_name}
                         WHERE LINE=? AND ((FACTORY=?) OR (FACTORY IS NULL AND ? IS NULL))
                         ORDER BY ID DESC""", line, factory, factory
                ).fetchone()
                if row:
                    cur.execute(f"UPDATE {table_name} SET DATETIME=? WHERE ID=?", ts, row[0])
                else:
                    cur.execute(
                        f"INSERT INTO {table_name} (LINE, DATETIME, STATUS, FACTORY) VALUES (?, ?, ?, ?)",
                        line, ts, "OK", factory,
                    )
            conn.commit()
