"""从本机企业微信（WXWork）的 CEF Cookie 缓存中提取 JSESSIONID。

学校缴费系统的会话 Cookie 落在企业微信内嵌 CEF 浏览器的 SQLite Cookie 库里，
值使用 Windows DPAPI + AES-256-GCM 加密。本脚本读取该库并解密，
**无需任何抓包工具或代理中间人**。

用法：
    pip install pycryptodome
    python tools/extract_cookie.py                            # 列出全部命中
    python tools/extract_cookie.py --host pay2.hjnu.edu.cn    # 只看某域名
    python tools/extract_cookie.py --ready                    # 只输出可粘贴的值
    python tools/extract_cookie.py --auto --push              # 全自动（推荐，配 bat 用）：
        # 自动带调试口重启企业微信（登录态保留），用户只需在客户端里点开一次
        # 「网上缴学杂费 / 校园一卡通」页面，脚本通过 CDP（Chrome DevTools 协议）
        # 直接从 webview 内存里读会话 Cookie，验活后自动推给机器人
    python tools/extract_cookie.py --host pay2.hjnu.edu.cn --watch --push
        # 旧一条龙：只盯本地 Cookie 库文件（WXWork 5.0.11 起缴费页 Cookie 落
        # 加密库、明文库读不到，此路基本只对老版本有效），等新 Cookie 验活后推送

⚠️ 版本实测（2026-10-05/06，WXWork 5.0.11.6018）：
- 工作台/公众号 H5 页面的 Cookie 从 5.0.11 起写入加密的
  Profiles\\<hash>\\webview\\webview.db（非 SQLCipher、自研格式），明文库提取已死；
  --auto 的 CDP 通道（--remote-debugging-port）是唯一可行的自动捕获方式。
- CDP 只在带调试口启动的实例上可用：本脚本会自动重启企业微信（登录态保留）。
- OAuth 授权页（open.weixin.qq.com）在 CDP 自建的标签里过不去（服务端校验
  客户端身份），所以「让脚本自己开页面」不成立，必须借用户点开的真实页面。

注意：
- 本脚本只在本机运行，不会被插件加载（AstrBot 只加载 main.py），
  也不进 requirements.txt（插件运行时不依赖它）。
- 企业微信运行时会锁住 Cookies 库，此时用 esentutl 拷贝副本再读。
- --watch/--push 为纯标准库实现（urllib/getpass/ctypes），无新增依赖；
  --auto 的 CDP 通道需要 `pip install websocket-client`（缺失时自动降级为只盯
  明文库并给出安装提示）。--push 的密码从环境变量 ASTRBOT_PASS 读取，
  没有则提示输入，不落盘。
- 脚本自身已把 stdout/stderr 切到 UTF-8；若在 PowerShell 5.1 里看到中文乱码，
  那是控制台代码页问题，先执行 `[Console]::OutputEncoding = [System.Text.Encoding]::UTF8`。
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import getpass
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from ctypes import wintypes
from pathlib import Path

COOKIE_NAME = "JSESSIONID"
WALKSALT = b"DPAPI"  # Local State 里 encrypted_key 解 base64 后的固定前缀

# ---- 一键更新（--watch/--push）相关默认值 ----
DEFAULT_BOT_URL = "http://43.143.104.106:6185"
DEFAULT_BOT_USER = "qxm"
DEFAULT_SCHOOL_BASE = "http://pay2.hjnu.edu.cn"
DEFAULT_AID = "0030000000004301"  # 空调费 aid，仅用于验活探测（--aid 可覆盖）
DEFAULT_PAY_URL = (
    "http://pay2.hjnu.edu.cn/wechat/url/redirectJkbh.html?jkbh=null"
)
DEFAULT_HOST_FILTER = "pay2"
WXWORK_UA = (
    "Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/107.0.5304.110 Safari/537.36 Language/zh ColorScheme/Light "
    "wxwork/5.0.11 (MicroMessenger/6.2) WindowsWechat MailPlugin_Electron WeMail "
    "embeddisk wwmver/3.26.511.637 noMediaCs/true"
)
# 从 WXWork.exe 二进制里扒出的「内置浏览器打开 URL」动词。
# ⚠️ 2026-10-05 实测（WXWork 5.0.11.6018）：外部 wxwork:// 调用一律弹「打不开此链接」，
# 这些动词只供页面内部 JS 桥使用。保留为 --try-verbs 实验项，供未来版本复测。
VERB_TEMPLATES = (
    "wxwork://openh5?url={url}",
    "wxwork://click_open_link?url={url}",
    "wxwork://openurl?url={url}",
)
WATCH_POLL_SECONDS = 2.0
VERB_WAIT_SECONDS = 20
MANUAL_WAIT_SECONDS = 300

# ---- 全自动模式（--auto，CDP 捕获通道）----
CDP_PORT = 9222
WXWORK_DEBUG_ARGS = (
    f"--remote-debugging-port={CDP_PORT}",
    "--remote-allow-origins=*",
    "--no-sandbox",
)
AUTO_POLL_SECONDS = 3.0
AUTO_CDP_BOOT_SECONDS = 120  # 重启后等调试口就绪（含扫码登录时间）


def _out() -> None:
    """PowerShell 5.1 控制台默认 GBK，统一切 UTF-8 以免中文乱码；逐行刷新防交错。"""
    try:
        sys.stdout.reconfigure(
            encoding="utf-8", errors="replace", line_buffering=True
        )
        sys.stderr.reconfigure(
            encoding="utf-8", errors="replace", line_buffering=True
        )
    except (AttributeError, ValueError):
        pass


class _Blob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def dpapi_decrypt(blob: bytes) -> bytes:
    """调用 Windows DPAPI 解密（CryptUnprotectData），不依赖 pywin32。"""
    buf = ctypes.create_string_buffer(blob, len(blob))
    src = _Blob(len(blob), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    dst = _Blob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    crypt32.CryptUnprotectData.restype = wintypes.BOOL
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(src), None, None, None, None, 0, ctypes.byref(dst)
    )
    if not ok:
        raise OSError(
            f"CryptUnprotectData 失败（WinError {kernel32.GetLastError()}）："
            "通常意味着当前用户不是该 Cookie 库的解密主体"
        )
    try:
        return ctypes.string_at(dst.pbData, dst.cbData)
    finally:
        kernel32.LocalFree(dst.pbData)


def load_master_key(wxwork_dir: Path) -> bytes:
    """从 Local State 取出 AES 主密钥（企业微信不做 KDF 派生，直接使用）。"""
    state_path = wxwork_dir / "Local State"
    if not state_path.is_file():
        raise FileNotFoundError(f"未找到企业微信 Local State：{state_path}")
    state = json.loads(state_path.read_text(encoding="utf-8"))
    encoded = (state.get("os_crypt") or {}).get("encrypted_key") or ""
    if not encoded:
        raise ValueError("Local State 中没有 os_crypt.encrypted_key（可能未登录过企业微信）")
    raw = base64.b64decode(encoded)
    if not raw.startswith(WALKSALT):
        raise ValueError(f"encrypted_key 前缀异常，期望 {WALKSALT!r}，实际 {raw[:5]!r}")
    key = dpapi_decrypt(raw[len(WALKSALT) :])
    if len(key) not in (16, 32):
        raise ValueError(f"解密出的主密钥长度异常：{len(key)} 字节")
    return key


def decrypt_cookie(encrypted_value: bytes, key: bytes) -> str | None:
    """解密单条 cookie（v10/v11 = 前缀3字节 + nonce12 + 密文 + tag16）。"""
    if len(encrypted_value) < 31 or encrypted_value[:3] not in (b"v10", b"v11"):
        return None
    nonce = encrypted_value[3:15]
    ciphertext = encrypted_value[15:-16]
    tag = encrypted_value[-16:]
    try:
        from Crypto.Cipher import AES

        cipher = AES.new(key, AES.MODE_GCM, nonce=nonce)
        return cipher.decrypt_and_verify(ciphertext, tag).decode("utf-8", "replace")
    except (ImportError, ValueError, KeyError):
        # MAC 校验失败或缺少 pycryptodome 时跳过该条，不影响其余条目
        return None


def _copy_readable(src: Path, workdir: Path) -> Path | None:
    """拷一份 Cookie 库到临时目录；企业微信锁文件时降级用 esentutl。"""
    dst = workdir / "Cookies"
    try:
        shutil.copy2(src, dst)
        return dst
    except (PermissionError, OSError):
        pass
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.run(
            ["esentutl", "/y", str(src), "/d", str(dst), "/o"],
            check=True,
            capture_output=True,
            creationflags=flags,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return dst if dst.is_file() else None


def _chrome_time(raw) -> str:
    """expires_utc 是 1601-01-01 起的微秒；0 表示会话 Cookie（闲置即失效）。"""
    if not raw:
        return "会话 Cookie（无过期时间，闲置即失效）"
    import datetime

    try:
        when = datetime.datetime(1601, 1, 1) + datetime.timedelta(microseconds=int(raw))
    except (OverflowError, TypeError, ValueError):
        return f"原始值 {raw}"
    return when.strftime("%Y-%m-%d %H:%M:%S")


def find_cookie_dbs(wxwork_dir: Path) -> list[Path]:
    """企业微信的 CEF 缓存：账号缓存、qtCef（工作台 webview）与根级 Default（内置浏览器）各有一份 Cookies 库。"""
    found: list[Path] = []
    for pattern in (
        "*/WXWorkCefCache/Network/Cookies",
        "qtCef/Network/Cookies",
        "Default/Network/Cookies",
    ):
        found.extend(sorted(wxwork_dir.glob(pattern)))
    # 去重并保持顺序
    seen: set[Path] = set()
    unique: list[Path] = []
    for db in found:
        if db not in seen:
            seen.add(db)
            unique.append(db)
    return unique


def collect(
    wxwork_dir: Path,
    host_filter: str = "",
    name: str = COOKIE_NAME,
    quiet: bool = False,
) -> list[dict]:
    """扫描全部 Cookie 库，返回解出明文的 cookie 记录列表。

    quiet=True 时不再把「文件被占用」之类提示打到 stderr（轮询场景防刷屏）。
    """
    key = load_master_key(wxwork_dir)
    records: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="wxcookie_") as tmp:
        for db in find_cookie_dbs(wxwork_dir):
            readable = _copy_readable(db, Path(tmp))
            if readable is None:
                if not quiet:
                    print(
                        f"  ! 读取失败（文件被占用且 esentutl 绕锁失败）：{db}",
                        file=sys.stderr,
                    )
                continue
            try:
                conn = sqlite3.connect(f"file:{readable.as_posix()}?mode=ro", uri=True)
            except sqlite3.Error as e:
                if not quiet:
                    print(f"  ! 打开失败：{db}（{e}）", file=sys.stderr)
                continue
            try:
                rows = conn.execute(
                    "SELECT host_key, name, value, encrypted_value, expires_utc "
                    "FROM cookies WHERE name = ?",
                    (name,),
                ).fetchall()
            except sqlite3.Error as e:
                if not quiet:
                    print(f"  ! 查询失败：{db}（{e}）", file=sys.stderr)
                continue
            finally:
                conn.close()
            for host, cname, plain, encrypted, expires in rows:
                if host_filter and host_filter not in host:
                    continue
                value = plain or ""
                if not value and encrypted:
                    value = decrypt_cookie(bytes(encrypted), key) or ""
                if not value:
                    continue
                records.append(
                    {
                        "db": db,
                        "host": host,
                        "name": cname,
                        "value": value,
                        "expires": _chrome_time(expires),
                    }
                )
    return records


# ================= 一键更新：唤起企业微信 / 等 Cookie / 推送机器人 =================


def _http(
    url: str,
    *,
    method: str = "GET",
    json_body: dict | None = None,
    form: dict | None = None,
    headers: dict | None = None,
    timeout: float = 30,
) -> tuple[int, str, str]:
    """urllib 薄封装：4xx/5xx 也返回 (status, body, content-type) 而不抛异常。"""
    hdrs = {"User-Agent": WXWORK_UA}
    if headers:
        hdrs.update(headers)
    data = None
    if json_body is not None:
        data = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    elif form is not None:
        data = urllib.parse.urlencode(form).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace"), (
                resp.headers.get("Content-Type") or ""
            )
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")
        except OSError:
            body = ""
        return e.code, body, e.headers.get("Content-Type", "") if e.headers else ""


def probe_alive(jsessionid: str, aid: str, school_base: str) -> bool:
    """拿 cookie 真查一次校区列表：91001=死，其余（含学校 5xx）=会话被认可。"""
    status, body, _ = _http(
        school_base.rstrip("/") + "/wechat/basicQuery/queryElecArea.html",
        method="POST",
        form={"aid": aid},
        headers={
            "Cookie": f"{COOKIE_NAME}={jsessionid}",
            "Referer": school_base.rstrip("/") + "/wechat/elecpay/queryelec.html",
        },
        timeout=15,
    )
    if status >= 500:
        # 学校侧异常页；按实测记录，已登录会话打接口才会走到 500，视为活
        return True
    return '"retcode":"91001"' not in body.replace(" ", "")


def wxwork_running() -> bool:
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq WXWork.exe"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "WXWork.exe" in out


def find_wxwork_exe(override: str = "") -> Path | None:
    if override:
        p = Path(override)
        return p if p.is_file() else None
    candidates = [
        Path(r"D:\企业微信\WXWork\WXWork.exe"),
        Path(r"C:\Program Files (x86)\WXWork\WXWork.exe"),
        Path(r"C:\Program Files\WXWork\WXWork.exe"),
    ]
    try:
        import winreg

        for root in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
            try:
                with winreg.OpenKey(
                    root,
                    r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\WXWork.exe",
                ) as key:
                    val = winreg.QueryValueEx(key, "")[0]
                    if val:
                        candidates.insert(0, Path(val.strip('"')))
            except OSError:
                continue
    except (ImportError, OSError):
        pass
    for c in candidates:
        if c.is_file():
            return c
    return None


def ensure_wxwork_running(exe_override: str = "") -> bool:
    if wxwork_running():
        return True
    exe = find_wxwork_exe(exe_override)
    if exe is None:
        print("  ! 未找到 WXWork.exe，请手动打开企业微信", file=sys.stderr)
        return False
    print(f"  启动 {exe}")
    flags = getattr(subprocess, "DETACHED_PROCESS", 0)
    try:
        subprocess.Popen([str(exe)], creationflags=flags, cwd=str(exe.parent))
    except OSError as e:
        print(f"  ! 启动失败：{e}，请手动打开企业微信", file=sys.stderr)
        return False
    for _ in range(30):
        time.sleep(1)
        if wxwork_running():
            time.sleep(3)  # 等主窗口渲染
            return True
    print("  ! 企业微信 30 秒内没起来，请手动打开", file=sys.stderr)
    return False


def activate_wxwork_window() -> None:
    """把企业微信主窗口拉到前台（尽力而为，失败不影响流程）。"""
    try:
        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(None, "企业微信")
        if hwnd:
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
    except Exception:
        pass


# ================= 全自动模式（--auto）：CDP 捕获通道 =================


def _kill_wxwork() -> None:
    """结束全部企业微信进程，释放 Cookies 库文件锁与 CDP 端口。"""
    for image in ("WXWork.exe", "WXWorkWeb.exe"):
        subprocess.run(
            ["taskkill", "/F", "/IM", image, "/T"],
            capture_output=True,
            check=False,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )


def _snapshot_cookie_dbs(wxwork_dir: Path) -> list[Path]:
    """锁释放窗口内把全部 Cookies 库（含 -wal/-shm）快照到临时目录。

    进程刚死、新进程未起的间隙里文件不锁，直接 copy2 即可；拷不动的跳过。
    """
    tmp = Path(tempfile.mkdtemp(prefix="wxsnap_"))
    out: list[Path] = []
    for db in find_cookie_dbs(wxwork_dir):
        dst = tmp / f"{db.parent.parent.name.replace(' ', '_')}_{db.name}"
        try:
            shutil.copy2(db, dst)
        except OSError:
            continue
        out.append(dst)
        for sfx in ("-wal", "-shm"):
            extra = db.with_name(db.name + sfx)
            if extra.exists():
                try:
                    shutil.copy2(extra, dst.with_name(dst.name + sfx))
                except OSError:
                    pass
    return out


def _scan_snapshots(snaps: list[Path], host_filter: str, name: str) -> list[str]:
    """从快照库里解出 cookie 值（验活由调用方做）。"""
    values: list[str] = []
    for db in snaps:
        try:
            conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        except sqlite3.Error:
            continue
        try:
            rows = conn.execute(
                "SELECT host_key, name, value, encrypted_value "
                "FROM cookies WHERE name = ?",
                (name,),
            ).fetchall()
        except sqlite3.Error:
            continue
        finally:
            conn.close()
        for host, _cname, plain, _encrypted in rows:
            if host_filter and host_filter not in host:
                continue
            if plain:
                values.append(plain)
    return list(dict.fromkeys(values))


def _cdp_ready(port: int = CDP_PORT) -> bool:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/json/version", timeout=3
        ) as resp:
            return resp.status == 200
    except (urllib.error.URLError, OSError, ValueError):
        return False


def _cdp_wait(seconds: float, port: int = CDP_PORT) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if _cdp_ready(port):
            return True
        time.sleep(2.0)
    return False


def _cdp_cookie_values(host_filter: str, port: int = CDP_PORT) -> list[str]:
    """attach 全部 page 目标，用 Network.getAllCookies 汇总内存里的 cookie 值。

    每次调用都新建/关闭 WebSocket 连接（轮询频率低，代价可忽略）；
    websocket-client 缺失或任何一步失败都返回空列表，不影响明文库通道。
    """
    try:
        import websocket  # type: ignore[import-not-found]
    except ImportError:
        return []
    try:
        ver = json.loads(
            urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/version", timeout=5
            ).read()
        )
        ws = websocket.create_connection(
            ver["webSocketDebuggerUrl"], timeout=15, suppress_origin=True
        )
    except Exception:
        return []
    state = {"id": 0}

    def cmd(method: str, params: dict | None = None, sid: str | None = None) -> dict:
        state["id"] += 1
        msg: dict = {"id": state["id"], "method": method, "params": params or {}}
        if sid:
            msg["sessionId"] = sid
        ws.send(json.dumps(msg))
        while True:
            data = json.loads(ws.recv())
            if data.get("id") == state["id"]:
                return data

    def page_values(target_id: str) -> list[str]:
        """attach 单个 page 目标取 cookie 值；单个目标挂了不影响其余。"""
        try:
            r = cmd("Target.attachToTarget", {"targetId": target_id, "flatten": True})
            sid = (r.get("result") or {}).get("sessionId")
            if not sid:
                return []
            cookies = (
                cmd("Network.getAllCookies", sid=sid).get("result") or {}
            ).get("cookies", [])
        except Exception:
            return []
        out = []
        for c in cookies:
            if host_filter and host_filter not in (c.get("domain") or ""):
                continue
            if c.get("name") == COOKIE_NAME and c.get("value"):
                out.append(c["value"])
        return out

    values: list[str] = []
    try:
        targets = json.loads(
            urllib.request.urlopen(
                f"http://127.0.0.1:{port}/json/list", timeout=5
            ).read()
        )
    except (OSError, ValueError):
        ws.close()
        return []
    for t in targets:
        if t.get("type") != "page":
            continue
        values.extend(page_values(t["id"]))
    ws.close()
    return list(dict.fromkeys(values))


def _find_devtools_ports(known: set[int], wxwork_dir: Path) -> set[int]:
    """扫 DevToolsActivePort 文件（CEF 随机端口时写在这里），返回新发现的端口。"""
    found: set[int] = set()
    try:
        files = list(wxwork_dir.rglob("DevToolsActivePort"))
    except OSError:
        return found
    for f in files:
        try:
            port = int(f.read_text(encoding="utf-8", errors="replace").split()[0])
        except (OSError, ValueError, IndexError):
            continue
        if port not in known and _cdp_ready(port):
            found.add(port)
    return found


def _capture_new_value(
    args: argparse.Namespace,
    wxwork_dir: Path,
    seen: list[str],
    seconds: float,
    ports: set[int],
    quiet_stores: bool = True,
) -> str:
    """全自动捕获循环：CDP 全端口 + DevToolsActivePort + 明文库三路并查。

    拿到「seen 里没有的」新值就验活，学校认可才返回；超时返回空串。
    """
    deadline = time.monotonic() + seconds
    host = args.host or DEFAULT_HOST_FILTER
    while time.monotonic() < deadline:
        for port in sorted(ports):
            for v in _cdp_cookie_values(host, port):
                if v in seen:
                    continue
                try:
                    if probe_alive(v, args.aid, args.school_base):
                        print("  ✓ CDP 捕获到新凭证，学校已认可")
                        return v
                except RuntimeError as e:
                    print(f"  ! {e}", file=sys.stderr)
                    return ""
                seen.append(v)
        for p in _find_devtools_ports(ports, wxwork_dir):
            print(f"  发现新调试端点：127.0.0.1:{p}")
            ports.add(p)
        try:
            records = collect(wxwork_dir, host, args.name, quiet=quiet_stores)
        except (FileNotFoundError, ValueError, OSError):
            records = []
        for r in records:
            v = r["value"]
            if v in seen:
                continue
            try:
                if probe_alive(v, args.aid, args.school_base):
                    print("  ✓ 明文库捕获到新凭证，学校已认可")
                    return v
            except RuntimeError as e:
                print(f"  ! {e}", file=sys.stderr)
                return ""
            seen.append(v)
        time.sleep(AUTO_POLL_SECONDS)
    return ""


def wait_new_cookie(
    args: argparse.Namespace, seen: list[str], seconds: float
) -> str:
    """轮询 Cookie 库等「seen 里没有的」新值，验活通过才返回。"""
    deadline = time.monotonic() + seconds
    wxdir = Path(args.wxwork_dir).expanduser()
    host = args.host or DEFAULT_HOST_FILTER
    while time.monotonic() < deadline:
        try:
            records = collect(wxdir, host, args.name)
        except (FileNotFoundError, ValueError, OSError):
            records = []
        for r in records:
            v = r["value"]
            if v in seen:
                continue
            try:
                alive = probe_alive(v, args.aid, args.school_base)
            except RuntimeError as e:
                print(f"  ! {e}", file=sys.stderr)
                return ""
            if alive:
                print("  ✓ 捕获到新凭证且学校已认可")
                return v
            seen.append(v)
        time.sleep(WATCH_POLL_SECONDS)
    return ""


def bot_login(base: str, user: str, password: str) -> str:
    status, body, _ = _http(
        base + "/api/v1/auth/login",
        method="POST",
        json_body={
            "username": user,
            "password": password,
            "code": None,
            "trust_device_flag": False,
        },
        timeout=30,
    )
    try:
        obj = json.loads(body)
    except ValueError as e:
        raise RuntimeError(f"登录响应不是 JSON（HTTP {status}）：{body[:160]}") from e
    data = obj.get("data") if isinstance(obj, dict) else None
    token = ""
    if isinstance(data, dict):
        token = data.get("token") or ""
    elif isinstance(data, str):
        token = data
    if not token and isinstance(obj, dict):
        token = obj.get("token") or ""
    if not token:
        raise RuntimeError(f"登录失败（HTTP {status}）：{body[:160]}")
    return token


def bot_new_session(base: str, token: str) -> str:
    _, body, _ = _http(
        base + "/api/v1/chat/sessions/new",
        headers={"Authorization": "Bearer " + token},
        timeout=30,
    )
    try:
        obj = json.loads(body)
    except ValueError as e:
        raise RuntimeError(f"新建会话响应异常：{body[:160]}") from e
    data = obj.get("data") if isinstance(obj, dict) else None
    if isinstance(data, str):
        return data
    if isinstance(data, dict):
        for key in ("session_id", "id", "sessionId"):
            if data.get(key):
                return str(data[key])
    raise RuntimeError(f"新建会话失败：{body[:160]}")


def _extract_reply_from_json(obj) -> str:
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        if isinstance(obj.get("data"), (dict, str, list)):
            inner = _extract_reply_from_json(obj["data"])
            if inner:
                return inner
        for key in ("text", "content", "message", "reason", "msg"):
            val = obj.get(key)
            if isinstance(val, str) and val:
                return val
        if isinstance(obj.get("content_parts"), list):
            parts = [p for p in obj["content_parts"] if isinstance(p, str)]
            return "".join(parts)
    if isinstance(obj, list):
        return "".join(_extract_reply_from_json(x) for x in obj)
    return ""


def parse_bot_reply(body: str, content_type: str) -> str:
    """兼容两种回包：普通 JSON 或 SSE（data: 行流）。"""
    stripped = body.lstrip()
    if "json" in content_type and not stripped.startswith("data:"):
        try:
            return _extract_reply_from_json(json.loads(body))
        except ValueError:
            return body[:2000]
    parts: list[str] = []
    for line in body.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except ValueError:
            parts.append(payload)
            continue
        piece = _extract_reply_from_json(obj)
        if piece:
            parts.append(piece)
    return "\n".join(parts)[:4000]


def bot_send(base: str, token: str, session_id: str, message: str) -> str:
    _, body, ctype = _http(
        base + "/api/v1/chat",
        method="POST",
        json_body={"session_id": session_id, "message": message, "stream": False},
        headers={
            "Authorization": "Bearer " + token,
            "Accept": "text/event-stream",
        },
        timeout=120,
    )
    return parse_bot_reply(body, ctype)


_BALANCE_RE = re.compile(r"\d+(?:\.\d+)?\s*(?:度|元)")


def classify_reply(text: str) -> str:
    """ok=成功 / expired=会话仍被拒 / unknown=结果不确定 / rejected=会话类型不对。"""
    if _BALANCE_RE.search(text) or "凭证已保存" in text:
        return "ok"
    if "91001" in text or "已失效" in text or "会话已超时" in text:
        return "expired"
    if "私聊" in text or "群聊" in text:
        return "rejected"
    if "不可用" in text or "未取到" in text:
        return "unknown"
    return "unknown"


def _push_and_report(args: argparse.Namespace, value: str) -> int:
    """登录机器人 → 新会话 → 发 /电费 凭证，按回复分类给出结论。"""
    base = args.bot_url.rstrip("/")
    print("④ 推送给机器人并验证…")
    password = os.environ.get("ASTRBOT_PASS") or getpass.getpass(
        f"AstrBot({args.bot_user}) 密码: "
    )
    try:
        token = bot_login(base, args.bot_user, password)
        session_id = bot_new_session(base, token)
        reply = bot_send(base, token, session_id, f"/电费 凭证 {args.name}={value}")
    except (OSError, RuntimeError, ValueError) as e:
        print(f"✗ 推送失败：{e}", file=sys.stderr)
        return 3

    print("\n—— 机器人回复 ——")
    print(reply or "（空回复）")
    verdict = classify_reply(reply)
    if verdict == "ok":
        print("\n✅ 凭证已更新，余额查询成功。收工。")
        return 0
    if verdict == "expired":
        print(
            "\n✗ 学校仍拒绝（91001）：这个会话没铸成功。"
            "请在企业微信里重新打开一次缴费页（看到余额），再跑一遍。",
            file=sys.stderr,
        )
        return 1
    if verdict == "rejected":
        print("\n✗ 机器人拒绝了请求（会话类型/权限问题），详见上面回复。", file=sys.stderr)
        return 1
    print(
        "\n⚠ 结果不确定（学校可能正抖）。稍后可私聊发 /电费 检查 复核。",
        file=sys.stderr,
    )
    return 2


def _run_oneclick(args: argparse.Namespace, wxwork_dir: Path) -> int:
    host = args.host or DEFAULT_HOST_FILTER
    records = collect(wxwork_dir, host, args.name)
    seen = list(dict.fromkeys(r["value"] for r in records))

    print("① 检查库内现有凭证…")
    value = ""
    for v in seen:
        try:
            if probe_alive(v, args.aid, args.school_base):
                print("  ✓ 已有有效会话，直接使用（无需打开企业微信）")
                value = v
                break
        except RuntimeError as e:
            print(f"  ! {e}", file=sys.stderr)
            return 3
    if not value:
        print("  库里没有活会话，需要铸一个新会话")

    if not value and not args.no_auto_open:
        print("② 启动/唤起企业微信…")
        ensure_wxwork_running(args.wxwork_exe)
        activate_wxwork_window()
        if args.try_verbs:
            print("③ 实验项：尝试 wxwork:// 协议动词打开缴费页…")
            encoded = urllib.parse.quote(args.pay_url, safe="")
            for tpl in VERB_TEMPLATES:
                verb_url = tpl.format(url=encoded)
                print(f"   -> {verb_url[:76]}")
                try:
                    os.startfile(verb_url)
                except OSError as e:
                    print(f"   协议调用失败：{e}")
                    continue
                value = wait_new_cookie(args, seen, VERB_WAIT_SECONDS)
                if value:
                    break
                print("   没等到新凭证，换下一个动词")

    if not value:
        if args.no_auto_open:
            print("② 请在 PC 企业微信里打开「校园一卡通 / 缴电费」页面（看到余额即可）")
        else:
            print("③ 请在企业微信里点开「校园一卡通 / 缴电费」页面（看到余额即可）")
        print(
            f"   本脚本会自动检测新凭证并继续（最多等 {args.manual_wait} 秒）…"
        )
        value = wait_new_cookie(args, seen, args.manual_wait)

    if not value:
        print("✗ 超时：没捕获到新凭证。确认缴费页能看到余额后重跑本脚本。", file=sys.stderr)
        return 1

    if not args.push:
        print(f"⑤ 捕获到凭证：{args.name}={value}")
        print("（--push 可把它直接推给机器人）")
        return 0

    return _push_and_report(args, value)


def _run_auto(args: argparse.Namespace, wxwork_dir: Path) -> int:
    """全自动模式：带调试口重启企业微信 → 用户点一次缴费页 → CDP/明文库捕获 → 推送。"""
    host = args.host or DEFAULT_HOST_FILTER

    print("① 结束现有企业微信进程，快照 Cookie 库…")
    _kill_wxwork()
    time.sleep(2.0)
    snaps = _snapshot_cookie_dbs(wxwork_dir)
    seen: list[str] = _scan_snapshots(snaps, host, args.name)
    print(f"   快照 {len(snaps)} 个库，已知旧值 {len(seen)} 个")

    exe = find_wxwork_exe(args.wxwork_exe)
    if exe is None:
        print("✗ 未找到 WXWork.exe（--wxwork-exe 可指定路径）。", file=sys.stderr)
        return 2
    print(f"② 带调试口重启企业微信（{exe}）…")
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0
    )
    try:
        subprocess.Popen(
            [str(exe), *WXWORK_DEBUG_ARGS],
            creationflags=flags,
            cwd=str(exe.parent),
        )
    except OSError as e:
        print(f"✗ 启动失败：{e}", file=sys.stderr)
        return 2
    if not _cdp_wait(AUTO_CDP_BOOT_SECONDS):
        print(
            "✗ 调试口没就绪。若弹出登录窗请先登录（登录态通常保留，一般不用扫码）。",
            file=sys.stderr,
        )
        return 1
    print(f"   调试口就绪：127.0.0.1:{CDP_PORT}")

    print("③ 请在企业微信里点开「网上缴学杂费」（或 办事大厅→校园一卡通）进到缴费页")
    print("   —— 这是唯一需要动手的一步，看到余额即可，其余全自动 ——")
    ports: set[int] = {CDP_PORT}
    value = _capture_new_value(args, wxwork_dir, seen, args.manual_wait, ports)

    if not value:
        print(
            "✗ 超时：没捕获到新凭证。\n"
            "  排查：① 缴费页是否真的打开并看到余额；② 若缺依赖先 "
            "pip install websocket-client；③ 学校服务器可能正抖（过会重试）。",
            file=sys.stderr,
        )
        return 1

    if not args.push:
        print(f"④ 捕获到凭证：{args.name}={value}")
        print("（--push 可把它直接推给机器人）")
        return 0
    return _push_and_report(args, value)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="从企业微信 CEF Cookie 缓存提取 JSESSIONID（无需抓包）",
    )
    parser.add_argument(
        "--wxwork-dir",
        default=str(Path.home() / "Documents" / "WXWork"),
        help="企业微信数据目录（默认 %%USERPROFILE%%\\Documents\\WXWork）",
    )
    parser.add_argument(
        "--host", default="", help="只显示 host_key 含该子串的 cookie（默认不过滤）"
    )
    parser.add_argument(
        "--name", default=COOKIE_NAME, help=f"cookie 名称（默认 {COOKIE_NAME}）"
    )
    parser.add_argument(
        "--ready", action="store_true", help="只输出可直接粘贴的 cookie 值，不带其他信息"
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="旧一条龙：只盯本地 Cookie 库文件（5.0.11 起缴费 Cookie 进加密库，基本失效）",
    )
    parser.add_argument(
        "--auto",
        action="store_true",
        help="全自动：带调试口重启企业微信，用户只需点开一次缴费页，CDP 捕获会话"
        "（需 pip install websocket-client）",
    )
    parser.add_argument(
        "--push",
        action="store_true",
        help="把（新）凭证自动推送给机器人验证（密码走 ASTRBOT_PASS 环境变量或提示输入）",
    )
    parser.add_argument("--bot-url", default=DEFAULT_BOT_URL, help="AstrBot 地址")
    parser.add_argument("--bot-user", default=DEFAULT_BOT_USER, help="AstrBot 账号")
    parser.add_argument(
        "--school-base", default=DEFAULT_SCHOOL_BASE, help="学校缴费系统地址（验活用）"
    )
    parser.add_argument(
        "--aid", default=DEFAULT_AID, help="验活探测用的缴费项目 aid"
    )
    parser.add_argument(
        "--pay-url", default=DEFAULT_PAY_URL, help="协议动词要打开的缴费页入口地址"
    )
    parser.add_argument(
        "--wxwork-exe", default="", help="WXWork.exe 路径（默认自动探测）"
    )
    parser.add_argument(
        "--try-verbs",
        action="store_true",
        help="实验项：尝试 wxwork:// 协议动词自动打开缴费页（5.0.11 实测弹「打不开此链接」）",
    )
    parser.add_argument(
        "--no-auto-open",
        action="store_true",
        help="跳过启动企业微信，直接进入人工点开等待",
    )
    parser.add_argument(
        "--manual-wait",
        type=int,
        default=MANUAL_WAIT_SECONDS,
        help="人工点开缴费页的等待秒数（默认 120）",
    )
    args = parser.parse_args(argv)

    _out()
    wxwork_dir = Path(args.wxwork_dir).expanduser()
    if not wxwork_dir.is_dir():
        print(f"✗ 未找到企业微信数据目录：{wxwork_dir}", file=sys.stderr)
        print("  请确认企业微信已登录过，或用 --wxwork-dir 指定路径。", file=sys.stderr)
        return 2

    if args.auto:
        try:
            return _run_auto(args, wxwork_dir)
        except KeyboardInterrupt:
            print("\n已取消。", file=sys.stderr)
            return 130

    if args.watch or args.push:
        try:
            return _run_oneclick(args, wxwork_dir)
        except KeyboardInterrupt:
            print("\n已取消。", file=sys.stderr)
            return 130

    try:
        records = collect(wxwork_dir, args.host, args.name)
    except (FileNotFoundError, ValueError, OSError) as e:
        print(f"✗ 读取失败：{e}", file=sys.stderr)
        return 2

    if args.ready:
        if not records:
            print(
                f"✗ 没有找到 {args.name}。请先在企业微信里打开一次缴费查询页面，让 Cookie 入库。",
                file=sys.stderr,
            )
            return 1
        print(f"{args.name}={records[0]['value']}")
        return 0

    print(f"企业微信数据目录：{wxwork_dir}")
    if not records:
        print(f"未找到 {args.name}。")
        print("排查建议：")
        print("  1) 先在 PC 端企业微信里成功打开一次校园一卡通 / 缴电费页面（能看到余额）")
        print("  2) 确认企业微信没有清理过 WebView 缓存（设置 → 存储空间）")
        if args.host:
            print(f"  3) 去掉 --host {args.host} 过滤再试一次，看看命中了哪些域名")
        return 1

    print(f"命中 {len(records)} 条：\n")
    for r in records:
        print(f"  域名   {r['host']}")
        print(f"  名称   {r['name']}")
        print(f"  过期   {r['expires']}")
        print(f"  来源   {r['db']}")
        print(f"  取值   {r['name']}={r['value']}")
        print()
    print("把上面「取值」整行私聊发给机器人：/电费 凭证 " + f"{records[0]['name']}={records[0]['value']}")
    print("该凭证是闲置型过期（数小时），插件的 20 分钟轮询会顺带保活；真过期重复上述步骤即可。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
