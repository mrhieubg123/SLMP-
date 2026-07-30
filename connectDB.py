# connectDB.py
# Oracle upsert theo rule:
# - CHỈ ghi mới khi ĐỔI TRẠNG THÁI (RUN/STOP/OFF/ERROR/CUT...).
# - ERROR → ERROR (chỉ đổi ERROR_CODE/ERROR_TYPE/ERROR_TEXT) => KHÔNG ghi (no-op).
# - CUT: giữ NGUYÊN hành vi cũ (ghi khi code CUT đổi).
# Yêu cầu: pip install oracledb

import os
import oracledb
from datetime import datetime


def _init_oracle_client():
    """
    Bật thick mode nếu có Instant Client (tốt cho 11g).
    Đặt biến môi trường ORACLE_CLIENT_DIR = thư mục chứa oci.dll để ép thick mode.
    Nếu không có thì chạy thin mode bình thường.
    """
    lib_dir = os.environ.get("ORACLE_CLIENT_DIR")
    try:
        if lib_dir:
            oracledb.init_oracle_client(lib_dir=lib_dir)
        else:
            # Nếu không chỉ định lib_dir, lệnh dưới có thể raise -> sẽ fallback sang thin
            oracledb.init_oracle_client()
    except Exception:
        # Không sao, vẫn dùng thin mode
        pass


_init_oracle_client()


