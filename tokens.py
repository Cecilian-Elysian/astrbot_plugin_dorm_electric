"""AI 写操作确认码：生成、回复匹配、待确认项文案（纯函数）。

从 main.py 平移而来；令牌 dict 的生命周期（存取、TTL 清扫、一次性消费）
仍在 main.py 的 DormElectricPlugin 上，这里只放无状态部分。
"""

import re
import secrets
import time
import unicodedata

# 待确认项的验证码位数。
CODE_LENGTH = 6

# 用户回复验证码时的严格格式：整条消息里除了标点只剩验证码。
# 这样群里有人问「481526 度电够吗」不会被误判成已同意。
CODE_ONLY_PATTERN = r"[\s，,。.!！?？:：]*{code}[\s，,。.!！?？:：]*"

# 私聊放宽：「确认 481526」「验证码是481526」也算亲手回复。
# 码是 6 位随机数且按会话隔离，私聊里没有误判对象；群聊仍只用上面的严格格式。
CODE_KEYWORD_PATTERN = r"(?:确认|验证码|码)\s*[是码:：,，\s]*{code}(?!\d)"


def generate_code() -> str:
    return f"{secrets.randbelow(900000) + 100000:0{CODE_LENGTH}d}"


def match_code_reply(text: str, code: str, private: bool) -> bool:
    """判断消息是否为「用户亲手回复了验证码」。

    裸码全场景生效；关键词形式（确认/验证码/码 + 码）仅私聊。
    先做 NFKC 归一，全角数字「４８１５２６」也能识别。
    """
    text = unicodedata.normalize("NFKC", str(text or "")).strip()
    if not text:
        return False
    escaped = re.escape(str(code or ""))
    if re.fullmatch(CODE_ONLY_PATTERN.format(code=escaped), text):
        return True
    return bool(
        private and re.search(CODE_KEYWORD_PATTERN.format(code=escaped), text)
    )


def pending_view_text(token: dict, ttl: int) -> str:
    """未带码时的待确认项概览文案（只对发起它的会话可见）。"""
    left = max(0, int(ttl - (time.time() - token["at"])))
    action = str(token.get("action", "bind"))
    if action == "bind":
        what = f"绑定到 {token['label']}"
    elif action == "rebind":
        prev = token.get("prev_label")
        what = f"改绑到 {token['label']}" + (f"（原 {prev}）" if prev else "")
    else:
        what = f"解绑 {token['label']}"
    return (
        f"⏳ 待确认：{what}\n"
        f"验证码：{token['code']}（剩余 {left // 60} 分 {left % 60} 秒，"
        f"{'已收到你的确认' if token['user_ok'] else '等你回复验证码'}）\n"
        f"回复这个 {CODE_LENGTH} 位数字即可完成，或发 /电费 确认 {token['code']}"
    )
