"""tools/extract_cookie.py 的单测（Windows 专属，CI 的 ubuntu 上整份跳过）。

脚本本身不随插件加载（AstrBot 只加载 main.py），但它是主人换凭证的唯一入口，
出问题时排查成本很高，因此这里钉住几处「静默退化/静默失败」的高风险行为。
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
import urllib.request
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not sys.platform.startswith("win"), reason="企业微信提取脚本仅在 Windows 可用"
)

_TOOLS = Path(__file__).resolve().parent.parent / "tools" / "extract_cookie.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("_extract_cookie", _TOOLS)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_extract_cookie"] = mod
    spec.loader.exec_module(mod)
    return mod


ec = _load_module()


# ---------------- mask_value ----------------


def test_mask_hides_tail_and_reports_length():
    shown = ec.mask_value("A" * 40)
    assert shown.startswith("AAAAAAAA")
    assert "A" * 9 not in shown
    assert "40" in shown


def test_mask_of_short_value_keeps_two_chars():
    assert ec.mask_value("abcd") == "ab…"


def test_mask_of_empty_is_placeholder():
    assert ec.mask_value("") == "(空)"


# ---------------- classify_reply ----------------


@pytest.mark.parametrize(
    ("reply", "verdict"),
    [
        ("✅ 凭证已保存，空调费 12.34 度", "ok"),
        ("空调费：12.34 度", "ok"),
        ("查询失败：retcode=91001 会话已超时", "expired"),
        ("🔒 凭证是全局会话密钥，请私聊机器人发送", "rejected"),
        ("❌ 学校接口不可用，凭证状态未知", "unknown"),
        ("", "unknown"),
    ],
)
def test_classify_reply_matrix(reply, verdict):
    assert ec.classify_reply(reply) == verdict


# ---------------- 代理绕过 ----------------


def test_opener_bypasses_system_proxy(monkeypatch):
    """开着 Clash 时 ProxyHandler 会劫持 127.0.0.1:9222，脚本必须显式绕过。

    build_opener(ProxyHandler({})) 的效果是「默认那个读系统代理的 handler 被排除，
    且空字典 ProxyHandler 不注册任何 *_open 方法」→ 彻底不走代理。
    """
    monkeypatch.setattr(
        urllib.request, "getproxies", lambda: {"http": "127.0.0.1:9999"}
    )
    default_opener = urllib.request.build_opener()
    assert any(
        isinstance(h, urllib.request.ProxyHandler) for h in default_opener.handlers
    ), "对照组：默认 opener 确实会挂系统代理"
    assert not any(
        isinstance(h, urllib.request.ProxyHandler) for h in ec._DIRECT_OPENER.handlers
    ), "脚本自己的 opener 不该有任何代理 handler"


# ---------------- 缺环境变量时交互追问 ----------------


def test_ask_returns_given_value_without_prompting(monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("不该追问"))
    assert ec._ask_bot_field("http://x:6185", "地址", "ASTRBOT_URL", "e") == "http://x:6185"
    assert capsys.readouterr().out == ""


def test_ask_prompts_and_returns_input(monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda *a: "  http://y:6185  \n")
    assert ec._ask_bot_field("", "地址", "ASTRBOT_URL", "e") == "http://y:6185"
    assert "ASTRBOT_URL" in capsys.readouterr().out


def test_ask_returns_empty_on_eof(monkeypatch):
    def _eof(*a):
        raise EOFError

    monkeypatch.setattr("builtins.input", _eof)
    assert ec._ask_bot_field("", "地址", "ASTRBOT_URL", "e") == ""


def _push_args(**kw) -> argparse.Namespace:
    base = {"bot_url": "", "bot_user": "", "name": "JSESSIONID", "host": "pay2", "no_mask": False}
    base.update(kw)
    return argparse.Namespace(**base)


def test_push_aborts_without_creds_but_keeps_value_recoverable(monkeypatch, capsys):
    """凭证只存在于本次进程：推送走不通必须把它打出来，否则用户得重来一遍
    「杀进程 → 重启企业微信 → 点页面」。"""
    monkeypatch.setattr(ec, "_ask_bot_field", lambda *a: "")
    secret = "SECRET_SESSION_VALUE_0123456789"
    assert ec._push_and_report(_push_args(), secret) == 3
    err = capsys.readouterr().err
    assert secret in err
    assert "/电费 凭证 JSESSIONID=" + secret in err
    assert "不要发到群里" in err


def test_push_abort_explains_how_to_avoid_next_time(monkeypatch, capsys):
    monkeypatch.setattr(ec, "_ask_bot_field", lambda *a: "")
    assert ec._push_and_report(_push_args(), "v") == 3
    err = capsys.readouterr().err
    for var in ("ASTRBOT_URL", "ASTRBOT_USER", "ASTRBOT_PASS"):
        assert var in err


def test_push_never_calls_bot_when_user_gives_up(monkeypatch):
    monkeypatch.setattr(ec, "_ask_bot_field", lambda *a: "")
    monkeypatch.setattr(ec, "bot_login", lambda *a: pytest.fail("不该发起登录"))
    assert ec._push_and_report(_push_args(), "v") == 3


# ---------------- 密码输入（星号掩码） ----------------


def _keys(seq: str):
    """把字符串变成逐字符的 read_char 可调用对象。"""
    it = iter(seq)
    return lambda: next(it)


def _writer():
    buf: list[str] = []
    return buf, buf.append


def test_secret_masks_every_char():
    out, write = _writer()
    assert ec._read_secret(_keys("abc123\r"), write) == "abc123"
    assert "".join(out) == "******\n"


def test_secret_backspace_erases_star_and_char():
    out, write = _writer()
    # 打 abc → 退格 → 打 xy → 回车
    assert ec._read_secret(_keys("abc\bxy\r"), write) == "abxy"
    assert out == ["*", "*", "*", "\b \b", "*", "*", "\n"]


def test_secret_backspace_on_empty_does_nothing():
    out, write = _writer()
    assert ec._read_secret(_keys("\b\ba\r"), write) == "a"
    assert out == ["*", "\n"]


def test_secret_accepts_delete_key_backspace():
    out, write = _writer()
    assert ec._read_secret(_keys("ab\x7fa\r"), write) == "aa"
    assert out == ["*", "*", "\b \b", "*", "\n"]


def test_secret_empty_submit():
    out, write = _writer()
    assert ec._read_secret(_keys("\r"), write) == ""
    assert out == ["\n"]


def test_secret_accepts_both_newline_conventions():
    _out, write = _writer()
    assert ec._read_secret(_keys("pw\n"), write) == "pw"
    out2, _ = _writer()
    assert ec._read_secret(_keys("pw\r"), out2.append) == "pw"


def test_secret_ctrl_c_aborts():
    _out, write = _writer()
    with pytest.raises(KeyboardInterrupt):
        ec._read_secret(_keys("ab\x03"), write)


def test_secret_ctrl_z_aborts():
    _out, write = _writer()
    with pytest.raises(EOFError):
        ec._read_secret(_keys("ab\x1a"), write)


@pytest.mark.parametrize("prefix", ["\x00", "\xe0"])
@pytest.mark.parametrize("code", ["H", "P", "M", "G", "O", "I"])
def test_secret_swallows_function_key_codes(prefix, code):
    """方向键/翻页键的第二个字节是可打印字符（'H'=Home、'P'=PgUp）。

    不吞掉的话按一下方向键就会凭空多出一个星号、密码末尾多个字母。
    """
    out, write = _writer()
    assert ec._read_secret(_keys(f"ab{prefix}{code}cd\r"), write) == "abcd"
    assert out == ["*", "*", "*", "*", "\n"]


def test_secret_masks_non_ascii():
    out, write = _writer()
    assert ec._read_secret(_keys("密碼\r"), write) == "密碼"
    assert "".join(out) == "**\n"


def test_secret_pasted_password_is_read_whole():
    out, write = _writer()
    pasted = "Loki8986@" + "\r"
    assert ec._read_secret(_keys(pasted), write) == "Loki8986@"
    assert out.count("*") == 9


def test_secret_ignores_other_control_chars():
    _out, write = _writer()
    assert ec._read_secret(_keys("a\x07b\tc\r"), write) == "abc"


# ---------------- 密码来源优先级 ----------------


def test_password_prefers_env(monkeypatch):
    monkeypatch.setattr(ec.os, "environ", {"ASTRBOT_PASS": "from-env"})
    monkeypatch.setattr(ec, "_prompt_password", lambda u: pytest.fail("不该追问"))
    assert ec._ask_password("qxm") == "from-env"


def test_password_uses_masked_prompt(monkeypatch, capsys):
    monkeypatch.setattr(ec.os, "environ", {})
    seen = {}

    def _fake(user):
        seen["user"] = user
        return " typed \n"

    monkeypatch.setattr(ec, "_prompt_password", _fake)
    assert ec._ask_password("qxm") == "typed"
    assert seen["user"] == "qxm"


def test_prompt_password_falls_back_without_console(monkeypatch, capsys):
    """不是控制台时退回可见 input，不能卡死。"""
    monkeypatch.setattr(ec.sys, "stdin", None)
    monkeypatch.setattr("builtins.input", lambda *a: "typed-visible")
    assert ec._prompt_password("qxm") == "typed-visible"
    assert "直接显示在屏幕上" in capsys.readouterr().out


def test_ask_password_swallows_eof(monkeypatch):
    monkeypatch.setattr(ec.os, "environ", {})

    def _eof(user):
        raise EOFError

    monkeypatch.setattr(ec, "_prompt_password", _eof)
    assert ec._ask_password("qxm") == ""


def test_ask_password_swallows_console_error(monkeypatch, capsys):
    monkeypatch.setattr(ec.os, "environ", {})

    def _boom(user):
        raise OSError("no console")

    monkeypatch.setattr(ec, "_prompt_password", _boom)
    assert ec._ask_password("qxm") == ""
    assert "星号输入不可用" in capsys.readouterr().out


def test_push_skips_when_password_missing(monkeypatch, capsys):
    monkeypatch.setattr(ec, "_ask_bot_field", lambda v, *a: v or "http://x:6185")
    monkeypatch.setattr(ec, "_ask_password", lambda user: "")
    monkeypatch.setattr(ec, "bot_login", lambda *a: pytest.fail("不该用空密码登录"))
    args = _push_args(bot_url="http://x:6185", bot_user="qxm")
    assert ec._push_and_report(args, "captured-value") == 3
    err = capsys.readouterr().err
    assert "没拿到密码" in err and "captured-value" in err


def test_push_recovers_value_when_login_fails(monkeypatch, capsys):
    monkeypatch.setattr(ec, "_ask_bot_field", lambda v, *a: v or "http://x:6185")
    monkeypatch.setattr(ec, "_ask_password", lambda user: "pw")

    def _boom(*a):
        raise RuntimeError("登录失败（HTTP 401）")

    monkeypatch.setattr(ec, "bot_login", _boom)
    args = _push_args(bot_url="http://x:6185", bot_user="qxm")
    assert ec._push_and_report(args, "captured-value") == 3
    err = capsys.readouterr().err
    assert "登录失败（HTTP 401）" in err and "captured-value" in err


# ---------------- 依赖预检 ----------------


def test_preflight_passes_when_nothing_missing(monkeypatch):
    monkeypatch.setattr(ec, "_missing_deps", list)
    assert ec._preflight(require_cdp=True) is True


def test_preflight_fails_hard_when_websocket_missing(monkeypatch, capsys):
    monkeypatch.setattr(ec, "_missing_deps", lambda: ["websocket-client"])
    assert ec._preflight(require_cdp=True) is False
    assert "pip install" in capsys.readouterr().err


def test_preflight_tolerates_optional_crypto(monkeypatch):
    monkeypatch.setattr(ec, "_missing_deps", lambda: ["pycryptodome"])
    assert ec._preflight(require_cdp=True) is True


# ---------------- 临时快照目录 ----------------


def test_cleanup_removes_registry_even_when_no_snaps(tmp_path, monkeypatch):
    """一个库都没拷到时（5.0.11 的常态）也不能留下空的 wxsnap_* 目录。"""
    made = tmp_path / "wxsnap_abc"
    made.mkdir()
    monkeypatch.setattr(ec, "_SNAP_DIRS", [made])
    ec._cleanup_snapshots([])
    assert not made.exists()
    assert ec._SNAP_DIRS == []


def test_cleanup_removes_dir_derived_from_snaps(tmp_path, monkeypatch):
    made = tmp_path / "wxsnap_def"
    made.mkdir()
    db = made / "profile_Cookies"
    db.write_bytes(b"x")
    monkeypatch.setattr(ec, "_SNAP_DIRS", [])
    ec._cleanup_snapshots([db])
    assert not made.exists()


def test_cleanup_keeps_foreign_dirs(tmp_path, monkeypatch):
    foreign = tmp_path / "somebody-elses-snapshots"
    foreign.mkdir()
    monkeypatch.setattr(ec, "_SNAP_DIRS", [])
    ec._cleanup_snapshots([])
    assert foreign.exists()


# ---------------- --auto 的执行顺序 ----------------


def test_auto_does_not_kill_wxwork_when_exe_missing(monkeypatch, tmp_path, capsys):
    """先探测 exe 再杀进程：探测失败时企业微信必须保持原样。"""
    monkeypatch.setattr(ec, "find_wxwork_exe", lambda override="": None)
    monkeypatch.setattr(
        ec, "_kill_wxwork", lambda: pytest.fail("探测失败前不该杀企业微信")
    )
    monkeypatch.setattr(
        ec, "_snapshot_cookie_dbs", lambda d: pytest.fail("探测失败前不该快照")
    )
    args = argparse.Namespace(wxwork_exe="", host="pay2", name="JSESSIONID")
    assert ec._run_auto(args, tmp_path) == 2
    assert "WXWork.exe" in capsys.readouterr().err


def test_auto_preflight_before_kill(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(ec, "find_wxwork_exe", lambda override="": tmp_path / "WXWork.exe")
    monkeypatch.setattr(ec, "_missing_deps", lambda: ["websocket-client"])
    monkeypatch.setattr(
        ec, "_kill_wxwork", lambda: pytest.fail("依赖缺失时不该先杀企业微信")
    )
    args = argparse.Namespace(wxwork_exe="", host="pay2", name="JSESSIONID")
    assert ec._run_auto(args, tmp_path) == 2
    err = capsys.readouterr().err
    assert "websocket-client" in err and "pip install" in err


def test_auto_restores_wxwork_on_failure(monkeypatch, tmp_path):
    """捕获过程中抛异常也必须还原企业微信（finally 收尾）。"""
    exe = tmp_path / "WXWork.exe"
    exe.write_bytes(b"stub")
    restored: list[str] = []
    monkeypatch.setattr(ec, "find_wxwork_exe", lambda override="": exe)
    monkeypatch.setattr(ec, "_preflight", lambda require_cdp: True)
    monkeypatch.setattr(ec, "_kill_wxwork", lambda: None)
    monkeypatch.setattr(ec.time, "sleep", lambda *a: None)
    monkeypatch.setattr(ec, "_snapshot_cookie_dbs", lambda d: [])
    monkeypatch.setattr(ec, "_scan_snapshots", lambda *a: [])
    monkeypatch.setattr(ec.subprocess, "Popen", lambda *a, **k: None)
    monkeypatch.setattr(ec, "_restore_wxwork", lambda e: restored.append(str(e)))

    def _boom(*a, **k):
        raise RuntimeError("模拟 CDP 崩溃")

    monkeypatch.setattr(ec, "_auto_capture_and_push", _boom)
    args = argparse.Namespace(wxwork_exe="", host="pay2", name="JSESSIONID")
    with pytest.raises(RuntimeError):
        ec._run_auto(args, tmp_path)
    assert restored == [str(exe)]
