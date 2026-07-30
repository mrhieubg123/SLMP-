# main_V1.0.py
# XB10 - giữ logic cũ Table1/Table2
# Sửa Table3 theo logic mới:
# - dùng shared persistent session theo IP:PORT, không tách thêm PLC connection
# - poll theo runtime_config.json: table3_poll_seconds
# - PASS_NOW / FAIL_NOW = giá trị hiện tại đọc từ PLC
# - PASS = PASS_NOW - PASS_NOW_TRUOC
# - FAIL = FAIL_NOW - FAIL_NOW_TRUOC
# - CYCLE_TIME = (PASS + FAIL) / table3_poll_seconds
# - nếu PASS_NOW, FAIL_NOW trùng bản ghi mới nhất DB -> không ghi
# - bỏ reset theo giờ / break time / dead time cho Table3
# - chỉ reset tay ngoài PLC nếu cần

import os, sys, json, time, threading, queue, re, importlib
from typing import Dict, List, Tuple, Optional, Any
from datetime import datetime, timedelta

import pymcprotocol
from PyQt5 import QtCore, QtWidgets
from Gui_main import Ui_MainWindow
from plc_snapshot import PLCSnapshotHub

try:
    from connectDB import connectDB as OracleCoreDB
except ModuleNotFoundError:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    OracleCoreDB = importlib.import_module("connectDB").connectDB

try:
    from connectAPI import ApiSupervisor
except ModuleNotFoundError:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    ApiSupervisor = importlib.import_module("connectAPI").ApiSupervisor

try:
    from connectSQL import SqlSupervisor
except ModuleNotFoundError:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    SqlSupervisor = importlib.import_module("connectSQL").SqlSupervisor

# === cấu hình đọc PLC ===
USE_SMLP_ONE_SHOT = False  # ép dùng shared persistent session theo IP:PORT
EXPECTED_LICENSE = "PTH-LOCK-HAI"

_ADDR_RE = re.compile(r"^((?:SM|M|L))(\d+)$")
_WORD_ADDR_RE = re.compile(r"^([A-Z]+)(\d+)$")
DEFAULT_D_BT_GROUPS = [
    ["D3011", "D3012", "D3013"],
    ["D3021", "D3022", "D3023"],
]


def app_dir() -> str:
    return os.path.dirname(sys.executable) if getattr(sys, "frozen", False) \
           else os.path.dirname(os.path.abspath(__file__))


def res_path(name: str) -> str:
    return os.path.join(app_dir(), name)


def now_hms() -> str:
    return time.strftime("%H:%M:%S")


def normalize_run_addrs(value) -> List[str]:
    """Chuẩn hóa M_RUN dạng chuỗi cũ hoặc danh sách bit mới."""
    raw = value if isinstance(value, (list, tuple)) else [value]
    result: List[str] = []
    for item in raw:
        addr = str(item or "").strip().upper()
        if addr and _ADDR_RE.fullmatch(addr) and addr not in result:
            result.append(addr)
    return result


def normalize_d_bt_groups(value) -> List[List[str]]:
    """Mỗi phần tử D_BT phải là một combo gồm đúng 3 thanh ghi word."""
    raw_groups = value if isinstance(value, (list, tuple)) else []
    result: List[List[str]] = []
    for raw_group in raw_groups:
        if not isinstance(raw_group, (list, tuple)) or len(raw_group) != 3:
            continue
        group = [str(addr or "").strip().upper() for addr in raw_group]
        if all(_WORD_ADDR_RE.fullmatch(addr) for addr in group):
            result.append(group)
    return result or [list(group) for group in DEFAULT_D_BT_GROUPS]


def parse_bit_addr(addr: str) -> Tuple[str, int]:
    m = _ADDR_RE.match(str(addr).strip().upper())
    if not m:
        raise ValueError(f"Invalid bit address: {addr}")
    return m.group(1), int(m.group(2))


def parse_word_addr(addr: str) -> Tuple[str, int]:
    m = _WORD_ADDR_RE.match(str(addr).strip().upper())
    if not m:
        raise ValueError(f"Invalid word address: {addr}")
    return m.group(1), int(m.group(2))


def parse_word_pair(spec: str) -> List[str]:
    parts = [str(x).strip().upper() for x in str(spec or '').split(',') if str(x).strip()]
    if len(parts) != 2:
        raise ValueError(f"Invalid word pair: {spec}")
    for a in parts:
        parse_word_addr(a)
    return parts


def group_contiguous(nums: List[int]) -> List[Tuple[int, int]]:
    if not nums:
        return []
    nums = sorted(set(nums))
    res: List[Tuple[int, int]] = []
    s = p = nums[0]
    for n in nums[1:]:
        if n == p + 1:
            p = n
        else:
            res.append((s, p - s + 1))
            s = p = n
    res.append((s, p - s + 1))
    return res


# ───────── TIMESTOP helpers (giữ nguyên cho Table1) ─────────
def _parse_time_any(s: str) -> Tuple[int, int]:
    s = str(s).strip()
    low = s.lower()
    ampm = None
    if low.endswith("am") or low.endswith("pm"):
        ampm = low[-2:]
        s = s[:-2].strip()
    parts = s.split(":")
    if len(parts) < 2:
        raise ValueError(f"Invalid time '{s}'")
    h, m = int(parts[0]), int(parts[1])
    if ampm == "am":
        if h == 12:
            h = 0
    elif ampm == "pm":
        if h != 12:
            h += 12
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"Bad time {s}")
    return h, m


def _window_hit(now_min: int, start_min: int, end_min: int) -> bool:
    if start_min == end_min:
        return False
    return (start_min <= now_min < end_min) if start_min < end_min \
           else (now_min >= start_min or now_min < end_min)


def load_timestop_slots(rt_raw: dict) -> List[Dict[str, str]]:
    ts = (rt_raw.get("timestop")
          or (rt_raw.get("config") or {}).get("timestop")
          or {})
    slots: List[Dict[str, str]] = []
    if not isinstance(ts, dict):
        return slots
    for _, slot in sorted(ts.items(), key=lambda kv: kv[0]):
        if not isinstance(slot, dict):
            continue
        if "start" not in slot or "end" not in slot:
            continue
        code, label = None, ""
        for k2, v2 in slot.items():
            k2u = str(k2).upper()
            if k2u.startswith("L") and k2u[1:].isdigit():
                code, label = k2u, str(v2)
                break
        if not code:
            continue
        sh, sm = _parse_time_any(slot["start"])
        eh, em = _parse_time_any(slot["end"])
        start, end = sh * 60 + sm, eh * 60 + em
        dur = (end - start) % (24 * 60)
        if dur == 0 or dur > 120:
            continue
        slots.append({"start": start, "end": end, "code": code, "label": label})
    return slots


# ───────── Config ─────────
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


def _read_json_file(path: str, label: str):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        raise RuntimeError(f"Không đọc được {label}: {e}")


def _merge_split_machine_config() -> List[dict]:
    """
    Bản mới tách config_5.json thành 3 file:
      - connect.json       : IP, PORT, M_RUN, PASS, FAIL nếu cần Table3
      - config_oracle.json : thông tin ghi Oracle theo logic cũ
      - config_api.json    : MACHINE_NO, PROJECT_NAME, SECTION_NAME cho API mới

    Logic Oracle vẫn dùng cùng dict máy như cũ, chỉ khác nguồn đọc dữ liệu.
    """
    connect_raw = _read_json_file(res_path("connect.json"), "connect.json")
    oracle_raw = _read_json_file(res_path("config_oracle.json"), "config_oracle.json")
    api_raw = _read_json_file(res_path("config_api.json"), "config_api.json")

    keys = sorted(set(connect_raw) | set(oracle_raw) | set(api_raw), key=lambda x: int(x) if str(x).isdigit() else str(x))
    machines: List[dict] = []
    for k in keys:
        c = connect_raw.get(k) or {}
        o = oracle_raw.get(k) or {}
        a = api_raw.get(k) or {}
        if not isinstance(c, dict) or not isinstance(o, dict):
            continue
        if not c.get("IP") or not c.get("PORT"):
            continue
        machines.append({
            "KEY": k,
            "STATUS": o.get("STATUS", "True"),
            "Machine_name": o.get("Machine_name"),
            "Line": o.get("Line"),
            "Location": o.get("Location"),
            "Category": o.get("Category"),
            "FACTORY": o.get("FACTORY"),
            "IP": c.get("IP"),
            "PORT": int(c.get("PORT")),
            "MACHINE_TYPE": c.get("MACHINE_TYPE") or o.get("MACHINE_TYPE"),
            "M_RUN": normalize_run_addrs(c.get("M_RUN") or o.get("M_RUN")),
            "D_BT": normalize_d_bt_groups(c.get("D_BT")),
            "PASS": (c.get("PASS") or o.get("PASS") or "").strip().upper(),
            "FAIL": (c.get("FAIL") or o.get("FAIL") or "").strip().upper(),
            # Phần API chạy độc lập, để kèm ở đây cho tiện quản lý nhưng Oracle không dùng.
            "MACHINE_NO": a.get("MACHINE_NO"),
            "PROJECT_NAME": a.get("PROJECT_NAME"),
            "SECTION_NAME": a.get("SECTION_NAME"),
        })
    return machines


