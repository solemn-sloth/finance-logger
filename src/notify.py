"""
Failure alert — plain SMTP email to yourself when a cron job dies,
using the Gmail app password in config/.env (IMAP_USER / IMAP_APP_PASSWORD;
names kept from the retired Barclaycard IMAP setup).
"""

import os
import smtplib
from email.mime.text import MIMEText
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / "config" / ".env")


def send_alert(subject: str, body: str) -> None:
    user = os.environ["IMAP_USER"]
    password = os.environ["IMAP_APP_PASSWORD"]

    msg = MIMEText(body)
    msg["Subject"] = f"[Finance Automations] {subject}"
    msg["From"] = user
    msg["To"] = user

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(user, password)
        server.send_message(msg)
