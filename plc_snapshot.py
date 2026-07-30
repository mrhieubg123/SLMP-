import re
import threading
import time
from typing import Dict, Iterable, Optional, Tuple

import pymcprotocol


_BIT_RE = re.compile(r"^((?:SM|M|L))(\d+)$")
_WORD_RE = re.compile(r"^([A-Z]+)(\d+)$")


def _norm(addr) -> str:
    return str(addr or "").strip().upper()


def _groups(numbers):
    numbers = sorted(set(numbers))
    if not numbers:
        return []
    result = []
    start = prev = numbers[0]
    for number in numbers[1:]:
        if number == prev + 1:
            prev = number
        else:
            result.append((start, prev - start + 1))
            start = prev = number
    result.append((start, prev - start + 1))
    return result


class _SnapshotEndpoint(threading.Thread):
    def __init__(self, ip: str, port: int, poll: float, timeout: float, log_put=None):
        super().__init__(daemon=True, name=f"PLC-snapshot-{ip}:{port}")
        self.ip, self.port = str(ip).strip(), int(port)
        self.poll = max(0.05, float(poll))
        self.timeout = float(timeout)
        self.log_put = log_put or (lambda _msg: None)
        # lock chỉ bảo vệ subscription/snapshot; tuyệt đối không giữ lock này
        # trong lúc network I/O để GUI có thể subscribe tức thời.
        self.lock = threading.RLock()
        self.socket_lock = threading.RLock()
        self.changed = threading.Event()
        self.ready = threading.Event()
        self.stop_ev = threading.Event()
        self.bit_addrs, self.word_addrs = set(), set()
        self.bits: Dict[str, bool] = {}
        self.words: Dict[str, int] = {}
        self.updated_at = 0.0
        self.error: Optional[Exception] = None
        self.mc = None

    def subscribe(self, bits: Iterable[str] = (), words: Iterable[str] = ()):
        with self.lock:
            before = (len(self.bit_addrs), len(self.word_addrs))
            self.bit_addrs.update(a for a in map(_norm, bits) if a)
            self.word_addrs.update(a for a in map(_norm, words) if a)
            after = (len(self.bit_addrs), len(self.word_addrs))
        if after != before:
            self.changed.set()

    def _connect(self):
        if self.mc is not None:
            return
        mc = pymcprotocol.Type3E()
        mc.soc_timeout = self.timeout
        mc.connect(self.ip, self.port)
        self.mc = mc

    def _close(self):
        try:
            if self.mc is not None:
                self.mc.close()
        except Exception:
            pass
        self.mc = None

    def _read_bits(self, addrs) -> Dict[str, bool]:
        by_dev = {}
        for addr in addrs:
            match = _BIT_RE.fullmatch(addr)
            if not match:
                raise ValueError(f"Invalid bit address: {addr}")
            by_dev.setdefault(match.group(1), []).append(int(match.group(2)))
        result = {addr: False for addr in addrs}
        for dev, numbers in by_dev.items():
            for start, length in _groups(numbers):
                values = self.mc.batchread_bitunits(headdevice=f"{dev}{start}", readsize=length)
                for index, value in enumerate(values):
                    addr = f"{dev}{start + index}"
                    if addr in result:
                        result[addr] = bool(value)
        return result

    def _read_words(self, addrs) -> Dict[str, int]:
        by_dev = {}
        for addr in addrs:
            match = _WORD_RE.fullmatch(addr)
            if not match:
                raise ValueError(f"Invalid word address: {addr}")
            by_dev.setdefault(match.group(1), []).append(int(match.group(2)))
        result = {addr: 0 for addr in addrs}
        for dev, numbers in by_dev.items():
            for start, length in _groups(numbers):
                values = self.mc.batchread_wordunits(headdevice=f"{dev}{start}", readsize=length)
                for index, value in enumerate(values):
                    addr = f"{dev}{start + index}"
                    if addr in result:
                        result[addr] = int(value)
        return result

    def run(self):
        while not self.stop_ev.is_set():
            try:
                with self.lock:
                    bit_addrs = sorted(self.bit_addrs)
                    word_addrs = sorted(self.word_addrs)
                # Chỉ socket_lock được giữ trong network I/O. Việc subscribe
                # vẫn truy cập được self.lock dù PLC đang timeout.
                with self.socket_lock:
                    self._connect()
                    bits = self._read_bits(bit_addrs) if bit_addrs else {}
                    words = self._read_words(word_addrs) if word_addrs else {}
                with self.lock:
                    self.bits, self.words = bits, words
                    self.updated_at = time.time()
                    self.error = None
                    self.ready.set()
            except Exception as exc:
                with self.lock:
                    self.error = exc
                self.ready.set()
                with self.socket_lock:
                    self._close()
            self.changed.wait(self.poll)
            self.changed.clear()
        with self.socket_lock:
            self._close()

    def snapshot(self, bits=(), words=(), timeout: Optional[float] = None):
        bit_keys = {_norm(a) for a in bits if _norm(a)}
        word_keys = {_norm(a) for a in words if _norm(a)}
        self.subscribe(bit_keys, word_keys)
        limit = time.time() + (self.timeout if timeout is None else timeout)
        while time.time() < limit:
            self.ready.wait(max(0.01, limit - time.time()))
            with self.lock:
                if self.error is not None:
                    raise RuntimeError(str(self.error))
                if bit_keys.issubset(self.bits) and word_keys.issubset(self.words):
                    return (
                        {a: self.bits.get(_norm(a), False) for a in bits},
                        {a: self.words.get(_norm(a), -1) for a in words},
                        self.updated_at,
                    )
            time.sleep(0.01)
        raise TimeoutError(f"PLC snapshot timeout {self.ip}:{self.port}")

    def write_words(self, word_map):
        with self.socket_lock:
            self._connect()
            by_dev = {}
            for addr, value in (word_map or {}).items():
                addr = _norm(addr)
                match = _WORD_RE.fullmatch(addr)
                if not match:
                    raise ValueError(f"Invalid word address: {addr}")
                by_dev.setdefault(match.group(1), {})[int(match.group(2))] = int(value)
            for dev, values_by_number in by_dev.items():
                for start, length in _groups(values_by_number):
                    values = [values_by_number[start + offset] for offset in range(length)]
                    self.mc.batchwrite_wordunits(headdevice=f"{dev}{start}", values=values)
            self.changed.set()


