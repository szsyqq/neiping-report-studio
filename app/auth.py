"""访问口令：不让别人打开页面就用掉你的模型额度。

口令来源，优先级从高到低：
    1. 环境变量 REPORT_PASSWORD（临时改口令、或不想落盘时用）
    2. data/auth.json 的 scrypt 哈希（改口令后写这里）
    3. 默认 ceshi

登录成功后发一个自签 token（过期时间 + 随机串 + HMAC 签名），**服务端不存会话**，
前端放 localStorage，之后每个请求带 `Authorization: Bearer <token>`。
无状态是有意的：云端容器会重启、也可能多副本，写文件的会话读回来时有时无。

为什么用 Bearer 而不是 Cookie：GitHub Pages 上的页面调 127.0.0.1 属跨站请求，
第三方 Cookie 会被浏览器直接拦掉（SameSite=None 还要求 Secure，本机 http 不满足），
而自定义请求头只要服务端在 CORS 里放行就能带——这是唯一能在两种入口下都成立的方案。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import base64
import secrets
from datetime import datetime, timedelta
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
DATA = BASE / "data"
AUTH_JSON = DATA / "auth.json"

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
    # 旧 token 会随密钥变化自动失效，无需清理会话表


def _secret() -> bytes:
    """签发密钥：由当前口令的校验材料派生。

    不落盘、不依赖会话文件——容器可能重启、可能多副本共用同一镜像，
    存文件里的会话会时有时无（云端实测就是：登录成功，紧接着带 token 就 401）。
    改成无状态签名：密钥由口令派生，口令一改旧 token 全失效，天然满足「改密踢下线」。
    """
    env = env_password()
    if env:
        material = "env:" + env
    else:
        rec = _read(AUTH_JSON)
        material = ("file:" + str(rec.get("hash") or "") + ":" + str(rec.get("salt") or "")) \
            if rec.get("hash") else "default:" + DEFAULT_PASSWORD
    return hashlib.sha256(("neiping-report|" + material).encode()).digest()


def _sign(payload: str) -> str:
    return hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()[:32]


def issue(days: int = TOKEN_DAYS) -> dict:
    """发一个自签 token：payload 是过期时间 + 随机串，附 HMAC 签名。"""
    exp = _now() + timedelta(days=days)
    exp_s = exp.isoformat(timespec="seconds")
    payload = f"{exp_s}|{secrets.token_hex(8)}"
    token = base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=") + "." + _sign(payload)
    return {"token": token, "exp": exp_s}


def valid(token: str) -> bool:
    """校验自签 token：签名对得上、且没过期。"""
    tok = (token or "").strip()
    if "." not in tok:
        return False
    body, _, sig = tok.rpartition(".")
    try:
        payload = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)).decode()
    except (ValueError, UnicodeDecodeError):
        return False
    if not hmac.compare_digest(sig, _sign(payload)):
        return False
    exp_s = payload.split("|", 1)[0]
    return exp_s > _now().isoformat(timespec="seconds")


def revoke(token: str) -> None:
    """无状态 token 撤不掉服务端记录；前端删掉即可，改口令会让全部失效。"""
    return None


def cookie(token: str, clear: bool = False) -> str:
    """会话 Cookie。同源部署（云端）用它最稳——代理通常不动 Cookie，
    而 Authorization 头有可能被网关剥掉（云端实测过）。"""
    if clear or not token:
        return "nr_auth=; Path=/; Max-Age=0; SameSite=Lax; HttpOnly"
    return (f"nr_auth={token}; Path=/; Max-Age={TOKEN_DAYS * 86400}; "
            "SameSite=Lax; HttpOnly")


def is_default() -> bool:
    """口令还是不是默认的 ceshi（是的话登录页给个提示，也知道自己没改过）。"""
    if env_password():
        return False
    return not bool((_read(AUTH_JSON) or {}).get("hash"))
