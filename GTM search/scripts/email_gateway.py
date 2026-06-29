#!/usr/bin/env python3
from __future__ import annotations

import os
import smtplib
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path
from typing import Dict, List, Optional, Tuple


TRUE_VALUES = {"1", "true", "yes", "on"}
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_FILES = [
    PROJECT_ROOT / "data" / "smtp_profile.local.env",
    PROJECT_ROOT / "data" / "smtp_profile.env",
    PROJECT_ROOT.parent / "competitor_pr_automation" / "data" / "smtp_profile.local.env",
]


@dataclass
class SMTPConfig:
    host: str
    port: int
    from_address: str
    from_name: str
    username: str = ""
    password: str = ""
    reply_to: str = ""
    use_ssl: bool = False
    use_starttls: bool = True
    timeout_seconds: int = 30


def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in TRUE_VALUES


def load_env_file(path: Path) -> None:
    if not path.exists():
        return

    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if value[:1] == value[-1:] and value[:1] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


def load_smtp_config_from_env(default_from_name: str = "Competitor Monitor") -> Tuple[Optional[SMTPConfig], List[str]]:
    for path in DEFAULT_ENV_FILES:
        load_env_file(path)

    missing: List[str] = []
    host = os.getenv("SMTP_HOST", "").strip()
    port_raw = os.getenv("SMTP_PORT", "").strip() or "587"
    from_address = os.getenv("MAIL_FROM_ADDRESS", "").strip()
    from_name = os.getenv("MAIL_FROM_NAME", "").strip() or default_from_name
    username = os.getenv("SMTP_USERNAME", "").strip()
    password = os.getenv("SMTP_PASSWORD", "").strip()
    reply_to = os.getenv("MAIL_REPLY_TO", "").strip()

    if not host:
        missing.append("SMTP_HOST")
    if not from_address:
        missing.append("MAIL_FROM_ADDRESS")

    port = 587
    try:
        port = int(port_raw)
    except ValueError:
        missing.append("SMTP_PORT")

    if username and not password:
        missing.append("SMTP_PASSWORD")

    if missing:
        return None, missing

    use_ssl = env_bool("SMTP_USE_SSL", False)
    use_starttls = env_bool("SMTP_USE_STARTTLS", not use_ssl)
    timeout_raw = os.getenv("SMTP_TIMEOUT_SECONDS", "").strip() or "30"
    try:
        timeout_seconds = int(timeout_raw)
    except ValueError:
        timeout_seconds = 30

    return (
        SMTPConfig(
            host=host,
            port=port,
            from_address=from_address,
            from_name=from_name,
            username=username,
            password=password,
            reply_to=reply_to,
            use_ssl=use_ssl,
            use_starttls=use_starttls,
            timeout_seconds=timeout_seconds,
        ),
        [],
    )


def build_email_message(config: SMTPConfig, to_email: str, subject: str, body: str) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject.strip()
    msg["From"] = formataddr((config.from_name, config.from_address))
    msg["To"] = to_email.strip()
    if config.reply_to:
        msg["Reply-To"] = config.reply_to
    msg["Message-ID"] = make_msgid()
    msg.set_content(body.rstrip() + "\n")
    return msg


def send_via_smtp(config: SMTPConfig, msg: EmailMessage) -> Dict[str, str]:
    server = None
    try:
        if config.use_ssl:
            server = smtplib.SMTP_SSL(config.host, config.port, timeout=config.timeout_seconds)
        else:
            server = smtplib.SMTP(config.host, config.port, timeout=config.timeout_seconds)
            server.ehlo()
            if config.use_starttls:
                server.starttls()
                server.ehlo()

        if config.username:
            server.login(config.username, config.password)

        refused = server.send_message(msg)
        status = "SENT" if not refused else "ERROR"
        error_message = ""
        if refused:
            error_message = "; ".join([f"{k}:{v}" for k, v in refused.items()])
        return {
            "status": status,
            "provider_message_id": str(msg.get("Message-ID", "")),
            "error_message": error_message,
        }
    except (smtplib.SMTPException, OSError) as exc:
        return {
            "status": "ERROR",
            "provider_message_id": str(msg.get("Message-ID", "")),
            "error_message": str(exc),
        }
    finally:
        if server is not None:
            try:
                server.quit()
            except Exception:
                pass