def _load_legacy_config5(config5_path: str) -> List[dict]:
    raw5 = _read_json_file(config5_path, config5_path)
    machines: List[dict] = []
    for k, v in raw5.items():
        if k.isdigit() and isinstance(v, dict):
            machines.append({
                "KEY": k,
                "STATUS": v.get("STATUS", "True"),
                "Machine_name": v.get("Machine_name"),
                "Line": v.get("Line"),
                "Location": v.get("Location"),
                "Category": v.get("Category"),
                "FACTORY": v.get("FACTORY"),
                "IP": v.get("IP"),
                "PORT": int(v.get("PORT")),
                "MACHINE_TYPE": v.get("MACHINE_TYPE"),
                "M_RUN": normalize_run_addrs(v.get("M_RUN")),
                "D_BT": normalize_d_bt_groups(v.get("D_BT")),
                "PASS": (v.get("PASS") or "").strip().upper(),
                "FAIL": (v.get("FAIL") or "").strip().upper(),
                "MACHINE_NO": v.get("MACHINE_NO"),
                "PROJECT_NAME": v.get("PROJECT_NAME"),
                "SECTION_NAME": v.get("SECTION_NAME"),
            })
    return machines


def load_machine_config(config5_path: str = None):
    rt_path = res_path("runtime_config.json")
    try:
        with open(rt_path, "r", encoding="utf-8") as f:
            rt_raw = json.load(f)
    except Exception as e:
        raise RuntimeError(f"Không đọc được runtime_config.json: {e}")

    globals_cfg = {
        "oracle_enabled": _as_bool(_get(rt_raw, "oracle_enabled", True), True),
        "api_enabled": _as_bool(_get(rt_raw, "api_enabled", True), True),
        "sql_enabled": _as_bool(_get(rt_raw, "sql_enabled", True), True),
        "oracle_user": _get(rt_raw, "oracle_user"),
        "oracle_password": _get(rt_raw, "oracle_password"),
        "oracle_dsn": _get(rt_raw, "oracle_dsn"),

        "table_name1": (_get(rt_raw, "table_name1") or _get(rt_raw, "table_name") or "FATP_MACHINE_DATA"),
        "table_name2": _get(rt_raw, "table_name2", "FATP_MACHINE_DATA_CONNECT"),
        "poll_seconds": int(_get(rt_raw, "poll_seconds", _get(rt_raw, "poll_seconds1", 600))),
        "poll_interval_sec": float(_get(rt_raw, "poll_interval_sec", 0.5)),
        "debounce_polls": int(_get(rt_raw, "debounce_polls", 2)),
        "reconnect_minutes": int(_get(rt_raw, "reconnect_minutes", 5)),
        "cut_clear_debounce": int(_get(rt_raw, "cut_clear_debounce", 4)),
        "license_key": _get(rt_raw, "license_key"),
        "timestop_slots": load_timestop_slots(rt_raw),
        "wait_hold_secs": int(_get(rt_raw, "WAIT_ON_HOLD_SECS", 30)),

        # Table3
        "table_name3": _get(rt_raw, "table_name3", "FATP_MACHINE_FPY_DATA"),
        "table3_poll_seconds": int(_get(rt_raw, "table3_poll_seconds", 120)),
        "manual_reset_password": str(_get(rt_raw, "manual_reset_password", "")),

        # API mới: gửi CURRENT_STATE theo chu kỳ, để connectAPI.py tự đọc thêm nếu cần.
        "api_current_state_send_seconds": int(_get(rt_raw, "api_current_state_send_seconds", 15)),
    }

    # Ưu tiên config mới đã tách file. Nếu không có thì fallback config_5.json cũ.
    if os.path.exists(res_path("connect.json")) and os.path.exists(res_path("config_oracle.json")):
        machines = _merge_split_machine_config()
    else:
        if config5_path is None:
            config5_path = res_path("config_5.json")
        machines = _load_legacy_config5(config5_path)
    return globals_cfg, machines


def get_unique_lines_from_machine_config() -> List[str]:
    try:
        _, machines = load_machine_config()
    except Exception:
        return []
    lines = set()
    for m in machines:
        line = m.get("Line")
        if line:
            lines.add(str(line).strip())
    return sorted(lines)


# Giữ tên hàm cũ để không ảnh hưởng logic cũ ở các đoạn khác.
def get_unique_lines_from_config5(config5_path: str = None) -> List[str]:
    return get_unique_lines_from_machine_config()


def load_error_catalog_grouped(path: str):
    with open(path, "r", encoding="utf-8") as f:
        raw = json.load(f)

    line_bits: Dict[str, str] = {}
    for k, v in (raw.get("LINE_OVERRIDE") or {}).items():
        kk = str(k).strip().upper()
        if re.match(r"^(?:SM|M|L)\d+$", kk):
            line_bits[kk] = v if isinstance(v, str) else str(v)

    def parse_entry(code, val):
        code = str(code).strip().upper()
        if isinstance(val, dict) and "bit" in val:
            bit = str(val.get("bit", code)).strip().upper()
            label = str(val.get("error") or val.get("label") or "")
            if not re.match(r"^(?:SM|M|L)\d+$", bit):
                return None
            return {"code": code, "bit": bit, "label": label}
        return None

    grouped: Dict[str, List[Dict[str, str]]] = {}
    wait_by_mt: Dict[str, List[Dict[str, str]]] = {}

    for group_name, group_val in raw.items():
        if group_name == "LINE_OVERRIDE":
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
    return line_bits, grouped, wait_by_mt


# ───────── Shared PLC client (giữ nguyên) ─────────
class SharedPLCConnection:
    def __init__(self, ip: str, port: int, timeout_sec: float = 10.0):
        self.ip = ip
        self.port = int(port)
        self.timeout_sec = float(timeout_sec)
        self.mc = None
        self.lock = threading.RLock()
        self.ref_count = 0
        self.last_error = None

    def acquire(self):
        with self.lock:
            self.ref_count += 1
            return self

    def release(self):
        with self.lock:
            self.ref_count = max(0, self.ref_count - 1)
            if self.ref_count == 0:
                self._close_locked()

    def _connect_locked(self):
        if self.mc is not None:
            return
        self.mc = pymcprotocol.Type3E()
        try:
            self.mc.soc_timeout = self.timeout_sec
            self.mc.connect(self.ip, self.port)
            self.last_error = None
        except Exception as e:
            self.last_error = str(e)
            try:
                self.mc.close()
            except Exception:
                pass
            self.mc = None
            raise

    def _close_locked(self):
        try:
            if self.mc is not None:
                self.mc.close()
        except Exception:
            pass
        finally:
            self.mc = None

    def connect(self):
        with self.lock:
            self._connect_locked()

    def close(self):
        with self.lock:
            self._close_locked()

    def is_connected(self) -> bool:
        with self.lock:
            return self.mc is not None

    def _read_bits_locked(self, addrs: List[str]) -> Dict[str, bool]:
        per_dev = {"SM": [], "M": [], "L": []}
        norm: List[str] = []
        for a in addrs:
            if not a:
                continue
            a = a.upper().strip()
            norm.append(a)
            d, n = parse_bit_addr(a)
            per_dev[d].append(n)
        res = {a: False for a in norm}
        for dev, nums in per_dev.items():
            if not nums:
                continue
            for s, ln in group_contiguous(nums):
                vals = self.mc.batchread_bitunits(headdevice=f"{dev}{s}", readsize=ln)
                for i, v in enumerate(vals):
                    addr = f"{dev}{s+i}"
                    if addr in res:
                        res[addr] = bool(v)
        return res

    def _read_words_locked(self, addrs: List[str]) -> Dict[str, int]:
        groups: Dict[str, List[int]] = {}
        norm: List[str] = []
        for a in addrs:
            if not a:
                continue
            a = a.upper().strip()
            norm.append(a)
            d, n = parse_word_addr(a)
            groups.setdefault(d, []).append(n)
        res = {a: 0 for a in norm}
        for dev, nums in groups.items():
            if not nums:
                continue
            for s, ln in group_contiguous(nums):
                vals = self.mc.batchread_wordunits(headdevice=f"{dev}{s}", readsize=ln)
                for i, v in enumerate(vals):
                    addr = f"{dev}{s+i}"
                    if addr in res:
                        res[addr] = int(v)
        return res

    def _write_words_locked(self, word_map: Dict[str, int]):
        groups: Dict[str, Dict[int, int]] = {}
        for a, v in (word_map or {}).items():
            if not a:
                continue
            addr = a.upper().strip()
            d, n = parse_word_addr(addr)
            groups.setdefault(d, {})[n] = int(v)
        for dev, mp in groups.items():
            nums = sorted(mp)
            for s, ln in group_contiguous(nums):
                payload = [int(mp[s + i]) for i in range(ln)]
                self.mc.batchwrite_wordunits(headdevice=f"{dev}{s}", values=payload)

    def _call_locked(self, op_name: str, func, *args, **kwargs):
        last_err = None
        for attempt in range(2):
            try:
                self._connect_locked()
                return func(*args, **kwargs)
            except Exception as e:
                last_err = e
                self.last_error = str(e)
                self._close_locked()
                if attempt == 0:
                    continue
                raise
        raise last_err

    def batch_read_bits(self, addrs: List[str]) -> Dict[str, bool]:
        with self.lock:
            return self._call_locked('read_bits', self._read_bits_locked, addrs)

    def batch_read_words(self, addrs: List[str]) -> Dict[str, int]:
        with self.lock:
            return self._call_locked('read_words', self._read_words_locked, addrs)

    def batch_write_words(self, word_map: Dict[str, int]):
        with self.lock:
            return self._call_locked('write_words', self._write_words_locked, word_map)


