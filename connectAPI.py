# connectAPI.py
# Chương trình API chạy ĐỘC LẬP với Oracle.
#
# API v2 theo yêu cầu mới nhất nhưng gửi BODY dạng FORM như bản cũ:
# 1) postMachineStatus
#    - POST application/x-www-form-urlencoded mỗi 1 giây.
#    - Gửi MACHINE_NO + CURRENT_STATE.
#    - CURRENT_STATE: 1=RUN, 2=STOP, 3=ERROR, 4=OFF.
#
# 2) postMachineErrorRecord
#    - Khi vừa bắt được ERROR: POST FORM có MACHINE_NO, PROJECT_NAME, SECTION_NAME,
#      ERROR_CODE, START_TIME. Không gửi END_TIME.
#    - Khi ERROR kết thúc hoặc đổi lỗi: POST FORM có MACHINE_NO, PROJECT_NAME,
#      SECTION_NAME, ERROR_CODE, END_TIME. Không gửi START_TIME.
#
# 3) postMachineSummary
#    - Mỗi giờ gửi 1 record FORM cho từng máy.
#    - Tính số giây RUN/OFF/ERROR/STOP trong giờ đó.
#    - Trạng thái nào không phát sinh thì gửi 0.
#
# Cả 3 API đều dùng:
#    Method: POST
#    Body: application/x-www-form-urlencoded

import json
import os
import re
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any

import pymcprotocol
from plc_snapshot import PLCSnapshotHub


_ADDR_RE = re.compile(r"^((?:SM|M|L))(\d+)$")
_WORD_ADDR_RE = re.compile(r"^([A-Z]+)(\d+)$")

STATUS_RUN = 1
STATUS_STOP = 2
STATUS_ERROR = 3
STATUS_OFF = 4
STATUS_STANDBY = 5

DEFAULT_STATUS_URL = "https://fiisw-cns.myfiinet.com/ws-sync-data/api/postMachineStatus"
DEFAULT_RECORD_URL = "https://fiisw-cns.myfiinet.com/ws-sync-data/api/postMachineErrorRecord"
DEFAULT_SUMMARY_URL = "https://fiisw-cns.myfiinet.com/ws-sync-data/api/postMachineSummary"

M_STOP1 = "M56"
M_STOP2 = "M5056"
DEFAULT_D_BT_GROUPS = [
    ["D3011", "D3012", "D3013"],
    ["D3021", "D3022", "D3023"],
]

def now_hms() -> str:
    return time.strftime("%H:%M:%S")


def now_dt() -> datetime:
    return datetime.now()


