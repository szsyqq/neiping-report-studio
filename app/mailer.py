"""发邮件：把报告作为附件寄出去，标题默认是企业名称。

配置来源（优先级从高到低）：
    1. 环境变量 SMTP_HOST / SMTP_PORT / SMTP_USER / SMTP_PASS / SMTP_FROM / SMTP_TLS
    2. data/mail.json：
       {"host":"smtp.xxx.com","port":465,"user":"me@xxx.com",
        "password":"授权码","from":"me@xxx.com","tls":"ssl"}

tls 三档：ssl（465，直连 TLS）、starttls（587，先明文再升级）、none（25，内网中继）。

为什么要单独一个文件：smtplib 的报错是英文且分散（SMTPAuthenticationError、
SMTPServerDisconnected、socket.timeout），页面上得说人话——是哪一步卡住、
该改哪个字段。所以这里把异常翻成中文提示，并区分「连不上」「认证失败」
「被拒收」三类，用户照着改配置就行。
"""

from __future__ import annotations

import json
import mimetypes
import os
import smtplib
import socket
import ssl
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
MAIL_JSON = BASE / "data" / "mail.json"

DEFAULT_TO = "panyuping641@pingan.com.cn"
MAX_ATTACH = 25 * 1024 * 1024   # 多数企业邮箱单封 25–35MB，取保守值


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def config() -> dict:
    """读发件配置。环境变量优先，逐字段覆盖 data/mail.json。"""
    cfg: dict = {}
    if MAIL_JSON.exists():
        try:
            cfg = json.loads(MAIL_JSON.read_text(encoding="utf-8")) or {}
        except (json.JSONDecodeError, OSError):
            cfg = {}
    for key, env in (("host", "SMTP_HOST"), ("user", "SMTP_USER"),
                     ("password", "SMTP_PASS"), ("from", "SMTP_FROM"),
                     ("tls", "SMTP_TLS"), ("default_to", "SMTP_DEFAULT_TO")):
        v = _env(env)
        if v:
            cfg[key] = v
    port = _env("SMTP_PORT")
    if port.isdigit():
        cfg["port"] = int(port)
    if cfg.get("port"):
        try:
            cfg["port"] = int(cfg["port"])
        except (TypeError, ValueError):
            cfg.pop("port", None)
    cfg.setdefault("tls", "ssl")
    cfg["from"] = cfg.get("from") or cfg.get("user") or ""
    cfg["ready"] = bool(cfg.get("host") and cfg.get("user") and cfg.get("password"))
    return cfg