class SharedPLCRegistry:
    def __init__(self):
        self.lock = threading.RLock()
        self._items: Dict[Tuple[str, int], SharedPLCConnection] = {}

    def acquire(self, ip: str, port: int, timeout_sec: float = 10.0) -> SharedPLCConnection:
        key = (str(ip).strip(), int(port))
        with self.lock:
            conn = self._items.get(key)
            if conn is None:
                conn = SharedPLCConnection(key[0], key[1], timeout_sec=timeout_sec)
                self._items[key] = conn
            return conn.acquire()

    def release(self, ip: str, port: int):
        key = (str(ip).strip(), int(port))
        with self.lock:
            conn = self._items.get(key)
        if conn is None:
            return
        conn.release()
        with self.lock:
            if conn.ref_count == 0 and self._items.get(key) is conn:
                self._items.pop(key, None)


_SHARED_PLC_REGISTRY = SharedPLCRegistry()


class PLCClient:
    def __init__(self, ip: str, port: int, timeout_sec: float = 10.0, style: str = 'persistent'):
        self.ip = str(ip).strip()
        self.port = int(port)
        self.timeout_sec = float(timeout_sec)
        self.style = 'shared-persistent'
        self.endpoint = _SHARED_PLC_REGISTRY.acquire(self.ip, self.port, timeout_sec=self.timeout_sec)
        self._released = False

    def connect(self):
        self.endpoint.connect()

    def close(self):
        if self._released:
            return
        _SHARED_PLC_REGISTRY.release(self.ip, self.port)
        self._released = True

    def force_disconnect(self):
        self.endpoint.close()

    def is_connected(self):
        return self.endpoint.is_connected()

    def batch_read_bits(self, addrs: List[str]) -> Dict[str, bool]:
        return self.endpoint.batch_read_bits(addrs)

    def batch_read_words(self, addrs: List[str]) -> Dict[str, int]:
        return self.endpoint.batch_read_words(addrs)

    def batch_write_words(self, word_map: Dict[str, int]):
        return self.endpoint.batch_write_words(word_map)


# ───────── Oracle DB adapter (giữ nguyên) ─────────
class OracleDBAdapter:
    def __init__(self, user: str, password: str, dsn: str, table_name: str, log_put):
        self.core = OracleCoreDB(user=user, password=password, dsn=dsn, table_name=table_name)
        self.log_put = log_put
        self._clk_cache = {"t": 0.0, "now": None}

    def test_connection(self) -> Tuple[bool, Optional[str]]:
        """
        Kiểm tra Oracle thật sự query được hay không.
        SessionPool có thể tạo được nhưng query lỗi do network/service/table/quyền,
        nên cần test rõ để GUI log ra ORACLE CONNECTED / ORACLE CONNECT FAIL.
        """
        try:
            rows = self.core.fetch_all("SELECT 1 FROM dual")
            if rows and rows[0] and rows[0][0] == 1:
                self.core.last_error = None
                return True, None
            return False, "SELECT 1 FROM dual không trả về dữ liệu"
        except Exception as e:
            self.core.last_error = str(e)
            return False, str(e)

    def now_clock(self) -> datetime:
        mono = time.monotonic()
        if self._clk_cache["now"] is not None and mono - self._clk_cache["t"] < 0.9:
            return self._clk_cache["now"]
        try:
            rows = self.core.fetch_all("SELECT SYSDATE FROM dual")
            if rows and rows[0] and rows[0][0]:
                self._clk_cache["now"] = rows[0][0]
                self._clk_cache["t"] = mono
                return self._clk_cache["now"]
        except Exception:
            pass
        self._clk_cache["now"] = datetime.now()
        self._clk_cache["t"] = mono
        return self._clk_cache["now"]

    def upsert_status(self, line: str, loc: str, mtype: str, name: str,
                      status: str, code: Optional[str], label: Optional[str],
                      ts: Optional[datetime] = None, category: Optional[str] = None,
                      factory: Optional[str] = None):
        cfg = {"Line": line, "Location": loc, "Machine_name": name,
               "Type_machine": mtype, "Category": category, "FACTORY": factory}
        ok = self.core.upsert_status(cfg=cfg, status=status,
                                     now_dt=ts or self.now_clock(),
                                     error_code=code, error_text=label)
        mw = getattr(self, 'main_window', None)
        if ok:
            if mw:
                mw.set_table1_status('ok')
            self.log_put(f"[{now_hms()}] ↑DB {line}/{loc}/{category}/{name} → {status}"
                        + (f" ({code} - {label})" if code else ""))
        elif self.core.last_error:
            if mw:
                mw.set_table1_status('false')
            self.log_put(f"[{now_hms()}] ↑DB FAIL {line}/{loc}/{category}/{name}: {self.core.last_error}")
        else:
            if mw:
                mw.set_table1_status('-')


# ───────── CUT registry (giữ nguyên) ─────────
class LineOverrideRegistry:
    def __init__(self):
        self.data: Dict[str, Dict[str, object]] = {}
        self.lock = threading.Lock()

    def set(self, line: str, code: str, name: str):
        with self.lock:
            self.data[line] = {"code": code, "name": name, "ts": time.time()}

    def get(self, line: str) -> Optional[Dict[str, object]]:
        with self.lock:
            return self.data.get(line)

    def clear(self, line: str):
        with self.lock:
            self.data.pop(line, None)


LINE_OVR = LineOverrideRegistry()
LINE_OVR_EPOCH: Dict[str, float] = {}
LINE_OVR_GUARD_SEC: float = 0.0


def _sort_addr(a: str) -> int:
    m = _ADDR_RE.match(a)
    return int(m.group(2)) if m else 0


