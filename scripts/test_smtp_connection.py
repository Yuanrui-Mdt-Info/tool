#!/usr/bin/env python3
from __future__ import annotations

import smtplib

from email_gateway import load_smtp_config_from_env


def main() -> int:
    config, missing = load_smtp_config_from_env(default_from_name="Competitor Monitor")
    if config is None:
        print("CONFIG_MISSING:" + ",".join(missing))
        return 1

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

        print(f"SMTP_OK host={config.host} port={config.port} ssl={config.use_ssl} starttls={config.use_starttls}")
        return 0
    except (smtplib.SMTPException, OSError) as exc:
        print(f"SMTP_ERROR:{exc}")
        return 2
    finally:
        if server is not None:
            try:
                server.quit()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