def save_config(patch: dict) -> dict:
    """页面里填的配置落盘。password 留空表示不改。"""
    cur = {}
    if MAIL_JSON.exists():
        try:
            cur = json.loads(MAIL_JSON.read_text(encoding="utf-8")) or {}
        except (json.JSONDecodeError, OSError):
            cur = {}
    for k in ("host", "user", "from", "tls"):
        if patch.get(k) is not None:
            cur[k] = str(patch.get(k)).strip()
    if patch.get("port"):
        cur["port"] = int(patch["port"])
    if (patch.get("password") or "").strip():
        cur["password"] = str(patch["password"]).strip()
    MAIL_JSON.parent.mkdir(parents=True, exist_ok=True)
    tmp = MAIL_JSON.with_suffix(".tmp")
    tmp.write_text(json.dumps(cur, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, MAIL_JSON)
    return config()


def describe() -> dict:
    """给页面用的配置摘要——**不回传密码**，只说有没有。"""
    cfg = config()
    return {"host": cfg.get("host") or "", "port": cfg.get("port") or "",
            "user": cfg.get("user") or "", "from": cfg.get("from") or "",
            "tls": cfg.get("tls") or "ssl", "ready": cfg["ready"],
            "has_password": bool(cfg.get("password")),
            "default_to": cfg.get("default_to") or DEFAULT_TO}


def _attach(msg: EmailMessage, path: Path) -> int:
    ctype = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    maintype, _, subtype = ctype.partition("/")
    msg.add_attachment(path.read_bytes(), maintype=maintype, subtype=subtype,
                       filename=path.name)
    return path.stat().st_size


def _split_addrs(value) -> list[str]:
    """把「a@x.com, b@y.com；c@z.com」拆成列表——页面上是一个输入框，用户就按习惯填。"""
    raw = value if isinstance(value, (list, tuple)) else [value]
    out: list[str] = []
    for item in raw:
        for part in str(item or "").replace("；", ";").replace("，", ",").split(";"):
            for addr in part.split(","):
                addr = addr.strip()
                if addr:
                    out.append(addr)
    return out


def send(to: str | list[str], subject: str, body: str, attachments: list[Path],
         cc: str | list[str] | None = None) -> dict:
    """发一封带附件的邮件。返回 {ok, to, subject, attachments, size}，失败抛 ValueError。"""
    cfg = config()
    if not cfg["ready"]:
        raise ValueError("还没配置发件邮箱：填写 SMTP 服务器、账号与授权码后即可发送")

    rcpts = _split_addrs(to)
    if not rcpts:
        raise ValueError("收件人为空")
    for addr in rcpts:
        if "@" not in addr or " " in addr:
            raise ValueError(f"收件人格式不对：{addr}")
    ccs = _split_addrs(cc)

    files = [p for p in attachments if p and p.is_file()]
    total = sum(p.stat().st_size for p in files)
    if total > MAX_ATTACH:
        raise ValueError(f"附件合计 {total / 1048576:.1f} MB，超过 {MAX_ATTACH // 1048576} MB 上限；"
                         "可先打包成 zip 或只发报告本体")

    msg = EmailMessage()
    msg["Subject"] = (subject or "").strip() or "内评简评报告"
    msg["From"] = formataddr(("内评简评报告", cfg["from"]))
    msg["To"] = ", ".join(rcpts)
    if ccs:
        msg["Cc"] = ", ".join(ccs)
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=(cfg["from"].split("@")[-1] or "localhost"))
    msg.set_content((body or "").strip() or "见附件。")

    size = 0
    for p in files:
        size += _attach(msg, p)

    host, port = cfg["host"], int(cfg.get("port") or 465)
    tls = (cfg.get("tls") or "ssl").lower()
    ctx = ssl.create_default_context()
    try:
        if tls == "ssl":
            server: smtplib.SMTP = smtplib.SMTP_SSL(host, port, timeout=30, context=ctx)
        else:
            server = smtplib.SMTP(host, port, timeout=30)
            server.ehlo()
            if tls == "starttls":
                server.starttls(context=ctx)
                server.ehlo()
    except (socket.timeout, TimeoutError):
        raise ValueError(f"连不上 {host}:{port}（超时）。端口常见填法：465 配 ssl、587 配 starttls；"
                         "若公司邮箱只在内网开放，需要连内网或换一个可外发的邮箱") from None
    except (socket.gaierror, ConnectionRefusedError, ssl.SSLError) as e:
        raise ValueError(f"连不上 {host}:{port}（{e}）。核对服务器地址与端口/加密方式") from None

    try:
        server.login(cfg["user"], cfg["password"])
    except smtplib.SMTPAuthenticationError:
        server.close()
        raise ValueError("认证失败：账号或授权码不对。企业邮箱多要求「客户端授权码」而不是登录密码") from None
    except smtplib.SMTPException as e:
        server.close()
        raise ValueError(f"登录失败：{e}") from None

    try:
        server.send_message(msg, from_addr=cfg["from"], to_addrs=rcpts + ccs)
    except smtplib.SMTPRecipientsRefused as e:
        server.close()
        detail = "；".join(f"{k}: {v[1].decode('utf-8', 'ignore') if isinstance(v[1], bytes) else v[1]}"
                          for k, v in (e.recipients or {}).items())
        raise ValueError(f"收件人被拒：{detail}") from None
    except smtplib.SMTPException as e:
        server.close()
        raise ValueError(f"发送失败：{e}") from None
    finally:
        try:
            server.quit()
        except Exception:  # noqa: BLE001 —— 已发出去，退出握手失败不算错
            pass

    return {"ok": True, "to": rcpts, "cc": ccs, "subject": msg["Subject"],
            "attachments": [p.name for p in files], "size": size + len(msg.as_bytes()),
            "host": host, "port": port, "ts": datetime.now().isoformat(timespec="seconds")}