# ───────── MachineWorker (giữ nguyên logic không liên quan) ─────────
class MachineWorker(threading.Thread):
    def __init__(self, db: OracleDBAdapter, m: dict,
                 line_bits: Dict[str, str], err_entries: list,
                 poll: float, debounce: int, reconn_min: int,
                 timestop_slots: List[Dict[str, str]],
                 cut_clear_debounce: int,
                 log_put,
                 wait_entries: Optional[List[Dict[str, str]]] = None,
                 wait_hold_secs: int = 30, plc_hub=None):
        super().__init__(daemon=True)
        self.db, self.m = db, m
        self.poll, self.debounce, self.reconn = poll, debounce, reconn_min
        self.timestop_slots = list(timestop_slots or [])
        self.log_put = log_put

        self.line_bits = {k.upper(): v for k, v in (line_bits or {}).items()}
        self.addr_line: List[str] = sorted(self.line_bits.keys(), key=_sort_addr) if self.line_bits else []

        self.err_entries = list(err_entries or [])
        uniq: List[str] = []
        seen = set()
        for e in self.err_entries:
            b = e["bit"].upper()
            if b not in seen:
                seen.add(b)
                uniq.append(b)
        self.addr_err: List[str] = uniq

        self.wait_entries = list(wait_entries or [])
        wuniq: List[str] = []
        wseen = set()
        for e in self.wait_entries:
            b = e["bit"].upper()
            if b not in wseen:
                wseen.add(b)
                wuniq.append(b)
        self.addr_wait: List[str] = wuniq
        self.wait_hold_secs = max(1, int(wait_hold_secs))
        self._wait_state = {"code": None, "label": None, "since": None}

        self.addr_run: List[str] = normalize_run_addrs(m.get("M_RUN"))
        self.d_bt_groups: List[List[str]] = normalize_d_bt_groups(m.get("D_BT"))
        self.addr_d_bt: List[str] = list(dict.fromkeys(
            addr for group in self.d_bt_groups for addr in group
        ))

        self.plc = (plc_hub.client(m["IP"], int(m["PORT"])) if plc_hub else
                    PLCClient(m["IP"], int(m["PORT"]), timeout_sec=10.0,
                              style=("smlp" if USE_SMLP_ONE_SHOT else "persistent")))
        if hasattr(self.plc, "subscribe"):
            self.plc.subscribe(
                bits=self.addr_line + self.addr_err + self.addr_wait + self.addr_run,
                words=self.addr_d_bt,
            )
        self.stop_ev = threading.Event()
        self.last_decision: Optional[Tuple[str, Optional[str], Optional[str]]] = None
        self.streak = 0
        self.cut_clear_debounce = max(2, int(cut_clear_debounce))
        self.cut_off_streak = 0
        self.last_logged: Optional[Tuple[str, Optional[str]]] = None

    def _timestop_hit_now(self) -> Optional[Tuple[str, str]]:
        if not self.timestop_slots:
            return None
        now = self.db.now_clock()
        now_min = now.hour * 60 + now.minute
        for s in self.timestop_slots:
            if _window_hit(now_min, s["start"], s["end"]):
                return s["code"], s["label"]
        return None

    def _log_once(self, name: str, status: str, code: Optional[str], label: Optional[str], reason: Optional[str] = None):
        """
        Log trạng thái PLC cho tab ORACLE.
        Ghi log này độc lập với kết quả ghi DB, để tab ORACLE và tab API nhìn cùng kiểu trạng thái:
          RUN / STOP / ERROR / OFF / CUT.
        """
        key = (status, code, reason)
        if key != self.last_logged:
            msg = f"{status}" if not code else f"{status} → {code} - {label}"
            if reason:
                msg += f" ({reason})"
            self.log_put(f"[{now_hms()}] ORACLE {name}: {msg}")
            self.last_logged = key

    def _update_wait_hold(self, vals: Dict[str, bool]) -> Optional[Tuple[str, str]]:
        picked = None
        for e in self.wait_entries:
            if vals.get(e["bit"].upper()):
                picked = e
                break

        now = time.time()
        if not picked:
            self._wait_state = {"code": None, "label": None, "since": None}
            return None

        code, label = picked["code"], picked["label"]
        st = self._wait_state
        if st["code"] != code:
            self._wait_state = {"code": code, "label": label, "since": now}
            return None

        if st["since"] is None:
            self._wait_state["since"] = now
            return None

        if (now - self._wait_state["since"]) >= self.wait_hold_secs:
            return (code, label)
        return None

    def run(self):
        name = self.m["Machine_name"]
        line = self.m["Line"]
        loc = str(self.m["Location"])
        mtype = self.m["MACHINE_TYPE"]
        category = self.m.get("Category")
        factory = self.m.get("FACTORY")

        while not self.stop_ev.is_set():
            try:
                ts = self._timestop_hit_now()
                if ts:
                    dec = ("CUT", ts[0], ts[1])
                    if dec == self.last_decision:
                        self.streak += 1
                    else:
                        self.last_decision = dec
                        self.streak = self.debounce
                    if self.streak == self.debounce:
                        ts_now = self.db.now_clock()
                        self._log_once(name, dec[0], dec[1], dec[2])
                        self.db.upsert_status(line, loc, mtype, name, *dec, ts=ts_now, category=category, factory=factory)
                    time.sleep(self.poll)
                    continue

                # o = LINE_OVR.get(line)
                # if o:
                #     decision = ("CUT", o["code"], o["name"])
                #     if decision == self.last_decision:
                #         self.streak += 1
                #     else:
                #         self.last_decision = decision
                #         self.streak = self.debounce
                #     if self.streak == self.debounce:
                #         ts2 = self.db.now_clock()
                #         self._log_once(name, decision[0], decision[1], decision[2])
                #         self.db.upsert_status(line, loc, mtype, name, *decision, ts=ts2, category=category, factory=factory)
                #     try:
                #         if self.addr_line:
                #             if not self.plc.is_connected():
                #                 self.plc.connect()
                #             vals = self.plc.batch_read_bits(self.addr_line)
                #             on_now = [a for a in self.addr_line if vals.get(a)]
                #             if on_now:
                #                 chosen = sorted(on_now, key=_sort_addr)[0]
                #                 LINE_OVR.set(line, chosen, self.line_bits.get(chosen, chosen))
                #                 LINE_OVR_EPOCH[line] = time.time()
                #                 self.cut_off_streak = 0
                #             else:
                #                 self.cut_off_streak += 1
                #                 if self.cut_off_streak >= self.cut_clear_debounce:
                #                     LINE_OVR.clear(line)
                #                     self.cut_off_streak = 0
                #     except Exception as ee:
                #         print(f"[CUT][READ-L][WARN] {name} → {ee}")
                #     time.sleep(self.poll)
                #     continue

                if not self.plc.is_connected():
                    try:
                        self.plc.connect()
                        self.log_put(f"[{now_hms()}] ORACLE {name}: CONNECTED {self.m['IP']}:{self.m['PORT']}")
                    except Exception as ce:
                        self.log_put(f"[{now_hms()}] ORACLE {name}: PLC/connect error: {ce}")
                        decision = ("OFF", None, None)
                        if decision != self.last_decision:
                            self.last_decision = decision
                            self.streak = self.debounce
                            if self.streak == self.debounce:
                                ts3 = self.db.now_clock()
                                # Log trạng thái trước, không phụ thuộc DB có ghi được hay không.
                                self._log_once(name, decision[0], decision[1], decision[2], "PLC disconnected/read failed")
                                self.db.upsert_status(line, loc, mtype, name, *decision, ts=ts3, category=category, factory=factory)
                        for _ in range(self.reconn * 60):
                            if self.stop_ev.is_set():
                                return
                            time.sleep(1)
                        continue

                # addrs = self.addr_line + self.addr_err + self.addr_wait + self.addr_run
                addrs = self.addr_err + self.addr_wait + self.addr_run
                if hasattr(self.plc, "read_snapshot"):
                    vals, vals_d, _snapshot_ts = self.plc.read_snapshot(addrs, self.addr_d_bt)
                else:
                    vals = self.plc.batch_read_bits(addrs) if addrs else {}
                    vals_d = None

                # on_line = [a for a in self.addr_line if vals.get(a)]
                # if on_line:
                #     chosen = sorted(on_line, key=_sort_addr)[0]
                #     label = self.line_bits.get(chosen, chosen)
                #     LINE_OVR.set(line, chosen, label)
                #     LINE_OVR_EPOCH[line] = time.time()
                #     decision = ("CUT", chosen, label)
                #     if decision == self.last_decision:
                #         self.streak += 1
                #     else:
                #         self.last_decision = decision
                #         self.streak = self.debounce
                # else:
                is_running = False
                if self.addr_run:
                    is_running = all(vals.get(addr, False) for addr in self.addr_run)

                picked_normal = None
                for e in self.err_entries:
                    bit = str(e.get("bit") or "").strip().upper()
                    if bit and vals.get(bit, False):
                        picked_normal = e
                        break

                if is_running:
                    if self.d_bt_groups:
                        if vals_d is None:
                            vals_d = self.plc.batch_read_words(self.addr_d_bt)
                        combos = [tuple(vals_d.get(addr, -1) for addr in group)
                                  for group in self.d_bt_groups]
                        standby_patterns = {(0, 0, 0), (6, 10, 10)}
                        if any(combo in standby_patterns for combo in combos):
                            decision = ("IDLE", "IDLE", "IDLE")
                        elif any(combo == (6, 2, 0) for combo in combos):
                            if picked_normal:
                                decision = ("ERROR", picked_normal["code"], picked_normal["label"])
                            else:
                                waited = self._update_wait_hold(vals)
                                if waited:
                                    decision = ("ERROR", waited[0], waited[1])
                                else:
                                    decision = ("RUN", None, None)
                        else:
                            decision = ("RUN", None, None)
                    else:
                        decision = ("RUN", None, None)
                elif picked_normal :
                    decision = ("ERROR", picked_normal["code"], picked_normal["label"])
                else:
                    waited = self._update_wait_hold(vals)
                    if waited:
                        decision = ("ERROR", waited[0], waited[1])
                    else:
                        decision = ("STOP", None, None)
                if decision == self.last_decision:
                    self.streak += 1
                else:
                    self.last_decision = decision
                    self.streak = 1

                # t0 = LINE_OVR_EPOCH.get(line)
                # if t0 and (time.time() - t0) < LINE_OVR_GUARD_SEC:
                #     if self.last_decision and self.last_decision[0] != "CUT":
                #         o_now = LINE_OVR.get(line)
                #         if o_now:
                #             self.last_decision = ("CUT", o_now["code"], o_now["name"])
                #             self.streak = self.debounce
                #         else:
                #             time.sleep(self.poll)
                #             continue

                # o_now = LINE_OVR.get(line)
                # if o_now and self.last_decision[0] != "CUT":
                #     decision = ("CUT", o_now["code"], o_now["name"])
                #     if decision != self.last_decision:
                #         self.last_decision = decision
                #         self.streak = self.debounce

                if self.streak == self.debounce:
                    ts4 = self.db.now_clock()
                    status, code, label = self.last_decision
                    # Log trạng thái trước, không phụ thuộc DB có ghi được hay không.
                    self._log_once(name, status, code, label)
                    self.db.upsert_status(line, loc, mtype, name, status, code, label, ts=ts4, category=category, factory=factory)

                time.sleep(self.poll)

            except Exception as e:
                self.log_put(f"[{now_hms()}] ORACLE {name}: PLC/read error: {e}")
                try:
                    self.plc.force_disconnect()
                except Exception:
                    pass
                o = LINE_OVR.get(line)
                decision = ("CUT", o["code"], o["name"]) if o else ("OFF", None, None)
                if decision != self.last_decision:
                    self.last_decision = decision
                    self.streak = self.debounce
                    if self.streak == self.debounce:
                        ts5 = self.db.now_clock()
                        reason = "PLC disconnected/read failed" if decision[0] == "OFF" else None
                        # Log trạng thái trước, không phụ thuộc DB có ghi được hay không.
                        self._log_once(name, decision[0], decision[1], decision[2], reason)
                        self.db.upsert_status(line, loc, mtype, name, *decision, ts=ts5, category=category, factory=factory)
                for _ in range(self.reconn * 60):
                    if self.stop_ev.is_set():
                        return
                    time.sleep(1)
                continue

    def stop(self):
        self.stop_ev.set()
        try:
            self.plc.close()
        except Exception:
            pass


