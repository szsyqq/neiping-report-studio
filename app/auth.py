"""访问口令：不让别人打开页面就用掉你的模型额度。

口令来源，优先级从高到低：
    1. 环境变量 REPORT_PASSWORD（临时改口令、或不想落盘时用）
    2. data/auth.json 的 scrypt 哈希（改口令后写这里）
    3. 默认 ceshi

登录成功后发一个随机 token，服务端只存它的 sha256（`data/sessions.json`），
前端放 localStorage，之后每个请求带 `Authorization: Bearer <token>`。

为什么用 Bearer 而不是 Cookie：GitHub Pages 上的页面调 127.0.0.1 属跨站请求，
第三方 Cookie 会被浏览器直接拦掉（SameSite=None 还要求 Secure，本机 http 不满足），
而自定义请求头只要服务端在 CORS 里放行就能带——这是唯一能在两种入口下都成立的方案。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DATA = BASE / "data"
AUTH_JSON = DATA / "auth.json"
SESS_JSON = DATA / "sessions.json"

DEFAULT_PASSWORD = "ceshi"
TOKEN_DAYS = 30
# scrypt 参数：本机单次登录 ~50ms，够挡住批量试口令
_SCRYPT = dict(n=2 ** 14, r=8, p=1, dklen=32)


def _now() -> datetime:
    return datetime.now()


def _read(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def _hash(pw: str, salt: bytes) -> str:
    return hashlib.scrypt(pw.encode("utf-8"), salt=salt, **_SCRYPT).hex()


def env_password() -> str:
    return (os.environ.get("REPORT_PASSWORD") or "").strip()


def has_own_password() -> bool:
    """口令是不是已经被改过（不是默认值）。"""
    return bool(env_password()) or bool((_read(AUTH_JSON) or {}).get("salt"))


def check(password: str) -> bool:
    """校验口令。默认值走常量比较，改过的走 scrypt。"""
    pw = (password or "").strip()
    if not pw:
        return False
    env = env_password()
    if env:
        return hmac.compare_digest(pw.encode(), env.encode())
    rec = _read(AUTH_JSON)
    if rec.get("salt") and rec.get("hash"):
        try:
            salt = bytes.fromhex(rec["salt"])
            return hmac.compare_digest(_hash(pw, salt), rec["hash"])
        except (ValueError, TypeError):
            return False
    return hmac.compare_digest(pw.encode(), DEFAULT_PASSWORD.encode())


def set_password(password: str) -> None:
    """改口令（写 data/auth.json）。环境变量优先，设了环境变量这里就不生效。"""
    pw = (password or "").strip()
    if len(pw) < 4:
        raise ValueError("口令至少 4 位")
    salt = secrets.token_bytes(16)
    _write(AUTH_JSON, {"salt": salt.hex(), "hash": _hash(pw, salt),
                       "updated": _now().isoformat(timespec="seconds")})
    # 改口令后旧会话全部失效
    _write(SESS_JSON, {"sessions": {}})


def _digest(token: str) -> str:
    return hashlib.sha256((token or "").encode()).hexdigest()


def issue(days: int = TOKEN_DAYS) -> dict:
    """发一个新 token，返回 {token, exp}。"""
    token = secrets.token_urlsafe(32)
    exp = _now() + timedelta(days=days)
    data = _read(SESS_JSON)
    sessions = data.get("sessions") or {}
    # 顺手清掉过期与超量的旧会话
    today = _now()
    sessions = {k: v for k, v in sessions.items()
                if str(v.get("exp") or "") > today.isoformat(timespec="seconds")}
    if len(sessions) > 20:
        for k in sorted(sessions, key=lambda x: str(sessions[x].get("exp")))[:len(sessions) - 20]:
            sessions.pop(k, None)
    sessions[_digest(token)] = {"exp": exp.isoformat(timespec="seconds")}
    _write(SESS_JSON, {"sessions": sessions})
    return {"token": token, "exp": exp.isoformat(timespec="seconds")}


def valid(token: str) -> bool:
    """token 是否有效。文件不存在（从未登录过）时一律不通过。"""
    tok = (token or "").strip()
    if not tok:
        return False
    sessions = (_read(SESS_JSON).get("sessions") or {})
    rec = sessions.get(_digest(tok))
    if not rec:
        return False
    return str(rec.get("exp") or "") > _now().isoformat(timespec="seconds")


def revoke(token: str) -> None:
    sessions = (_read(SESS_JSON).get("sessions") or {})
    if sessions.pop(_digest(token or ""), None) is not None:
        _write(SESS_JSON, {"sessions": sessions})


def is_default() -> bool:
    """口令还是不是默认的 ceshi（是的话登录页给个提示，也知道自己没改过）。"""
    if env_password():
        return False
    return not bool((_read(AUTH_JSON) or {}).get("hash"))
