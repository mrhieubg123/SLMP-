"""Đọc trạng thái PLC như connectAPI và lưu trực tiếp vào Microsoft SQL Server."""

import json
import os
import threading
import time
from typing import Optional, Tuple

try:
    import pyodbc
except ImportError:
    pyodbc = None

from connectAPI import (
    ApiCurrentStateWorker,
    ApiErrorRecordManager,
    ApiMachineWorker,
    ApiSummaryTracker,
    ApiSummaryWorker,
    ApiSupervisor,
    load_api_machine_config,
    load_error_catalog,
    load_runtime_api_config,
    fmt_dt,
    now_dt,
    now_hms,
)
from plc_snapshot import PLCSnapshotHub


ERROR_TABLE = "CNT_MACHINE_ERROR_RECORD"
INFO_TABLE = "CNT_MACHINE_INFO"
SUMMARY_TABLE = "CNT_MACHINE_SUMMARY"


def load_sql_config(runtime_path: str) -> dict:
    """Đọc cấu hình SQL; hỗ trợ key ở root hoặc trong object config."""
    try:
        with open(runtime_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except Exception:
        raw = {}
    cfg = raw.get("config") if isinstance(raw.get("config"), dict) else {}
    string_connect = raw.get("string_connect", cfg.get("string_connect", ""))
    sql_enabled = raw.get("sql_enabled", cfg.get("sql_enabled", True))
    if isinstance(sql_enabled, str):
        sql_enabled = sql_enabled.strip().lower() not in ("0", "false", "no", "off", "")
    return {
        "string_connect": str(string_connect or "").strip(),
        "sql_enabled": bool(sql_enabled),
    }


class SqlServerClient:
    """Adapter có cùng interface với ApiHttpClient nhưng ghi bằng câu lệnh SQL."""

    # Các worker cũ dùng ba thuộc tính này để kiểm tra luồng có được bật hay không.
    record_url = ERROR_TABLE
    current_state_url = INFO_TABLE
    summary_url = SUMMARY_TABLE

    def __init__(self, string_connect: str, log_put=None, reconnect_attempts: int = 3,
                 reconnect_delay_seconds: float = 2.0):
        self.string_connect = str(string_connect or "").strip()
        self.log_put = log_put or (lambda msg: None)
        self.lock = threading.RLock()
        self.reconnect_attempts = max(1, int(reconnect_attempts))
        self.reconnect_delay_seconds = max(0.1, float(reconnect_delay_seconds))

    def _connect(self):
        if pyodbc is None:
            raise RuntimeError("Chưa cài pyodbc. Hãy cài bằng: pip install pyodbc")
        if not self.string_connect:
            raise RuntimeError("string_connect trong runtime_config.json đang rỗng")
        return pyodbc.connect(self.string_connect, autocommit=False, timeout=10)

    def test_connection(self):
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1")
            cursor.fetchone()

    def _execute(self, operation: str, payload: dict, action) -> Tuple[bool, str]:
        last_exc = None
        for attempt in range(1, self.reconnect_attempts + 1):
            try:
                with self.lock:
                    with self._connect() as conn:
                        cursor = conn.cursor()
                        action(cursor)
                        conn.commit()
                if attempt > 1:
                    self.log_put(f"[{now_hms()}] SQL RECONNECTED")
                return True, "success"
            except Exception as exc:
                last_exc = exc
                if attempt < self.reconnect_attempts:
                    self.log_put(
                        f"[{now_hms()}] SQL connection lost; reconnect "
                        f"{attempt}/{self.reconnect_attempts - 1} in "
                        f"{self.reconnect_delay_seconds:g}s: {exc}"
                    )
                    time.sleep(self.reconnect_delay_seconds)

        exc = last_exc or RuntimeError("SQL connection failed")
        try:
            detail = f"{type(exc).__name__}: {exc}"
            try:
                body = json.dumps(payload, ensure_ascii=False, default=str)
            except Exception:
                body = repr(payload)
            self.log_put(
                f"[{now_hms()}] SQL {operation} ERROR | ERROR={detail} | DATA={body}"
            )
            return False, detail
        except Exception:
            return False, str(exc)

    def send_error_record(self, payload: dict) -> Tuple[bool, str]:
        def action(cursor):
            if payload.get("START_TIME"):
                cursor.execute(
                    f"""
                    INSERT INTO {ERROR_TABLE}
                        (MACHINE_NO, ERROR_CODE, START_TIME)
                    VALUES (?, ?, ?)
                    """,
                    payload.get("MACHINE_NO"),
                    payload.get("ERROR_CODE"),
                    payload.get("START_TIME"),
                )
                return

            cursor.execute(
                f"""
                UPDATE {ERROR_TABLE}
                   SET END_TIME = ?
                 WHERE MACHINE_NO = ?
                   AND END_TIME IS NULL
                """,
                payload.get("END_TIME"),
                payload.get("MACHINE_NO"),
            )
            if cursor.rowcount == 0:
                raise RuntimeError(
                    "Không tìm thấy bản ghi lỗi đang mở để cập nhật END_TIME"
                )

        return self._execute("ERROR_RECORD", payload, action)

    def send_current_state(self, payload: dict) -> Tuple[bool, str]:
        def action(cursor):
            current_state = payload.get("CURRENT_STATE")
            machine_no = payload.get("MACHINE_NO")
            cursor.execute(
                f"""
                UPDATE {INFO_TABLE}
                   SET CURRENT_STATE = ?
                 WHERE MACHINE_NO = ?
                """,
                current_state,
                machine_no,
            )
            if cursor.rowcount == 0:
                cursor.execute(
                    f"""
                    INSERT INTO {INFO_TABLE} (MACHINE_NO, CURRENT_STATE)
                    VALUES (?, ?)
                    """,
                    machine_no,
                    current_state,
                )

            # Khi máy không còn trạng thái ERROR (3), đóng mọi error record
            # đang mở bằng thời gian hiện tại lấy từ chương trình Python.
            if int(current_state) != 3:
                end_time = fmt_dt(now_dt())
                cursor.execute(
                    f"""
                    UPDATE {ERROR_TABLE}
                       SET END_TIME = ?
                     WHERE MACHINE_NO = ?
                       AND END_TIME IS NULL
                    """,
                    end_time,
                    machine_no,
                )

        return self._execute("MACHINE_INFO", payload, action)

    def send_summary(self, payload: dict) -> Tuple[bool, str]:
        def action(cursor):
            values = (
                payload.get("RUN_TIME"),
                payload.get("OFF_TIME"),
                payload.get("ERROR_TIME"),
                payload.get("STANDBY_TIME"),
                payload.get("STOP_TIME"),
                payload.get("MACHINE_NO"),
                payload.get("WORK_DATE"),
            )
            cursor.execute(
                    f"""
                    INSERT INTO {SUMMARY_TABLE}
                        (MACHINE_NO, WORK_DATE, RUN_TIME, OFF_TIME,
                         ERROR_TIME, STANDBY_TIME, STOP_TIME)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    payload.get("MACHINE_NO"),
                    payload.get("WORK_DATE"),
                    payload.get("RUN_TIME"),
                    payload.get("OFF_TIME"),
                    payload.get("ERROR_TIME"),
                    payload.get("STANDBY_TIME"),
                    payload.get("STOP_TIME"),
                )

        return self._execute("MACHINE_SUMMARY", payload, action)


class SqlErrorRecordManager(ApiErrorRecordManager):
    pass


class SqlCurrentStateWorker(ApiCurrentStateWorker):
    pass


class SqlSummaryWorker(ApiSummaryWorker):
    pass


class SqlMachineWorker(ApiMachineWorker):
    pass


class SqlSupervisor(ApiSupervisor):
    """Supervisor SQL dùng lại toàn bộ logic đọc PLC và tính summary của API."""

    def __init__(self, base_dir: str, log_put=None, plc_hub=None):
        output = log_put or (lambda msg: None)

        def sql_log(message):
            # Các worker dùng lại từ connectAPI; đổi nhãn để log SQL không bị hiểu nhầm.
            output(str(message).replace(" API ", " SQL ", 1))

        super().__init__(base_dir, log_put=sql_log, plc_hub=plc_hub)
        self.client: Optional[SqlServerClient] = None

    def start(self):
        if self.running:
            return

        runtime_path = self._path("runtime_config.json")
        sql_cfg = load_sql_config(runtime_path)
        if not sql_cfg["sql_enabled"]:
            self.log_put(f"[{now_hms()}] SQL disabled by runtime_config.json")
            return

        common_cfg = load_runtime_api_config(runtime_path)
        self.client = SqlServerClient(
            sql_cfg["string_connect"], log_put=self.log_put
        )
        try:
            self.client.test_connection()
        except Exception as exc:
            # Vẫn khởi động worker: mỗi thao tác SQL sẽ tự mở kết nối mới và retry.
            self.log_put(
                f"[{now_hms()}] SQL CONNECT FAIL: {exc}; "
                "workers started and will reconnect automatically"
            )

        machines = load_api_machine_config(
            self._path("connect.json"),
            self._path("config_api.json"),
            self._path("config_oracle.json"),
        )
        self.machines = list(machines)
        grouped, wait_by_mt = load_error_catalog(self._path("config_group.json"))
        self.record_mgr = SqlErrorRecordManager(self.client, log_put=self.log_put)
        self.summary_tracker = ApiSummaryTracker(log_put=self.log_put)

        self.threads = []
        for machine in machines:
            machine_type = str(machine.get("MACHINE_TYPE") or "").strip()
            worker = SqlMachineWorker(
                m=machine,
                err_entries=list(grouped.get(machine_type) or []),
                wait_entries=list(wait_by_mt.get(machine_type) or []),
                poll=common_cfg.get("poll_interval_sec", 0.5),
                debounce=common_cfg.get("debounce_polls", 2),
                reconnect_minutes=common_cfg.get("reconnect_minutes", 2),
                wait_hold_secs=common_cfg.get("WAIT_ON_HOLD_SECS", 30),
                record_mgr=self.record_mgr,
                summary_tracker=self.summary_tracker,
                current_state_registry=self.current_state_registry,
                registry_lock=self.registry_lock,
                log_put=self.log_put,
                plc_hub=self.plc_hub,
            )
            worker.start()
            self.threads.append(worker)

        self.current_state_worker = SqlCurrentStateWorker(
            client=self.client,
            interval_seconds=1,
            current_state_registry=self.current_state_registry,
            registry_lock=self.registry_lock,
            log_put=self.log_put,
        )
        self.current_state_worker.start()
        self.threads.append(self.current_state_worker)

        self.summary_worker = SqlSummaryWorker(
            client=self.client,
            tracker=self.summary_tracker,
            check_seconds=common_cfg.get("api_summary_check_seconds", 5),
            log_put=self.log_put,
        )
        self.summary_worker.start()
        self.threads.append(self.summary_worker)

        self.running = True
        self.log_put(f"[{now_hms()}] SQL started {len(machines)} machine worker(s).")


# Tên tương thích nếu nơi gọi đang import Supervisor theo convention khác.
ConnectSQLSupervisor = SqlSupervisor