# ───────── Table3 DB adapter (sửa theo schema mới) ─────────
class Table3DBAdapter:
    def __init__(self, db: OracleDBAdapter, table_name: str, log_put):
        self.db = db
        self.table_name = table_name
        self.log_put = log_put
        self.last_error = None

    def _machine_binds(self, m: dict) -> dict:
        return {
            "line": m.get("Line"),
            "loc": str(m.get("Location")) if m.get("Location") is not None else None,
            "cat": m.get("Category"),
            "mtype": m.get("MACHINE_TYPE"),
            "name": m.get("Machine_name"),
        }

    def fetch_latest(self, m: dict):
        self.last_error = None
        sql = f"""
            SELECT PASS, FAIL, PASS_NOW, FAIL_NOW, CREATED_AT
              FROM {self.table_name}
             WHERE LINE=:line AND LOCATION=:loc AND CATEGORY=:cat
               AND MACHINE_TYPE=:mtype AND MACHINE_NAME=:name
             ORDER BY CREATED_AT DESC, ID DESC
             FETCH FIRST 1 ROWS ONLY
        """
        try:
            return self.db.core.fetch_one(sql, self._machine_binds(m))
        except Exception as e:
            self.last_error = str(e)
            return None

    def insert_row(self, m: dict, created_at: datetime,
                   pass_delta: int, fail_delta: int,
                   pass_now: int, fail_now: int,
                   cycle_time: float) -> bool:
        self.last_error = None
        sql = f"""
            INSERT INTO {self.table_name}
                (LINE, LOCATION, CATEGORY, FACTORY, MACHINE_TYPE, MACHINE_NAME,
                 CYCLE_TIME, CREATED_AT, PASS, FAIL, PASS_NOW, FAIL_NOW)
            VALUES
                (:line, :loc, :cat, :factory, :mtype, :name,
                 :cycle_time, :created_at, :pass_delta, :fail_delta, :pass_now, :fail_now)
        """
        binds = self._machine_binds(m)
        binds.update({
            "factory": m.get("FACTORY"),
            "cycle_time": cycle_time,
            "created_at": created_at,
            "pass_delta": pass_delta,
            "fail_delta": fail_delta,
            "pass_now": pass_now,
            "fail_now": fail_now,
        })
        ok = self.db.core.execute(sql, binds)
        if not ok:
            self.last_error = self.db.core.last_error
        return ok


# ───────── Table3 worker (sửa logic mới, không reset giờ) ─────────
class Table3Worker(threading.Thread):
    def __init__(self, parent_window, db: Table3DBAdapter, workers: List[MachineWorker],
                 poll_seconds: int, log_put):
        super().__init__(daemon=True)
        self.parent_window = parent_window
        self.db = db
        self.poll_seconds = max(1, int(poll_seconds))
        self.log_put = log_put
        self.stop_ev = threading.Event()

        self.machine_items: List[dict] = []
        self._last_cycle_status = "-"

        for w in workers:
            try:
                pass_addrs = parse_word_pair(w.m.get("PASS")) if w.m.get("PASS") else []
                fail_addrs = parse_word_pair(w.m.get("FAIL")) if w.m.get("FAIL") else []
            except Exception as e:
                self.log_put(f"[{now_hms()}] TABLE3 config lỗi {w.m.get('Machine_name')}: {e}")
                continue
            if not pass_addrs or not fail_addrs:
                continue

            self.machine_items.append({
                "worker": w,
                "m": w.m,
                "pass_addrs": pass_addrs,
                "fail_addrs": fail_addrs,
                "state": {
                    "prev_pass_now": None,
                    "prev_fail_now": None,
                    "table3_status": "-",
                    "db_baseline_loaded": False,
                }
            })

    def stop(self):
        self.stop_ev.set()

    def _set_ui_status(self, value: str):
        try:
            self.parent_window.set_table3_status(value)
        except Exception:
            pass

    def _machine_tag(self, item: dict) -> str:
        m = item["m"]
        line = str(m.get("Line") or "").strip()
        loc_raw = str(m.get("Location") or "").strip()
        try:
            loc = f"{int(loc_raw):02d}" if loc_raw else ""
        except Exception:
            loc = loc_raw.zfill(2) if loc_raw else ""
        if line or loc:
            return f"[{line}_{loc}]" if loc else f"[{line}]"
        return "[TABLE3]"

    def _fmt_ct(self, value) -> str:
        if value is None:
            return "NULL"
        try:
            return f"{float(value):.6f}"
        except Exception:
            return str(value)

    def _read_counts(self, item: dict) -> Tuple[int, int]:
        plc = item["worker"].plc
        addrs = item["pass_addrs"] + item["fail_addrs"]
        vals = plc.batch_read_words(addrs)
        pass_words = [int(vals.get(a, 0)) for a in item["pass_addrs"]]
        fail_words = [int(vals.get(a, 0)) for a in item["fail_addrs"]]
        pass_now = ((pass_words[1] & 0xFFFF) << 16) | (pass_words[0] & 0xFFFF)
        fail_now = ((fail_words[1] & 0xFFFF) << 16) | (fail_words[0] & 0xFFFF)
        return int(pass_now), int(fail_now)

    def _load_db_baseline(self, item: dict):
        st = item["state"]
        latest = self.db.fetch_latest(item["m"])
        if latest is None:
            if self.db.last_error:
                self.log_put(f"[{now_hms()}] {self._machine_tag(item)}: FPY fetch latest FAIL: {self.db.last_error}")
            st["prev_pass_now"] = None
            st["prev_fail_now"] = None
            st["db_baseline_loaded"] = True
            return

        # row = PASS, FAIL, PASS_NOW, FAIL_NOW, CREATED_AT
        _pass_delta, _fail_delta, pass_now, fail_now, _created_at = latest
        st["prev_pass_now"] = None if pass_now is None else int(pass_now)
        st["prev_fail_now"] = None if fail_now is None else int(fail_now)
        st["db_baseline_loaded"] = True

        self.log_put(
            f"[{now_hms()}] {self._machine_tag(item)}: FPY baseline from DB "
            f"PASS_NOW={st['prev_pass_now']} FAIL_NOW={st['prev_fail_now']}"
        )

    def _poll_item(self, item: dict, now_dt: datetime):
        st = item["state"]
        tag = self._machine_tag(item)

        if not st["db_baseline_loaded"]:
            self._load_db_baseline(item)

        pass_now, fail_now = self._read_counts(item)

        # Chưa có baseline -> set baseline lần đầu, không ghi
        if st["prev_pass_now"] is None or st["prev_fail_now"] is None:
            st["prev_pass_now"] = pass_now
            st["prev_fail_now"] = fail_now
            self.log_put(f"[{now_hms()}] {tag}: FPY baseline set PASS_NOW={pass_now}, FAIL_NOW={fail_now}")
            return "-"

        # Không đổi raw -> không ghi
        if pass_now == st["prev_pass_now"] and fail_now == st["prev_fail_now"]:
            return "-"

        pass_delta = pass_now - int(st["prev_pass_now"])
        fail_delta = fail_now - int(st["prev_fail_now"])

        # Nếu counter bị reset tay / quay vòng -> cập nhật baseline, không ghi
        if pass_delta < 0 or fail_delta < 0:
            self.log_put(
                f"[{now_hms()}] {tag}: FPY raw counter changed backward -> "
                f"old PASS_NOW={st['prev_pass_now']} FAIL_NOW={st['prev_fail_now']} / "
                f"new PASS_NOW={pass_now} FAIL_NOW={fail_now} -> baseline reset"
            )
            st["prev_pass_now"] = pass_now
            st["prev_fail_now"] = fail_now
            return "-"

        # Nếu delta tổng bằng 0 -> không ghi
        total_delta = pass_delta + fail_delta
        if total_delta <= 0:
            st["prev_pass_now"] = pass_now
            st["prev_fail_now"] = fail_now
            return "-"

        cycle_time = float(self.poll_seconds) / float(total_delta)  

        ok = self.db.insert_row(
            item["m"],
            now_dt,
            int(pass_delta),
            int(fail_delta),
            int(pass_now),
            int(fail_now),
            float(cycle_time),
        )

        st["table3_status"] = "ok" if ok else "false"

        if ok:
            self.log_put(
                f"[{now_hms()}] {tag}: FPY saved "
                f"PASS={pass_delta} FAIL={fail_delta} "
                f"PASS_NOW={pass_now} FAIL_NOW={fail_now} "
                f"CT={self._fmt_ct(cycle_time)}"
            )
            st["prev_pass_now"] = pass_now
            st["prev_fail_now"] = fail_now
        else:
            self.log_put(f"[{now_hms()}] {tag}: FPY DB FAIL: {self.db.last_error}")

        return st["table3_status"]

    def run(self):
        if not self.machine_items:
            self._set_ui_status("-")
            self.log_put(f"[{now_hms()}] TABLE3: no machine has PASS/FAIL config -> standby")
            return

        self.log_put(
            f"[{now_hms()}] TABLE3 enabled: every {self.poll_seconds}s -> {self.db.table_name} "
            f"({len(self.machine_items)} machine(s))"
        )

        while not self.stop_ev.is_set():
            cycle_status = "-"
            ts_now = self.db.db.now_clock()

            for item in self.machine_items:
                if self.stop_ev.is_set():
                    break
                try:
                    st = self._poll_item(item, ts_now)
                    if st == "false":
                        cycle_status = "false"
                    elif st == "ok" and cycle_status != "false":
                        cycle_status = "ok"
                except Exception as e:
                    self.log_put(f"[{now_hms()}] {self._machine_tag(item)}: FPY poll lỗi: {e}")
                    cycle_status = "false"

            self._last_cycle_status = cycle_status
            self._set_ui_status(cycle_status)

            for _ in range(self.poll_seconds):
                if self.stop_ev.is_set():
                    break
                time.sleep(1)