class connectDB:
    def __init__(self, user: str, password: str, dsn: str, table_name: str):
        self.user = user
        self.password = password
        self.dsn = dsn
        self.table = table_name
        self.last_error = None

        # Pool kết nối cho đa luồng
        self.pool = oracledb.SessionPool(
            user=self.user,
            password=self.password,
            dsn=self.dsn,
            min=1,
            max=8,
            increment=1,
            threaded=True,
            getmode=oracledb.SPOOL_ATTRVAL_WAIT,
        )

    def close(self):
        try:
            if self.pool is not None:
                self.pool.close()
        except Exception:
            pass

    # ───────── Helpers (nội bộ) ─────────
    @staticmethod
    def _norm(x):
        if x is None:
            return None
        s = str(x).strip()
        return None if s == "" else s

    def _fetch_open(self, cur, *, line, loc, name, mtype, cat, factory):
        if cat is None:
            sql = f"""
                SELECT STATUS, ERROR_TYPE, ERROR_CODE, START_TIME
                  FROM {self.table}
                 WHERE LINE=:line AND LOCATION=:loc AND MACHINE_NAME=:name
                   AND MACHINE_TYPE=:mtype AND CATEGORY IS NULL
                   AND END_TIME IS NULL AND FACTORY=:factory
                 ORDER BY START_TIME DESC
                 FETCH FIRST 1 ROWS ONLY
            """
            binds = {"line": line, "loc": loc, "name": name, "mtype": mtype, "factory": factory}
        else:
            sql = f"""
                SELECT STATUS, ERROR_TYPE, ERROR_CODE, START_TIME
                  FROM {self.table}
                 WHERE LINE=:line AND LOCATION=:loc AND MACHINE_NAME=:name
                   AND MACHINE_TYPE=:mtype AND CATEGORY=:cat
                   AND END_TIME IS NULL AND FACTORY=:factory
                 ORDER BY START_TIME DESC
                 FETCH FIRST 1 ROWS ONLY
            """
            binds = {"line": line, "loc": loc, "name": name, "mtype": mtype, "cat": cat, "factory": factory}
        cur.execute(sql, binds)
        return cur.fetchone()  # (status, error_type, error_code, start_time) hoặc None

    def _close_open(self, cur, *, line, loc, name, mtype, cat, ts: datetime, factory):
        if cat is None:
            sql = f"""
                UPDATE {self.table}
                   SET END_TIME = :end_t,
                       TIME     = ROUND((CAST(:end_t AS DATE) - CAST(START_TIME AS DATE)) * 86400)
                 WHERE LINE=:line AND LOCATION=:loc AND MACHINE_NAME=:name
                   AND MACHINE_TYPE=:mtype AND CATEGORY IS NULL
                   AND END_TIME IS NULL AND FACTORY=:factory
            """
            binds = {"end_t": ts, "line": line, "loc": loc, "name": name, "mtype": mtype, "factory": factory}
        else:
            sql = f"""
                UPDATE {self.table}
                   SET END_TIME = :end_t,
                       TIME     = ROUND((CAST(:end_t AS DATE) - CAST(START_TIME AS DATE)) * 86400)
                 WHERE LINE=:line AND LOCATION=:loc AND MACHINE_NAME=:name
                   AND MACHINE_TYPE=:mtype AND CATEGORY=:cat
                   AND END_TIME IS NULL AND FACTORY=:factory
            """
            binds = {"end_t": ts, "line": line, "loc": loc, "name": name, "mtype": mtype, "cat": cat, "factory": factory}
        cur.execute(sql, binds)

    def _insert(self, cur, *, line, loc, name, mtype, cat, factory, status, code, label, ts: datetime):
        sql = f"""
            INSERT INTO {self.table}
                (LINE, LOCATION, MACHINE_TYPE, MACHINE_NAME, CATEGORY, FACTORY,
                 STATUS, START_TIME, ERROR_TYPE, ERROR_CODE)
            VALUES (:line, :loc, :mtype, :name, :cat, :factory,
                    :status, :start_t, :err_text, :err_code)
        """
        cur.execute(
            sql,
            {
                "line": line,
                "loc": loc,
                "mtype": mtype,
                "name": name,
                "cat": cat,
                "factory": factory,
                "status": status,
                "start_t": ts,
                "err_text": label,
                "err_code": code,
            },
        )

    # ───────── Public helpers (dùng từ nơi khác) ─────────
    def fetch_all(self, sql: str, binds: dict | None = None):
        conn = None
        try:
            conn = self.pool.acquire()
            cur = conn.cursor()
            cur.execute(sql, binds or {})
            rows = cur.fetchall()
            return rows
        finally:
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass

    def fetch_one(self, sql: str, binds: dict | None = None):
        rows = self.fetch_all(sql, binds)
        return rows[0] if rows else None

    def execute(self, sql: str, binds: dict | None = None, many: bool = False):
        conn = None
        try:
            conn = self.pool.acquire()
            cur = conn.cursor()
            if many:
                cur.executemany(sql, binds or [])
            else:
                cur.execute(sql, binds or {})
            conn.commit()
            return True
        except Exception as e:
            self.last_error = str(e)
            try:
                if conn is not None:
                    conn.rollback()
            except Exception:
                pass
            return False
        finally:
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass

    # ───────── API chính (được gọi từ main1.py) ─────────
    def upsert_status(
        self,
        cfg: dict,
        status: str,
        now_dt: datetime | None = None,
        error_code: str | None = None,
        error_text: str | None = None,
    ) -> bool:
        self.last_error = None
        conn = None
        try:
            conn = self.pool.acquire()
            cur = conn.cursor()

            line = cfg.get("Line")
            loc = str(cfg.get("Location")) if cfg.get("Location") is not None else None
            name = cfg.get("Machine_name")
            mtype = cfg.get("Type_machine") or cfg.get("MACHINE_TYPE")
            cat = cfg.get("Category")  # có thể None
            factory = cfg.get("FACTORY")
            ts = now_dt or datetime.now()

            new_status = self._norm(status)
            new_code   = self._norm(error_code)
            new_label  = self._norm(error_text)

            # Lấy record đang mở (nếu có)
            row = self._fetch_open(
                cur,
                line=line, loc=loc, name=name, mtype=mtype, cat=cat, factory=factory
            )

            if row is None:
                # Chưa có record mở → chèn mới
                self._insert(
                    cur,
                    line=line, loc=loc, name=name, mtype=mtype, cat=cat, factory=factory,
                    status=new_status, code=new_code, label=new_label, ts=ts
                )
                conn.commit()
                return True

            cur_status, cur_label, cur_code, _cur_start = row
            cur_status = self._norm(cur_status)
            cur_label  = self._norm(cur_label)
            cur_code   = self._norm(cur_code)

            should_write = False

            if cur_status != new_status:
                # STATUS đổi → ghi
                should_write = True
            else:
                # STATUS không đổi
                if new_status == "ERROR":
                    # Khi đổi ERROR_CODE vẫn ghi trạng thái mới
                    if cur_code != new_code:
                        should_write = True
                    else:
                        should_write = False
                elif new_status == "CUT":
                    # GIỮ hành vi cũ: ghi khi code CUT đổi
                    if cur_code != new_code:
                        should_write = True
                else:
                    # RUN/STOP/OFF giữ nguyên → không ghi
                    should_write = False

            if not should_write:
                conn.rollback()  # không thay đổi
                return False

            # Ghi: đóng bản ghi cũ bằng cùng ts, rồi insert bản ghi mới với START_TIME = ts
            self._close_open(
                cur,
                line=line, loc=loc, name=name, mtype=mtype, cat=cat, ts=ts,  factory=factory
            )
            self._insert(
                cur,
                line=line, loc=loc, name=name, mtype=mtype, cat=cat, factory=factory,
                status=new_status, code=new_code, label=new_label, ts=ts
            )
            conn.commit()
            return True

        except Exception as e:
            self.last_error = str(e)
            try:
                if conn is not None:
                    conn.rollback()
            except Exception:
                pass
            return False
        finally:
            try:
                if conn is not None:
                    conn.close()  # trả về pool
            except Exception:
                pass