def fmt_dt(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def floor_hour(dt: datetime) -> datetime:
    if dt.minute < 30:
        base = dt - timedelta(hours=1)
        return base.replace(minute=30, second=0, microsecond=0)
    else:
        return dt.replace(minute=30, second=0, microsecond=0)


def status_label(status: int) -> str:
    return {
        STATUS_RUN: "RUN",
        STATUS_STOP: "STOP",
        STATUS_ERROR: "ERROR",
        STATUS_OFF: "OFF",
        STATUS_STANDBY: "STANDBY",
    }.get(int(status), str(status))


def _norm_addr(addr: str) -> str:
    return str(addr or "").strip().upper()


def _norm_run_addrs(value) -> List[str]:
    """Chấp nhận M_RUN dạng "M55" hoặc ["M55", "M5055", ...]."""
    raw = value if isinstance(value, (list, tuple)) else [value]
    result: List[str] = []
    for item in raw:
        addr = _norm_addr(item)
        if addr and _ADDR_RE.fullmatch(addr) and addr not in result:
            result.append(addr)
    return result


def _norm_d_bt_groups(value) -> List[List[str]]:
    """Chuẩn hóa D_BT thành danh sách combo, mỗi combo đúng 3 word register."""
    raw_groups = value if isinstance(value, (list, tuple)) else []
    result: List[List[str]] = []
    for raw_group in raw_groups:
        if not isinstance(raw_group, (list, tuple)) or len(raw_group) != 3:
            continue
        group = [_norm_addr(addr) for addr in raw_group]
        if all(_WORD_ADDR_RE.fullmatch(addr) for addr in group):
            result.append(group)
    return result or [list(group) for group in DEFAULT_D_BT_GROUPS]


def parse_bit_addr(addr: str) -> Tuple[str, int]:
    m = _ADDR_RE.match(_norm_addr(addr))
    if not m:
        raise ValueError(f"Invalid bit address: {addr}")
    return m.group(1), int(m.group(2))


def parse_word_addr(addr: str) -> Tuple[str, int]:
    m = _WORD_ADDR_RE.match(_norm_addr(addr))
    if not m:
        raise ValueError(f"Invalid word address: {addr}")
    return m.group(1), int(m.group(2))


def _sort_addr(addr: str) -> int:
    m = _ADDR_RE.match(_norm_addr(addr))
    return int(m.group(2)) if m else 0


def group_contiguous(nums: List[int]) -> List[Tuple[int, int]]:
    if not nums:
        return []
    nums = sorted(set(nums))
    out: List[Tuple[int, int]] = []
    start = prev = nums[0]
    for n in nums[1:]:
        if n == prev + 1:
            prev = n
        else:
            out.append((start, prev - start + 1))
            start = prev = n
    out.append((start, prev - start + 1))
    return out


def _get(rt_raw: dict, key: str, default=None):
    if key in rt_raw:
        return rt_raw.get(key, default)
    cfg = rt_raw.get("config") or {}
    return cfg.get(key, default)


def _as_bool(v, default=True) -> bool:
    if v is None:
        return bool(default)
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() not in ("0", "false", "no", "off", "")


def load_runtime_api_config(runtime_path: str) -> dict:
    try:
        with open(runtime_path, "r", encoding="utf-8") as f:
            rt_raw = json.load(f)
    except Exception:
        rt_raw = {}

    # Requirement mới: postMachineStatus phải gửi mỗi 1 giây.
    # Không dùng giá trị cũ 15s nữa để tránh cấu hình cũ làm sai yêu cầu.
    current_state_interval = 1

    return {
        "api_enabled": _as_bool(_get(rt_raw, "api_enabled", True), True),
        "api_record_url": str(_get(rt_raw, "api_record_url", DEFAULT_RECORD_URL) or "").strip(),
        "api_current_state_url": str(_get(rt_raw, "api_current_state_url", DEFAULT_STATUS_URL) or "").strip(),
        "api_summary_url": str(_get(rt_raw, "api_summary_url", DEFAULT_SUMMARY_URL) or "").strip(),
        "api_timeout_seconds": float(_get(rt_raw, "api_timeout_seconds", 10)),
        "api_current_state_send_seconds": current_state_interval,
        "api_summary_send_seconds": int(_get(rt_raw, "api_summary_send_seconds", 3600) or 3600),
        "api_summary_check_seconds": int(_get(rt_raw, "api_summary_check_seconds", 5) or 5),
        "poll_interval_sec": float(_get(rt_raw, "poll_interval_sec", 0.5)),
        "debounce_polls": int(_get(rt_raw, "debounce_polls", 2)),
        "reconnect_minutes": int(_get(rt_raw, "reconnect_minutes", 2)),
        "WAIT_ON_HOLD_SECS": int(_get(rt_raw, "WAIT_ON_HOLD_SECS", 30)),
        "api_headers": _get(rt_raw, "api_headers", {}) or {},
        # False = bỏ qua kiểm tra SSL certificate khi gọi API https nội bộ/proxy.
        # Có thể bật lại bằng runtime_config.json: "api_verify_ssl": true
        "api_verify_ssl": _as_bool(_get(rt_raw, "api_verify_ssl", False), False),
    }


def load_api_machine_config(connect_path: str, api_path: str, oracle_path: str) -> List[dict]:
    with open(connect_path, "r", encoding="utf-8") as f:
        connect_raw = json.load(f)
    with open(api_path, "r", encoding="utf-8") as f:
        api_raw = json.load(f)
    with open(oracle_path, "r", encoding="utf-8") as f:
        oracle_raw = json.load(f)

    keys = sorted(set(connect_raw) | set(api_raw) | set(oracle_raw), key=lambda x: int(x) if str(x).isdigit() else str(x))
    machines: List[dict] = []
    for k in keys:
        c = connect_raw.get(k) or {}
        a = api_raw.get(k) or {}
        o = oracle_raw.get(k) or {}
        if not isinstance(c, dict) or not isinstance(a, dict) or not isinstance(o, dict):
            continue
        if str(o.get("STATUS", "True")).lower() != "true":
            continue
        if not c.get("IP") or not c.get("PORT"):
            continue
        m = {
            "KEY": k,
            "IP": c.get("IP"),
            "PORT": int(c.get("PORT")),
            "M_STOP": _norm_run_addrs(c.get("M_STOP")),
            "D_BT": _norm_d_bt_groups(c.get("D_BT")),
            # MACHINE_TYPE dùng để tìm nhóm lỗi trong config_group.json.
            # Ưu tiên connect.json, nếu file cũ chưa có thì fallback config_oracle.json.
            "MACHINE_TYPE": c.get("MACHINE_TYPE") or o.get("MACHINE_TYPE"),
            "Machine_name": o.get("Machine_name"),
            "Line": o.get("Line"),
            "Location": o.get("Location"),
            "MACHINE_NO": a.get("MACHINE_NO"),
            "PROJECT_NAME": a.get("PROJECT_NAME"),
            "SECTION_NAME": a.get("SECTION_NAME"),
        }
        if m["MACHINE_NO"]:
            machines.append(m)
    return machines


def load_error_catalog(path: str):
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    def parse_entry(code, val):
        code = str(code).strip().upper()
        if isinstance(val, dict) and "bit" in val:
            bit = _norm_addr(val.get("bit", code))
            label = str(val.get("error") or val.get("label") or "")
            if not _ADDR_RE.match(bit):
                return None
            return {"code": code, "bit": bit, "label": label}
        return None

    grouped: Dict[str, List[Dict[str, str]]] = {}
    wait_by_mt: Dict[str, List[Dict[str, str]]] = {}

    for group_name, group_val in (raw or {}).items():
        if group_name == "LINE_OVERRIDE":
            # API không dùng CUT/LINE_OVERRIDE, chỉ RUN/STOP/ERROR/OFF.
            continue
        if not isinstance(group_val, dict):
            continue

        entries: List[Dict[str, str]] = []
        for code, val in group_val.items():
            if code == "WAIT" and isinstance(val, dict):
                w_entries: List[Dict[str, str]] = []
                for w_code, w_val in val.items():
                    e = parse_entry(w_code, w_val)
                    if e:
                        w_entries.append(e)
                if w_entries:
                    wait_by_mt[group_name] = w_entries
                continue
            e = parse_entry(code, val)
            if e:
                entries.append(e)
        if entries:
            grouped[group_name] = entries
    return grouped, wait_by_mt


class ApiHttpClient:
    """
    Client gọi API theo yêu cầu mới, nhưng body gửi dạng form như bản cũ:
    - Method: POST
    - Body: application/x-www-form-urlencoded
    - Response: JSON có code SUCCESS/FAILED.
    """
    def __init__(self, record_url: str = "", current_state_url: str = "", summary_url: str = "",
                 headers: Optional[dict] = None, timeout_seconds: float = 10, log_put=None,
                 verify_ssl: bool = False):
        self.record_url = str(record_url or "").strip()
        self.current_state_url = str(current_state_url or "").strip()
        self.summary_url = str(summary_url or "").strip()
        self.headers = dict(headers or {})
        self.timeout_seconds = float(timeout_seconds or 10)
        self.log_put = log_put or (lambda msg: None)
        self.verify_ssl = bool(verify_ssl)
        # verify_ssl=False: bỏ qua lỗi SSL certificate cho API nội bộ/proxy HTTPS.
        self.ssl_context = None if self.verify_ssl else ssl._create_unverified_context()

    @staticmethod
    def _clean_payload(payload: dict) -> dict:
        """Chuẩn hóa payload trước khi encode form."""
        out = {}
        for k, v in (payload or {}).items():
            if v is None:
                out[str(k)] = ""
            else:
                out[str(k)] = v
        return out

    @staticmethod
    def _check_api_body(body: str) -> Tuple[bool, str]:
        body = body or ""
        try:
            data = json.loads(body)
        except Exception:
            # Nếu HTTP 2xx nhưng body không phải JSON thì vẫn coi là OK.
            return True, body

        code = str(data.get("code", "")).upper()
        msg = str(data.get("message", "") or "")
        result = data.get("result")
        if code == "SUCCESS":
            return True, msg or str(result or "success")
        if code == "FAILED":
            return False, msg or str(result or "API returned FAILED")
        return True, body

    def _log_request_error(self, url: str, request_body: str, error_detail: str,
                           response_body: str = ""):
        """Ghi dữ liệu request/response để truy vết một lần gọi API bị lỗi."""
        message = (
            f"[{now_hms()}] API REQUEST ERROR | URL={url or '<empty>'} "
            f"| ERROR={error_detail} | REQUEST_BODY={request_body}"
        )
        if response_body:
            message += f" | RESPONSE_BODY={response_body}"
        self.log_put(message)

    def _post_form(self, url: str, payload: dict) -> Tuple[bool, str]:
        form_payload = self._clean_payload(payload)
        # Gửi FORM như bản cũ: MACHINE_NO=...&CURRENT_STATE=1
        data = urllib.parse.urlencode(form_payload).encode("utf-8")
        request_body = data.decode("utf-8", errors="replace")

        if not url:
            error_detail = "ConfigurationError: API URL is empty"
            self._log_request_error(url, request_body, error_detail)
            return False, "API URL is empty"

        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        for k, v in self.headers.items():
            # Cho phép runtime_config.json ghi đè header khác nếu có,
            # nhưng Content-Type luôn giữ form-urlencoded theo yêu cầu hiện tại.
            if str(k).lower() == "content-type":
                continue
            headers[k] = v

        req = urllib.request.Request(url=url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds, context=self.ssl_context) as resp:
                body = resp.read().decode("utf-8", errors="replace")
                if 200 <= int(resp.status) < 300:
                    ok, info = self._check_api_body(body)
                    if not ok:
                        self._log_request_error(
                            url, request_body, f"API response rejected: {info}", body
                        )
                    return ok, info
                error_detail = f"HTTP {resp.status}"
                self._log_request_error(url, request_body, error_detail, body)
                return False, f"{error_detail}: {body}"
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode("utf-8", errors="replace")
            except Exception:
                body = ""
            error_detail = f"HTTPError {e.code}: {e.reason}"
            self._log_request_error(url, request_body, error_detail, body)
            return False, f"HTTP {e.code}: {body}"
        except Exception as e:
            self._log_request_error(
                url, request_body, f"{type(e).__name__}: {e!r}"
            )
            return False, str(e)

    def send_error_record(self, payload: dict) -> Tuple[bool, str]:
        return self._post_form(self.record_url, payload)

    def send_current_state(self, payload: dict) -> Tuple[bool, str]:
        return self._post_form(self.current_state_url, payload)

    def send_summary(self, payload: dict) -> Tuple[bool, str]:
        return self._post_form(self.summary_url, payload)


class ApiErrorRecordManager:
    """
    Quản lý record lỗi API trong RAM.

    Logic postMachineErrorRecord theo yêu cầu mới nhất:
    - Khi vừa bắt được lỗi ERROR: gửi ngay 1 payload có START_TIME, KHÔNG có END_TIME.
    - Khi lỗi kết thúc: gửi thêm 1 payload có END_TIME, KHÔNG có START_TIME.
    - Nếu đang ERROR nhưng đổi ERROR_CODE: kết thúc lỗi cũ rồi bắt đầu lỗi mới.
    """
    def __init__(self, client: ApiHttpClient, log_put=None):
        self.client = client
        self.log_put = log_put or (lambda msg: None)
        self.lock = threading.RLock()
        self.open_errors: Dict[str, dict] = {}
        self.last_ok = True

    @staticmethod
    def _key(m: dict) -> str:
        return str(m.get("KEY") or m.get("MACHINE_NO") or "")

    def _base_payload(self, open_error: dict) -> dict:
        m = open_error["machine"]
        return {
            "MACHINE_NO": m.get("MACHINE_NO"),
            "PROJECT_NAME": m.get("PROJECT_NAME"),
            "SECTION_NAME": m.get("SECTION_NAME"),
            "ERROR_CODE": open_error.get("error_code") or "",
        }

    def _send_error_start(self, open_error: dict) -> bool:
        """Gửi record bắt đầu lỗi: có START_TIME, không có END_TIME."""
        if not self.client.record_url:
            self.last_ok = False
            return False

        payload = self._base_payload(open_error)
        payload["START_TIME"] = fmt_dt(open_error.get("start_time"))

        ok, info = self.client.send_error_record(payload)
        self.last_ok = ok
        if ok:
            self.log_put(
                f"[{now_hms()}] API ERROR_START {payload.get('MACHINE_NO')} → "
                f"{payload.get('ERROR_CODE')} START={payload.get('START_TIME')}"
            )
        else:
            self.log_put(f"[{now_hms()}] API ERROR_START FAIL {payload.get('MACHINE_NO')}: {info}")
        return ok

    def _send_error_end(self, open_error: dict, end_time: datetime) -> bool:
        """Gửi record kết thúc lỗi: có END_TIME, không có START_TIME."""
        if not self.client.record_url:
            self.last_ok = False
            return False

        payload = self._base_payload(open_error)
        payload["END_TIME"] = fmt_dt(end_time)

        ok, info = self.client.send_error_record(payload)
        self.last_ok = ok
        if ok:
            self.log_put(
                f"[{now_hms()}] API ERROR_END {payload.get('MACHINE_NO')} → "
                f"{payload.get('ERROR_CODE')} END={payload.get('END_TIME')}"
            )
        else:
            self.log_put(f"[{now_hms()}] API ERROR_END FAIL {payload.get('MACHINE_NO')}: {info}")
        return ok

    def update_status(self, m: dict, status: int, error_code: Optional[str], ts: Optional[datetime] = None) -> bool:
        ts = ts or now_dt()
        key = self._key(m)
        if not key:
            return False

        status = int(status)
        error_code = (str(error_code).strip().upper() if error_code and status == STATUS_ERROR else None)

        with self.lock:
            cur = self.open_errors.get(key)

            if status == STATUS_ERROR:
                # Bắt đầu lỗi mới: lưu RAM và gửi START_TIME ngay.
                if cur is None:
                    open_error = {
                        "machine": m,
                        "error_code": error_code or "",
                        "start_time": ts,
                    }
                    self.open_errors[key] = open_error
                    return self._send_error_start(open_error)

                # Vẫn cùng lỗi thì không gửi thêm.
                if str(cur.get("error_code") or "") == str(error_code or ""):
                    return False

                # Đổi mã lỗi khi vẫn ERROR: gửi END_TIME cho lỗi cũ, rồi START_TIME cho lỗi mới.
                sent_end = self._send_error_end(cur, ts)
                open_error = {
                    "machine": m,
                    "error_code": error_code or "",
                    "start_time": ts,
                }
                self.open_errors[key] = open_error
                sent_start = self._send_error_start(open_error)
                return bool(sent_end or sent_start)

            # Không còn ERROR nữa: nếu đang có lỗi mở thì gửi END_TIME.
            if cur is not None:
                sent = self._send_error_end(cur, ts)
                self.open_errors.pop(key, None)
                return sent

            return False

    # Giữ tên cũ để không ảnh hưởng các chỗ gọi trong code.
    def upsert_status(self, m: dict, status: int, error_code: Optional[str], ts: Optional[datetime] = None) -> bool:
        return self.update_status(m, status, error_code, ts=ts)

    def force_close_all(self, end_time: Optional[datetime] = None):
        end_time = end_time or now_dt()
        with self.lock:
            items = list(self.open_errors.items())
        for key, open_error in items:
            try:
                self._send_error_end(open_error, end_time)
            finally:
                with self.lock:
                    self.open_errors.pop(key, None)


class ApiSummaryTracker:
    """
    Theo dõi thời gian từng trạng thái theo từng giờ.
    Khi qua giờ mới sẽ tạo payload postMachineSummary cho giờ vừa kết thúc.
    """
    def __init__(self, log_put=None):
        self.log_put = log_put or (lambda msg: None)
        self.lock = threading.RLock()
        self.rows: Dict[str, dict] = {}
        self.pending: List[dict] = []

    @staticmethod
    def _key(m: dict) -> str:
        return str(m.get("KEY") or m.get("MACHINE_NO") or "")

    @staticmethod
    def _new_totals() -> Dict[int, float]:
        return {
            STATUS_RUN: 0.0,
            STATUS_STOP: 0.0,
            STATUS_ERROR: 0.0,
            STATUS_OFF: 0.0,
            STATUS_STANDBY: 0.0,
        }

    @staticmethod
    def _add_seconds(totals: Dict[int, float], status: int, seconds: float):
        if seconds <= 0:
            return
        status = int(status)
        if status not in totals:
            totals[status] = 0.0
        totals[status] += float(seconds)

    def _make_summary_payload(self, row: dict, bucket_start: datetime, totals: Dict[int, float]) -> dict:
        m = row["machine"]
        return {
            "MACHINE_NO": m.get("MACHINE_NO"),
            "WORK_DATE": fmt_dt(bucket_start),
            "RUN_TIME": int(round(totals.get(STATUS_RUN, 0.0))),
            "OFF_TIME": int(round(totals.get(STATUS_OFF, 0.0))),
            "ERROR_TIME": int(round(totals.get(STATUS_ERROR, 0.0))),
            "STANDBY_TIME": int(round(totals.get(STATUS_STANDBY, 0.0))),
            "STOP_TIME": int(round(totals.get(STATUS_STOP, 0.0))) + int(round(totals.get(STATUS_OFF, 0.0))),
        }

    def _accrue_to_locked(self, row: dict, to_ts: datetime):
        last_ts: datetime = row["last_ts"]
        if to_ts <= last_ts:
            return

        while last_ts < to_ts:
            bucket_start = row["bucket_start"]
            bucket_end = bucket_start + timedelta(hours=1)
            end = min(to_ts, bucket_end)
            seconds = (end - last_ts).total_seconds()
            self._add_seconds(row["totals"], int(row["status"]), seconds)
            m = row["machine"]
            # self.log_put(
            #     f"[ACCRUE DEBUG] {m.get('MACHINE_NO')} | status={status_label(row['status'])} "
            #     f"added={seconds:.1f}s | totals={ {status_label(k): round(v, 1) for k, v in row['totals'].items()} }"
            # )
            last_ts = end
            row["last_ts"] = last_ts

            # Chốt giờ vừa xong.
            if last_ts >= bucket_end:
                payload = self._make_summary_payload(row, bucket_start, row["totals"])
                self.pending.append(payload)
                row["bucket_start"] = bucket_end
                row["totals"] = self._new_totals()

    def update_state(self, m: dict, status: int, ts: Optional[datetime] = None):
        ts = ts or now_dt()
        key = self._key(m)
        if not key:
            return
        status = int(status)
        with self.lock:
            row = self.rows.get(key)
            if row is None:
                self.rows[key] = {
                    "machine": m,
                    "status": status,
                    "last_ts": ts,
                    "bucket_start": floor_hour(ts),
                    "totals": self._new_totals(),
                }
                return
            self._accrue_to_locked(row, ts)
            row["status"] = status
            row["machine"] = m

    def tick(self, ts: Optional[datetime] = None) -> List[dict]:
        ts = ts or now_dt()
        with self.lock:
            for row in list(self.rows.values()):
                self._accrue_to_locked(row, ts)
            out = list(self.pending)
            self.pending.clear()
        return out

    def requeue(self, payload: dict):
        with self.lock:
            self.pending.insert(0, payload)


class PLCReader:
    def __init__(self, ip: str, port: int, timeout_sec: float = 10.0):
        self.ip = str(ip).strip()
        self.port = int(port)
        self.timeout_sec = float(timeout_sec)
        self.mc = None

    def connect(self):
        if self.mc is not None:
            return
        self.mc = pymcprotocol.Type3E()
        try:
            self.mc.soc_timeout = self.timeout_sec
            self.mc.connect(self.ip, self.port)
        except Exception:
            try:
                self.mc.close()
            except Exception:
                pass
            self.mc = None
            raise

    def close(self):
        try:
            if self.mc is not None:
                self.mc.close()
        except Exception:
            pass
        finally:
            self.mc = None

    def force_disconnect(self):
        self.close()

    def is_connected(self) -> bool:
        return self.mc is not None

    def batch_read_bits(self, addrs: List[str]) -> Dict[str, bool]:
        self.connect()
        per_dev = {"SM": [], "M": [], "L": []}
        norm: List[str] = []
        for a in addrs or []:
            a = _norm_addr(a)
            if not a:
                continue
            dev, n = parse_bit_addr(a)
            norm.append(a)
            per_dev[dev].append(n)
        res = {a: False for a in norm}
        for dev, nums in per_dev.items():
            if not nums:
                continue
            for start, length in group_contiguous(nums):
                vals = self.mc.batchread_bitunits(headdevice=f"{dev}{start}", readsize=length)
                for i, v in enumerate(vals):
                    addr = f"{dev}{start + i}"
                    if addr in res:
                        res[addr] = bool(v)
        return res

    def batch_read_words(self, addrs: List[str]) -> Dict[str, int]:
        self.connect()
        per_dev = {}
        norm: List[str] = []
        for a in addrs or []:
            a = _norm_addr(a)
            if not a:
                continue
            dev, n = parse_word_addr(a)
            norm.append(a)
            per_dev.setdefault(dev, []).append(n)
        res = {a: 0 for a in norm}
        for dev, nums in per_dev.items():
            if not nums:
                continue
            for start, length in group_contiguous(nums):
                vals = self.mc.batchread_wordunits(headdevice=f"{dev}{start}", readsize=length)
                for i, v in enumerate(vals):
                    addr = f"{dev}{start + i}"
                    if addr in res:
                        res[addr] = int(v)
        return res


class ApiMachineWorker(threading.Thread):
    def __init__(self, m: dict, err_entries: list, wait_entries: list,
                 poll: float, debounce: int, reconnect_minutes: int,
                 wait_hold_secs: int, record_mgr: ApiErrorRecordManager,
                 summary_tracker: ApiSummaryTracker,
                 current_state_registry: dict, registry_lock: threading.RLock,
                 log_put=None, plc_hub=None):
        super().__init__(daemon=True)
        self.m = m
        self.err_entries = list(err_entries or [])
        self.wait_entries = list(wait_entries or [])
        self.poll = float(poll or 0.5)
        self.debounce = max(1, int(debounce or 1))
        self.reconnect_minutes = max(1, int(reconnect_minutes or 1))
        self.wait_hold_secs = max(1, int(wait_hold_secs or 30))
        self.record_mgr = record_mgr
        self.summary_tracker = summary_tracker
        self.current_state_registry = current_state_registry
        self.registry_lock = registry_lock
        self.log_put = log_put or (lambda msg: None)
        self.stop_ev = threading.Event()
        self.plc = (plc_hub.client(m["IP"], int(m["PORT"])) if plc_hub
                    else PLCReader(m["IP"], int(m["PORT"]), timeout_sec=10.0))

        self.addr_stop = _norm_run_addrs(m.get("M_STOP"))
        self.d_bt_groups = _norm_d_bt_groups(m.get("D_BT"))
        self.addr_d_bt = list(dict.fromkeys(
            addr for group in self.d_bt_groups for addr in group
        ))
        self.addr_err = []
        seen = set()
        for e in self.err_entries:
            b = _norm_addr(e.get("bit"))
            if b and b not in seen:
                seen.add(b)
                self.addr_err.append(b)
        self.addr_wait = []
        seen = set()
        for e in self.wait_entries:
            b = _norm_addr(e.get("bit"))
            if b and b not in seen:
                seen.add(b)
                self.addr_wait.append(b)

        if hasattr(self.plc, "subscribe"):
            self.plc.subscribe(
                bits=self.addr_err + self.addr_wait + self.addr_stop,
                words=self.addr_d_bt,
            )

        self.last_decision: Optional[Tuple[int, Optional[str]]] = None
        self.streak = 0
        self.last_logged: Optional[Tuple[int, Optional[str]]] = None
        self.wait_state = {"code": None, "since": None}

    def stop(self):
        self.stop_ev.set()
        self.plc.close()

    def _tag(self) -> str:
        return str(self.m.get("MACHINE_NO") or self.m.get("Machine_name") or self.m.get("KEY") or "API")

    def _update_registry(self, status: int, error_code: Optional[str], ts: Optional[datetime] = None):
        ts = ts or now_dt()
        with self.registry_lock:
            self.current_state_registry[str(self.m.get("KEY"))] = {
                "machine": self.m,
                "current_state": int(status),
                "error_code": error_code,
                "updated_at": ts,
            }
        self.summary_tracker.update_state(self.m, int(status), ts=ts)

    def _log_once(self, status: int, error_code: Optional[str]):
        key = (int(status), error_code if int(status) == STATUS_ERROR else None)
        if key == self.last_logged:
            return
        label = status_label(status)
        self.log_put(f"[{now_hms()}] API {self._tag()}: {label}" + (f" → {error_code}" if error_code else ""))
        self.last_logged = key

    def _set_off(self, reason: str = ""):
        ts = now_dt()
        self._update_registry(STATUS_OFF, None, ts=ts)
        self.record_mgr.upsert_status(self.m, STATUS_OFF, None, ts=ts)
        msg = f"[{now_hms()}] API {self._tag()}: OFF"
        if reason:
            msg += f" ({reason})"
        self.log_put(msg)
        self.last_decision = (STATUS_OFF, None)
        self.streak = 0
        self.last_logged = (STATUS_OFF, None)

    def _update_wait_hold(self, vals: Dict[str, bool]) -> Optional[str]:
        picked = None
        for e in self.wait_entries:
            if vals.get(_norm_addr(e.get("bit"))):
                picked = e
                break
        t = time.time()
        if not picked:
            self.wait_state = {"code": None, "since": None}
            return None
        code = str(picked.get("code") or "").strip().upper()
        if self.wait_state.get("code") != code:
            self.wait_state = {"code": code, "since": t}
            return None
        if self.wait_state.get("since") is None:
            self.wait_state["since"] = t
            return None
        if t - float(self.wait_state["since"]) >= self.wait_hold_secs:
            return code
        return None

    def _read_decision(self) -> Tuple[int, Optional[str]]:
        addrs = self.addr_err + self.addr_wait + self.addr_stop
        if hasattr(self.plc, "read_snapshot"):
            vals, vals_d, _snapshot_ts = self.plc.read_snapshot(addrs, self.addr_d_bt)
        else:
            vals = self.plc.batch_read_bits(addrs) if addrs else {}
            vals_d = None

        for s in self.addr_stop:
            if vals.get(s, False):
                return STATUS_STOP, None

        for e in self.err_entries:
            if vals.get(_norm_addr(e.get("bit"))):
                return STATUS_ERROR, str(e.get("code") or "").strip().upper()

        return STATUS_RUN, None

    def run(self):
        while not self.stop_ev.is_set():
            try:
                if not self.plc.is_connected():
                    self.plc.connect()
                    self.log_put(f"[{now_hms()}] API {self._tag()}: CONNECTED {self.m['IP']}:{self.m['PORT']}")

                decision = self._read_decision()
                if decision == self.last_decision:
                    self.streak += 1
                else:
                    self.last_decision = decision
                    self.streak = 1

                if self.streak == self.debounce:
                    st, code = self.last_decision
                    ts = now_dt()
                    self._update_registry(st, code, ts=ts)
                    # postMachineErrorRecord:
                    # - ERROR start: gửi START_TIME
                    # - ERROR end: gửi END_TIME
                    self.record_mgr.upsert_status(self.m, st, code, ts=ts)
                    self._log_once(st, code)

                time.sleep(self.poll)

            except Exception as e:
                self.log_put(f"[{now_hms()}] API {self._tag()}: PLC/read error: {e}")
                try:
                    self._set_off("PLC disconnected/read failed")
                except Exception as off_err:
                    self.log_put(f"[{now_hms()}] API {self._tag()}: set OFF failed: {off_err}")
                try:
                    self.plc.force_disconnect()
                except Exception:
                    pass
                for _ in range(self.reconnect_minutes * 60):
                    if self.stop_ev.is_set():
                        return
                    time.sleep(1)


class ApiCurrentStateWorker(threading.Thread):
    def __init__(self, client: ApiHttpClient, interval_seconds: int, current_state_registry: dict,
                 registry_lock: threading.RLock, log_put=None):
        super().__init__(daemon=True)
        self.client = client
        # Requirement mới: postMachineStatus gửi 1s/lần.
        self.interval_seconds = 1
        self.current_state_registry = current_state_registry
        self.registry_lock = registry_lock
        self.log_put = log_put or (lambda msg: None)
        self.stop_ev = threading.Event()

    def stop(self):
        self.stop_ev.set()

    def send_once(self) -> Tuple[int, int]:
        with self.registry_lock:
            rows = list(self.current_state_registry.values())
        ok_count = 0
        fail_count = 0
        for row in rows:
            m = row["machine"]
            payload = {
                "MACHINE_NO": m.get("MACHINE_NO"),
                "CURRENT_STATE": int(row.get("current_state")),
            }
            ok, info = self.client.send_current_state(payload)
            if ok:
                ok_count += 1
            else:
                fail_count += 1
                self.log_put(f"[{now_hms()}] API MACHINE_STATUS FAIL {payload.get('MACHINE_NO')}: {info}")
        return ok_count, fail_count

    def run(self):
        if not self.client.current_state_url:
            self.log_put(f"[{now_hms()}] API MACHINE_STATUS disabled: api_current_state_url is empty")
            return
        self.log_put(f"[{now_hms()}] API MACHINE_STATUS enabled: every 1s")
        while not self.stop_ev.is_set():
            for _ in range(self.interval_seconds):
                if self.stop_ev.is_set():
                    return
                time.sleep(1)
            try:
                with self.registry_lock:
                    has_rows = bool(self.current_state_registry)
                if not has_rows:
                    continue
                ok_count, fail_count = self.send_once()
                if fail_count:
                    self.log_put(f"[{now_hms()}] API MACHINE_STATUS sent ok={ok_count}, fail={fail_count} v1")
                # Gửi mỗi 1s nên không log thành công liên tục để tránh tràn log.
            except Exception as e:
                self.log_put(f"[{now_hms()}] API MACHINE_STATUS error: {e}")


class ApiSummaryWorker(threading.Thread):
    def __init__(self, client: ApiHttpClient, tracker: ApiSummaryTracker,
                 check_seconds: int = 5, log_put=None):
        super().__init__(daemon=True)
        self.client = client
        self.tracker = tracker
        self.check_seconds = max(1, int(check_seconds or 5))
        self.log_put = log_put or (lambda msg: None)
        self.stop_ev = threading.Event()

    def stop(self):
        self.stop_ev.set()

    def _send_payload(self, payload: dict) -> bool:
        ok, info = self.client.send_summary(payload)
        if ok:
            self.log_put(
                f"[{now_hms()}] API SUMMARY {payload.get('MACHINE_NO')} "
                f"{payload.get('WORK_DATE')} RUN={payload.get('RUN_TIME')} "
                f"STANDBY={payload.get('STANDBY_TIME')} STOP={payload.get('STOP_TIME')} ERROR={payload.get('ERROR_TIME')} OFF={payload.get('OFF_TIME')}"
            )
            return True
        self.log_put(f"[{now_hms()}] API SUMMARY FAIL {payload.get('MACHINE_NO')}: {info}")
        return False

    def flush_once(self):
        payloads = self.tracker.tick(now_dt())
        for payload in payloads:
            if not self._send_payload(payload):
                self.tracker.requeue(payload)
                break

    def run(self):
        if not self.client.summary_url:
            self.log_put(f"[{now_hms()}] API SUMMARY disabled: api_summary_url is empty")
            return
        self.log_put(f"[{now_hms()}] API SUMMARY enabled: send finished hour data")
        while not self.stop_ev.is_set():
            for _ in range(self.check_seconds):
                if self.stop_ev.is_set():
                    return
                time.sleep(1)
            try:
                self.flush_once()
            except Exception as e:
                self.log_put(f"[{now_hms()}] API SUMMARY error: {e}")


class ApiSupervisor:
    def __init__(self, base_dir: str, log_put=None, plc_hub=None):
        self.base_dir = base_dir
        self.log_put = log_put or (lambda msg: None)
        self.threads: List[threading.Thread] = []
        self.current_state_registry: Dict[str, dict] = {}
        self.registry_lock = threading.RLock()
        self.client: Optional[ApiHttpClient] = None
        self.record_mgr: Optional[ApiErrorRecordManager] = None
        self.summary_tracker: Optional[ApiSummaryTracker] = None
        self.machines: List[dict] = []
        self.current_state_worker: Optional[ApiCurrentStateWorker] = None
        self.summary_worker: Optional[ApiSummaryWorker] = None
        self.running = False
        self.plc_hub = plc_hub or PLCSnapshotHub(log_put=self.log_put)
        self._owns_plc_hub = plc_hub is None

    def _path(self, name: str) -> str:
        return os.path.join(self.base_dir, name)

    def start(self, force_enabled: bool = False):
        if self.running:
            return
        cfg = load_runtime_api_config(self._path("runtime_config.json"))
        if not force_enabled and not cfg.get("api_enabled", True):
            self.log_put(f"[{now_hms()}] API disabled by runtime_config.json")
            return

        machines = load_api_machine_config(
            self._path("connect.json"),
            self._path("config_api.json"),
            self._path("config_oracle.json"),
        )
        self.machines = list(machines)
        grouped, wait_by_mt = load_error_catalog(self._path("config_group.json"))

        self.client = ApiHttpClient(
            record_url=cfg.get("api_record_url"),
            current_state_url=cfg.get("api_current_state_url"),
            summary_url=cfg.get("api_summary_url"),
            headers=cfg.get("api_headers"),
            timeout_seconds=cfg.get("api_timeout_seconds", 10),
            log_put=self.log_put,
            verify_ssl=cfg.get("api_verify_ssl", False),
        )
        self.record_mgr = ApiErrorRecordManager(self.client, log_put=self.log_put)
        self.summary_tracker = ApiSummaryTracker(log_put=self.log_put)

        if not self.client.record_url:
            self.log_put(f"[{now_hms()}] API ERROR_RECORD URL empty -> điền api_record_url trong runtime_config.json")
        if not self.client.current_state_url:
            self.log_put(f"[{now_hms()}] API MACHINE_STATUS URL empty -> điền api_current_state_url trong runtime_config.json")
        if not self.client.summary_url:
            self.log_put(f"[{now_hms()}] API SUMMARY URL empty -> điền api_summary_url trong runtime_config.json")

        self.threads = []
        for m in machines:
            mt = str(m.get("MACHINE_TYPE") or "").strip()
            t = ApiMachineWorker(
                m=m,
                err_entries=list(grouped.get(mt) or []),
                wait_entries=list(wait_by_mt.get(mt) or []),
                poll=cfg.get("poll_interval_sec", 0.5),
                debounce=cfg.get("debounce_polls", 2),
                reconnect_minutes=cfg.get("reconnect_minutes", 2),
                wait_hold_secs=cfg.get("WAIT_ON_HOLD_SECS", 30),
                record_mgr=self.record_mgr,
                summary_tracker=self.summary_tracker,
                current_state_registry=self.current_state_registry,
                registry_lock=self.registry_lock,
                log_put=self.log_put,
                plc_hub=self.plc_hub,
            )
            t.start()
            self.threads.append(t)

        status_worker = ApiCurrentStateWorker(
            client=self.client,
            interval_seconds=1,
            current_state_registry=self.current_state_registry,
            registry_lock=self.registry_lock,
            log_put=self.log_put,
        )
        self.current_state_worker = status_worker
        status_worker.start()
        self.threads.append(status_worker)

        summary_worker = ApiSummaryWorker(
            client=self.client,
            tracker=self.summary_tracker,
            check_seconds=cfg.get("api_summary_check_seconds", 5),
            log_put=self.log_put,
        )
        self.summary_worker = summary_worker
        summary_worker.start()
        self.threads.append(summary_worker)

        self.running = True
        self.log_put(f"[{now_hms()}] API started {len(machines)} machine worker(s).")

    def _force_all_off(self, reason: str):
        """Khi chương trình đóng: cập nhật CURRENT_STATE=4 và đóng lỗi đang mở nếu có."""
        ts = now_dt()
        for m in list(self.machines or []):
            try:
                key = str(m.get("KEY") or m.get("MACHINE_NO") or "")
                with self.registry_lock:
                    self.current_state_registry[key] = {
                        "machine": m,
                        "current_state": STATUS_OFF,
                        "error_code": None,
                        "updated_at": ts,
                    }
                if self.summary_tracker:
                    self.summary_tracker.update_state(m, STATUS_OFF, ts=ts)
                if self.record_mgr:
                    self.record_mgr.upsert_status(m, STATUS_OFF, None, ts=ts)
            except Exception as e:
                self.log_put(f"[{now_hms()}] API OFF FAIL {m.get('MACHINE_NO')}: {e}")
        self.log_put(f"[{now_hms()}] API all machines set OFF ({reason}).")

        # Gửi CURRENT_STATE=4 một lần ngay lúc tắt, vì sau khi chương trình tắt sẽ không còn chu kỳ 1s.
        try:
            if self.current_state_worker and self.client and self.client.current_state_url:
                ok_count, fail_count = self.current_state_worker.send_once()
                self.log_put(f"[{now_hms()}] API MACHINE_STATUS OFF sent ok={ok_count}, fail={fail_count}")
        except Exception as e:
            self.log_put(f"[{now_hms()}] API MACHINE_STATUS OFF send failed: {e}")

        # Nếu đúng thời điểm qua giờ, cố flush summary trước khi thoát.
        try:
            if self.summary_worker:
                self.summary_worker.flush_once()
        except Exception as e:
            self.log_put(f"[{now_hms()}] API SUMMARY final flush failed: {e}")

    def stop(self):
        # Dừng worker đọc PLC trước để tránh vừa ghi OFF vừa đọc trạng thái mới.
        for t in list(self.threads):
            try:
                if isinstance(t, ApiMachineWorker):
                    t.stop()
            except Exception:
                pass
        for t in list(self.threads):
            try:
                if isinstance(t, ApiMachineWorker):
                    t.join(timeout=5)
            except Exception:
                pass

        self._force_all_off("program stopped")

        # Sau khi gửi OFF xong mới dừng worker gửi API định kỳ.
        for t in list(self.threads):
            try:
                if hasattr(t, "stop"):
                    t.stop()
            except Exception:
                pass
        for t in list(self.threads):
            try:
                t.join(timeout=5)
            except Exception:
                pass
        self.threads = []
        self.running = False
        if self._owns_plc_hub:
            self.plc_hub.stop()
        self.log_put(f"[{now_hms()}] API stopped.")
