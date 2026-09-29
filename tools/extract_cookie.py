"""从本机企业微信（WXWork）的 CEF Cookie 缓存中提取 JSESSIONID。

学校缴费系统的会话 Cookie 落在企业微信内嵌 CEF 浏览器的 SQLite Cookie 库里，
值使用 Windows DPAPI + AES-256-GCM 加密。本脚本读取该库并解密，
**无需任何抓包工具或代理中间人**。

用法：
    pip install pycryptodome
    python tools/extract_cookie.py                            # 列出全部命中
    python tools/extract_cookie.py --host pay2.hjnu.edu.cn    # 只看某域名
    python tools/extract_cookie.py --ready                    # 只输出可粘贴的值

注意：
- 本脚本只在本机运行，不会被插件加载（AstrBot 只加载 main.py），
  也不进 requirements.txt（插件运行时不依赖它）。
- 企业微信运行时会锁住 Cookies 库，此时用 esentutl 拷贝副本再读。
- 脚本自身已把 stdout/stderr 切到 UTF-8；若在 PowerShell 5.1 里看到中文乱码，
  那是控制台代码页问题，先执行 `[Console]::OutputEncoding = [System.Text.Encoding]::UTF8`。
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import json
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from ctypes import wintypes
from pathlib import Path

COOKIE_NAME = "JSESSIONID"
WALKSALT = b"DPAPI"  # Local State 里 encrypted_key 解 base64 后的固定前缀


def _out() -> None:
    """PowerShell 5.1 控制台默认 GBK，统一切 UTF-8 以免中文乱码。"""
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
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
    """企业微信的 CEF 缓存：账号缓存与 qtCef 各有一份 Cookies 库。"""
    found: list[Path] = []
    for pattern in ("*/WXWorkCefCache/Network/Cookies", "*/qtCef/Network/Cookies"):
        found.extend(sorted(wxwork_dir.glob(pattern)))
    return found


def collect(
    wxwork_dir: Path, host_filter: str = "", name: str = COOKIE_NAME
) -> list[dict]:
    """扫描全部 Cookie 库，返回解出明文的 cookie 记录列表。"""
    key = load_master_key(wxwork_dir)
    records: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="wxcookie_") as tmp:
        for db in find_cookie_dbs(wxwork_dir):
            readable = _copy_readable(db, Path(tmp))
            if readable is None:
                print(f"  ! 读取失败（文件被占用且 esentutl 绕锁失败）：{db}", file=sys.stderr)
                continue
            try:
                conn = sqlite3.connect(f"file:{readable.as_posix()}?mode=ro", uri=True)
            except sqlite3.Error as e:
                print(f"  ! 打开失败：{db}（{e}）", file=sys.stderr)
                continue
            try:
                rows = conn.execute(
                    "SELECT host_key, name, value, encrypted_value, expires_utc "
                    "FROM cookies WHERE name = ?",
                    (name,),
                ).fetchall()
            except sqlite3.Error as e:
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
    args = parser.parse_args(argv)

    _out()
    wxwork_dir = Path(args.wxwork_dir).expanduser()
    if not wxwork_dir.is_dir():
        print(f"✗ 未找到企业微信数据目录：{wxwork_dir}", file=sys.stderr)
        print("  请确认企业微信已登录过，或用 --wxwork-dir 指定路径。", file=sys.stderr)
        return 2

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