# ───────── GUI ─────────
class MainWindow(QtWidgets.QMainWindow, Ui_MainWindow):
    table_status_signal = QtCore.pyqtSignal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setupUi(self)
        self.running = False
        self.threads: List[MachineWorker] = []
        self.log_q: "queue.Queue[str]" = queue.Queue()
        self.api_log_q: "queue.Queue[str]" = queue.Queue()
        self.sql_log_q: "queue.Queue[str]" = queue.Queue()
        self.api_supervisor = None
        self.sql_supervisor = None
        self.plc_hub = None
        self.db = None
        self.machines: List[dict] = []
        self.globals_cfg: Optional[dict] = None
        self.table3_worker: Optional[Table3Worker] = None
        self._closing_via_confirm = False
        self._table3_status_value = "-"
        self.table_status_signal.connect(self._on_table_status_signal)

        if hasattr(self, "save_3"):
            self.save_3.clicked.connect(self.on_setting_clicked)
        elif hasattr(self, "setting"):
            self.setting.clicked.connect(self.on_setting_clicked)

        for btn in self.findChildren(QtWidgets.QPushButton):
            txt = btn.text().strip().lower()
            if txt in ("close", "đóng"):
                btn.clicked.connect(self.confirm_close)
                break

        self.set_table1_status("-")
        self.set_table2_status("-")
        self.set_table3_status("-")

        self.timer = QtCore.QTimer(self)
        self.timer.setInterval(250)
        self.timer.timeout.connect(self.drain_logs)
        self.timer.start()

        # Cho Qt render cửa sổ hoàn chỉnh trước khi bắt đầu các bước kết nối
        # Oracle/API có thể mất vài giây.
        QtCore.QTimer.singleShot(150, self.start_workers)

    def log_put(self, msg: str):
        try:
            self.log_q.put_nowait(msg)
        except Exception:
            pass

    def api_log_put(self, msg: str):
        # API log chỉ ghi vào tab API LOG.
        # Không copy/mirror sang ORACLE LOG để Oracle và API độc lập hoàn toàn.
        try:
            self.api_log_q.put_nowait(msg)
        except Exception:
            pass

    def sql_log_put(self, msg: str):
        try:
            self.sql_log_q.put_nowait(msg)
        except Exception:
            pass

    def drain_logs(self):
        # Không drain vô hạn: worker có thể tiếp tục đẩy log trong lúc GUI đang
        # xử lý, khiến event loop không có cơ hội render/cập nhật widget.
        max_logs_per_tick = 200
        drained = False
        oracle_logs = []
        for _ in range(max_logs_per_tick):
            try:
                msg = self.log_q.get_nowait()
            except queue.Empty:
                break
            self.listWidget.addItem(msg)
            if self.listWidget.count() > 500:
                self.listWidget.takeItem(0)
            oracle_logs.append(msg)
            drained = True

        if oracle_logs:
            try:
                date_str = datetime.now().strftime("%Y-%m-%d")
                log_dir = res_path(os.path.join("logs", date_str))
                os.makedirs(log_dir, exist_ok=True)
                log_path = os.path.join(log_dir, "log_oracle.txt")
                with open(log_path, "a", encoding="utf-8") as f:
                    for line in oracle_logs:
                        f.write(line + "\n")
            except Exception as e:
                print(f"Error writing to log_oracle: {e}")

        if drained:
            self.listWidget.scrollToBottom()

        api_drained = False
        api_list = getattr(self, "listWidget_api", None)
        api_logs = []
        for _ in range(max_logs_per_tick):
            try:
                msg = self.api_log_q.get_nowait()
            except queue.Empty:
                break
            if api_list is not None:
                api_list.addItem(msg)
                if api_list.count() > 500:
                    api_list.takeItem(0)
                api_logs.append(msg)
                api_drained = True

        if api_logs:
            try:
                date_str = datetime.now().strftime("%Y-%m-%d")
                log_dir = res_path(os.path.join("logs", date_str))
                os.makedirs(log_dir, exist_ok=True)
                log_path = os.path.join(log_dir, "log_api.txt")
                with open(log_path, "a", encoding="utf-8") as f:
                    for line in api_logs:
                        f.write(line + "\n")
            except Exception as e:
                print(f"Error writing to log_api: {e}")

        if api_drained and api_list is not None:
            api_list.scrollToBottom()

        sql_drained = False
        sql_list = getattr(self, "listWidget_sql", None)
        sql_logs = []
        for _ in range(max_logs_per_tick):
            try:
                msg = self.sql_log_q.get_nowait()
            except queue.Empty:
                break
            if sql_list is not None:
                sql_list.addItem(msg)
                if sql_list.count() > 500:
                    sql_list.takeItem(0)
                sql_logs.append(msg)
                sql_drained = True

        if sql_logs:
            try:
                date_str = datetime.now().strftime("%Y-%m-%d")
                log_dir = res_path(os.path.join("logs", date_str))
                os.makedirs(log_dir, exist_ok=True)
                log_path = os.path.join(log_dir, "log_sql.txt")
                with open(log_path, "a", encoding="utf-8") as f:
                    for line in sql_logs:
                        f.write(line + "\n")
            except Exception as e:
                print(f"Error writing to log_sql: {e}")

        if sql_drained and sql_list is not None:
            sql_list.scrollToBottom()

    def _apply_table_status(self, which: str, value: str):
        if which == "table1":
            lbl = getattr(self, "label_table1_value", None)
        elif which == "table2":
            lbl = getattr(self, "label_table2_value", None)
        elif which == "table3":
            lbl = getattr(self, "label_table3_value", None)
        else:
            lbl = None

        if lbl is not None:
            try:
                lbl.setText(value)
            except Exception:
                pass

    def _on_table_status_signal(self, which: str, value: str):
        self._apply_table_status(which, value)

    def _set_label_text_safe(self, which: str, value: str):
        try:
            if QtCore.QThread.currentThread() is self.thread():
                self._apply_table_status(which, value)
            else:
                self.table_status_signal.emit(which, value)
        except Exception:
            self._apply_table_status(which, value)

    def set_table1_status(self, value: str):
        self._set_label_text_safe("table1", value)

    def set_table2_status(self, value: str):
        self._set_label_text_safe("table2", value)

    def set_table3_status(self, value: str):
        self._table3_status_value = value
        self._set_label_text_safe("table3", value)

    # ───────── CONNECT heartbeat ─────────
    def _connect_ui_set_next_run(self):
        try:
            lbl = getattr(self, 'label_next_run_value', None)
            if lbl is None:
                return

            if getattr(self, "_connect_push_error", False):
                txt = "PUSH DATA ERROR"
            elif getattr(self, '_connect_table_missing', False):
                txt = "- (CONNECT table missing)"
            else:
                nr = getattr(self, '_connect_next_run', None)
                txt = "-" if nr is None else nr.strftime('%Y-%m-%d %H:%M:%S')

            def _apply():
                try:
                    lbl.setText(txt)
                except Exception:
                    pass

            try:
                if QtCore.QThread.currentThread() is not self.thread():
                    QtCore.QTimer.singleShot(0, _apply)
                else:
                    _apply()
            except Exception:
                _apply()

        except Exception:
            pass

    def _connect_tick(self):
        if not getattr(self, 'running', False):
            return
        if getattr(self, '_connect_table_missing', False):
            return
        nr = getattr(self, '_connect_next_run', None)
        if nr is None:
            return
        if datetime.now() < nr:
            return
        if getattr(self, '_connect_inflight', False):
            return

        step = max(1, int(getattr(self, '_connect_poll_seconds', 600)))
        now = datetime.now()
        while self._connect_next_run <= now:
            self._connect_next_run = self._connect_next_run + timedelta(seconds=step)
        self._connect_ui_set_next_run()

        self._connect_inflight = True
        th = threading.Thread(target=self._connect_job, daemon=True)
        th.start()

    def _connect_job(self):
        try:
            with self._connect_lock:
                self._connect_job_locked()
        finally:
            self._connect_inflight = False

    def _connect_job_locked(self):
        core = getattr(self.db, 'core', None)
        if self.db is None or core is None or not hasattr(core, 'pool') or core.pool is None:
            self.log_put(f"[{now_hms()}] CONNECT: DB chưa sẵn sàng -> bỏ qua")
            self._connect_push_error = True
            self.set_table2_status('false')
            self._connect_ui_set_next_run()
            return

        lines = get_unique_lines_from_machine_config()
        factory_by_line = {}
        for m in (self.machines or []):
            line_key = str(m.get("Line")).strip() if m.get("Line") is not None else None
            if line_key and line_key not in factory_by_line:
                factory_by_line[line_key] = m.get("FACTORY")
        if not lines:
            self.log_put(f"[{now_hms()}] CONNECT: không tìm thấy Line trong config_oracle.json")
            self._connect_push_error = True
            self.set_table2_status('false')
            self._connect_ui_set_next_run()
            return

        conn = None
        try:
            conn = core.pool.acquire()
            cur = conn.cursor()
            ts = datetime.now()
            sql = f"INSERT INTO {self._connect_table} (LINE, DATETIME, STATUS, FACTORY) VALUES (:line, :dt, :st, :factory)"
            binds = [{'line': ln, 'dt': ts, 'st': 'OK', 'factory': factory_by_line.get(ln)} for ln in lines]
            cur.executemany(sql, binds)
            conn.commit()

            self._connect_push_error = False
            self.set_table2_status('ok')
            self._connect_ui_set_next_run()
            self.log_put(f"[{now_hms()}] CONNECT: ghi {len(lines)} dòng vào {self._connect_table}")

        except Exception as e:
            msg = str(e)
            self._connect_push_error = True

            if 'ORA-00942' in msg or '00942' in msg:
                self._connect_table_missing = True
                self.set_table2_status('false')
                self._connect_ui_set_next_run()
                self.log_put(f"[{now_hms()}] CONNECT: không có bảng {self._connect_table} -> không ghi")
            else:
                self.set_table2_status('false')
                self._connect_ui_set_next_run()
                self.log_put(f"[{now_hms()}] CONNECT: lỗi ghi {self._connect_table}: {e}")

        finally:
            try:
                if conn is not None:
                    conn.close()
            except Exception:
                pass

    def on_setting_clicked(self):
        try:
            folder = app_dir()
            if sys.platform.startswith("win"):
                os.startfile(folder)
            elif sys.platform == "darwin":
                os.system(f'open "{folder}"')
            else:
                os.system(f'xdg-open "{folder}"')
        except Exception:
            self.log_put(f"[{now_hms()}] Mở thư mục thất bại: {app_dir()}")

    def start_api_supervisor(self):
        if self.api_supervisor is not None and getattr(self.api_supervisor, "running", False):
            return
        try:
            self.api_supervisor = ApiSupervisor(
                app_dir(), log_put=self.api_log_put, plc_hub=self.plc_hub
            )
            self.api_supervisor.start()
        except Exception as e:
            self.api_log_put(f"[{now_hms()}] API START FAIL: {e}")

    def stop_api_supervisor(self):
        try:
            if self.api_supervisor is not None:
                self.api_supervisor.stop()
        except Exception as e:
            self.api_log_put(f"[{now_hms()}] API STOP FAIL: {e}")
        self.api_supervisor = None

    def start_sql_supervisor(self):
        if self.sql_supervisor is not None and getattr(self.sql_supervisor, "running", False):
            return
        try:
            self.sql_supervisor = SqlSupervisor(
                app_dir(), log_put=self.sql_log_put, plc_hub=self.plc_hub
            )
            self.sql_supervisor.start()
        except Exception as e:
            self.sql_log_put(f"[{now_hms()}] SQL START FAIL: {e}")

    def stop_sql_supervisor(self):
        try:
            if self.sql_supervisor is not None:
                self.sql_supervisor.stop()
        except Exception as e:
            self.sql_log_put(f"[{now_hms()}] SQL STOP FAIL: {e}")
        self.sql_supervisor = None

    def confirm_close(self):
        box = QtWidgets.QMessageBox(self)
        box.setIcon(QtWidgets.QMessageBox.Warning)
        box.setWindowTitle("Xác nhận thoát chương trình")
        box.setText("Bạn có chắc muốn thoát chương trình?\nCác worker sẽ dừng lại.")
        box.setStandardButtons(QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No)

        chk = QtWidgets.QCheckBox("Gửi OFF tất cả máy trước khi thoát")
        chk.setChecked(True)
        box.setCheckBox(chk)

        ret = box.exec_()
        if ret == QtWidgets.QMessageBox.Yes:
            self.setEnabled(False)
            QtWidgets.QApplication.setOverrideCursor(QtCore.Qt.WaitCursor)
            try:
                if chk.isChecked():
                    self.send_all_off()
                self.stop_workers()
            finally:
                QtWidgets.QApplication.restoreOverrideCursor()

            self._closing_via_confirm = True
            QtWidgets.QApplication.instance().quit()

    def start_workers(self):
        if self.running:
            return
        try:
            g, machines = load_machine_config()
            line_bits, grouped, wait_by_mt = load_error_catalog_grouped(res_path("config_group.json"))
        except Exception as e:
            self.log_put(f"[{now_hms()}] CONFIG FAIL: Không đọc được config: {e}")
            self.log_put(f"[{now_hms()}] CONFIG FAIL: Kiểm tra runtime_config.json, connect.json, config_oracle.json, config_api.json, config_group.json cạnh .exe")
            self.api_log_put(f"[{now_hms()}] API CONFIG FAIL: Không đọc được config: {e}")
            self.sql_log_put(f"[{now_hms()}] SQL CONFIG FAIL: Không đọc được config: {e}")
            return

        self.machines = machines
        self.globals_cfg = g

        if self.plc_hub is None:
            self.plc_hub = PLCSnapshotHub(
                poll=g.get("poll_interval_sec", 0.5),
                timeout=10.0,
                log_put=self.log_put,
            )

        # API, Oracle và SQL độc lập; một tác vụ tắt/lỗi không chặn các tác vụ khác.
        if g.get("api_enabled", True):
            self.start_api_supervisor()
        else:
            self.api_log_put(f"[{now_hms()}] API disabled by runtime_config.json")

        if g.get("sql_enabled", True):
            self.start_sql_supervisor()
        else:
            self.sql_log_put(f"[{now_hms()}] SQL disabled by runtime_config.json")

        self.running = True

        if not g.get("oracle_enabled", True):
            self.log_put(f"[{now_hms()}] ORACLE disabled by runtime_config.json")
            return

        if not g.get("oracle_user") or not g.get("oracle_password") or not g.get("oracle_dsn"):
            self.set_table1_status('false')
            self.log_put(f"[{now_hms()}] ORACLE CONFIG FAIL: thiếu oracle_user/oracle_password/oracle_dsn trong runtime_config.json")
            self.log_put(f"[{now_hms()}] ORACLE CONFIG FAIL: API vẫn chạy độc lập nếu cấu hình API đúng")
            return

        global LINE_OVR_GUARD_SEC
        LINE_OVR_GUARD_SEC = float(g["poll_interval_sec"]) * int(g["debounce_polls"]) + 0.2

        try:
            self.log_put(f"[{now_hms()}] ORACLE CONNECTING: {g['oracle_dsn']}")
            db = OracleDBAdapter(
                user=g["oracle_user"],
                password=g["oracle_password"],
                dsn=g["oracle_dsn"],
                table_name=g["table_name1"],
                log_put=self.log_put
            )
            ok_db, db_msg = db.test_connection()
            if not ok_db:
                self.set_table1_status('false')
                self.log_put(f"[{now_hms()}] ORACLE CONNECT FAIL: {db_msg}")
                try:
                    db.core.close()
                except Exception:
                    pass
                self.log_put(f"[{now_hms()}] ORACLE CONNECT FAIL: API vẫn chạy độc lập nếu cấu hình API đúng")
                return
            self.set_table1_status('ok')
            self.log_put(f"[{now_hms()}] ORACLE CONNECTED: {g['oracle_dsn']}")
        except Exception as e:
            self.set_table1_status('false')
            self.log_put(f"[{now_hms()}] ORACLE CONNECT FAIL: {e}")
            self.log_put(f"[{now_hms()}] ORACLE CONNECT FAIL: API vẫn chạy độc lập nếu cấu hình API đúng")
            return
        db.main_window = self
        self.db = db
        self.machines = machines
        self.globals_cfg = g

        self.threads = []
        started = 0
        for m in machines:
            if str(m.get("STATUS", "True")).lower() != "true":
                continue
            mt = (m.get("MACHINE_TYPE") or "").strip()
            err_entries = list((grouped.get(mt) or []))
            wait_entries = list((wait_by_mt.get(mt) or []))
            t = MachineWorker(
                db=db, m=m, line_bits=line_bits, err_entries=err_entries,
                poll=g["poll_interval_sec"], debounce=g["debounce_polls"],
                reconn_min=g["reconnect_minutes"], timestop_slots=g["timestop_slots"],
                cut_clear_debounce=g["cut_clear_debounce"],
                log_put=self.log_put,
                wait_entries=wait_entries,
                wait_hold_secs=g.get("wait_hold_secs", 30),
                plc_hub=self.plc_hub,
            )
            t.start()
            self.threads.append(t)
            started += 1

        self.running = True

        try:
            self._connect_table = g.get('table_name2', 'FATP_MACHINE_DATA_CONNECT')
            self._connect_poll_seconds = int(g.get('poll_seconds', 600))
        except Exception:
            self._connect_table = 'FATP_MACHINE_DATA_CONNECT'
            self._connect_poll_seconds = 600

        self._connect_table_missing = False
        self._connect_push_error = False
        self._connect_inflight = False
        self._connect_lock = threading.Lock()
        self._connect_next_run = None
        if self._connect_poll_seconds > 0:
            self._connect_next_run = datetime.now() + timedelta(seconds=self._connect_poll_seconds)

        self._connect_ui_set_next_run()

        self._connect_timer = QtCore.QTimer(self)
        self._connect_timer.setInterval(1000)
        self._connect_timer.timeout.connect(self._connect_tick)
        self._connect_timer.start()

        if not getattr(self, '_connect_inflight', False):
            self._connect_inflight = True
            threading.Thread(target=self._connect_job, daemon=True).start()

        # Table3 mới: giữ shared PLC, chỉ poll delta/raw
        self.table3_worker = Table3Worker(
            parent_window=self,
            db=Table3DBAdapter(db, g.get('table_name3', 'FATP_MACHINE_FPY_DATA'), self.log_put),
            workers=self.threads,
            poll_seconds=g.get('table3_poll_seconds', 120),
            log_put=self.log_put,
        )
        self.table3_worker.start()

        self.log_put(f"[{now_hms()}] Started {started} workers (auto-run).")

    def stop_workers(self):
        if not self.threads:
            try:
                if hasattr(self, '_connect_timer') and self._connect_timer is not None:
                    self._connect_timer.stop()
            except Exception:
                pass
            try:
                if self.table3_worker is not None:
                    self.table3_worker.stop()
                    self.table3_worker.join(timeout=5)
            except Exception:
                pass
            self.table3_worker = None
            self.stop_api_supervisor()
            self.stop_sql_supervisor()
            if self.plc_hub is not None:
                self.plc_hub.stop()
                self.plc_hub = None
            self.running = False
            return
        for t in self.threads:
            try:
                t.stop()
            except Exception:
                pass
        for t in self.threads:
            try:
                t.join()
            except Exception:
                pass

        try:
            if hasattr(self, '_connect_timer') and self._connect_timer is not None:
                self._connect_timer.stop()
        except Exception:
            pass

        self._connect_next_run = None
        self._connect_ui_set_next_run()

        try:
            if self.table3_worker is not None:
                self.table3_worker.stop()
                self.table3_worker.join(timeout=5)
        except Exception:
            pass
        self.table3_worker = None

        self.stop_api_supervisor()
        self.stop_sql_supervisor()

        if self.plc_hub is not None:
            self.plc_hub.stop()
            self.plc_hub = None

        self.threads = []
        self.running = False
        self.log_put(f"[{now_hms()}] Stopped all workers.")

    def send_all_off(self):
        try:
            if self.globals_cfg is not None and not self.globals_cfg.get("oracle_enabled", True):
                self.log_put(f"[{now_hms()}] ORACLE disabled: bỏ qua gửi OFF vào Oracle")
                return
            if self.db is None or not self.machines:
                g, machines = load_machine_config()
                if not g.get("oracle_enabled", True):
                    self.log_put(f"[{now_hms()}] ORACLE disabled: bỏ qua gửi OFF vào Oracle")
                    return
                self.db = OracleDBAdapter(user=g["oracle_user"], password=g["oracle_password"],
                                          dsn=g["oracle_dsn"], table_name=g["table_name1"], log_put=self.log_put)
                self.machines = machines
                self.globals_cfg = g
            ts = self.db.now_clock()
            count = 0
            for m in self.machines:
                if str(m.get("STATUS", "True")).lower() != "true":
                    continue
                line = m["Line"]
                loc = str(m["Location"])
                mtype = m["MACHINE_TYPE"]
                name = m["Machine_name"]
                category = m.get("Category")
                factory = m.get("FACTORY")
                try:
                    self.db.upsert_status(line, loc, mtype, name, "OFF", None, None, ts=ts, category=category, factory=factory)
                    count += 1
                except Exception as ex:
                    self.log_put(f"[{now_hms()}] Gửi OFF lỗi cho {name}: {ex}")
            self.log_put(f"[{now_hms()}] Đã gửi OFF cho {count} máy.")
        except Exception as e:
            self.log_put(f"[{now_hms()}] Gửi OFF lỗi: Không thể gửi OFF cho tất cả máy: {e}")

    def closeEvent(self, event):
        if not getattr(self, "_closing_via_confirm", False):
            event.ignore()
            self.confirm_close()
            return

        try:
            if self.running:
                self.stop_workers()
        finally:
            super().closeEvent(event)


# ───────── App bootstrap ─────────
def main():
    try:
        with open(res_path("runtime_config.json"), "r", encoding="utf-8") as f:
            rt_raw = json.load(f)
    except Exception:
        rt_raw = {}
    license_key = (rt_raw.get("license_key") or (rt_raw.get("config") or {}).get("license_key") or "").strip()
    if license_key != EXPECTED_LICENSE:
        QtWidgets.QMessageBox.critical(None, "License", "Sai hoặc thiếu license_key.")
        sys.exit(2)

    app = QtWidgets.QApplication(sys.argv)
    w = MainWindow()
    w.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