class PLCSnapshotClient:
    def __init__(self, endpoint: _SnapshotEndpoint):
        self.endpoint = endpoint

    def subscribe(self, bits=(), words=()):
        self.endpoint.subscribe(bits, words)

    def connect(self):
        self.endpoint.changed.set()

    def close(self):
        pass

    def force_disconnect(self):
        # Connection belongs to the shared polling thread. Consumers only ask
        # it to retry; they must never close a socket another consumer uses.
        self.endpoint.changed.set()

    def is_connected(self):
        return self.endpoint.mc is not None and self.endpoint.error is None

    def batch_read_bits(self, addrs):
        return self.endpoint.snapshot(bits=addrs)[0]

    def batch_read_words(self, addrs):
        return self.endpoint.snapshot(words=addrs)[1]

    def read_snapshot(self, bit_addrs=(), word_addrs=()):
        return self.endpoint.snapshot(bits=bit_addrs, words=word_addrs)

    def batch_write_words(self, word_map):
        self.endpoint.write_words(word_map)


class PLCSnapshotHub:
    def __init__(self, poll: float = 0.2, timeout: float = 10.0, log_put=None):
        self.poll, self.timeout, self.log_put = poll, timeout, log_put
        self.lock = threading.RLock()
        self.endpoints: Dict[Tuple[str, int], _SnapshotEndpoint] = {}

    def client(self, ip: str, port: int) -> PLCSnapshotClient:
        key = (str(ip).strip(), int(port))
        with self.lock:
            endpoint = self.endpoints.get(key)
            if endpoint is None:
                endpoint = _SnapshotEndpoint(*key, self.poll, self.timeout, self.log_put)
                self.endpoints[key] = endpoint
                endpoint.start()
        return PLCSnapshotClient(endpoint)

    def stop(self):
        with self.lock:
            endpoints = list(self.endpoints.values())
            self.endpoints.clear()
        for endpoint in endpoints:
            endpoint.stop_ev.set()
            endpoint.changed.set()
        for endpoint in endpoints:
            endpoint.join(timeout=5)
