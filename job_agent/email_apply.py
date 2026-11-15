from __future__ import annotations

from dataclasses import asdict, dataclass
from email.headerregistry import Address
from email.message import EmailMessage
from email.policy import SMTP as SMTP_POLICY
from email.utils import formatdate
import hashlib
import logging
from pathlib import Path
import re
import smtplib
import ssl
import threading

from .connectors import SubmissionResult
from .models import public_url
from .resume import MAX_RESUME_BYTES


LOG = logging.getLogger(__name__)
MAILBOX = re.compile(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?\.[A-Za-z]{2,63}")
RESUME_TYPES = {
    ".pdf": ("application", "pdf"),
    ".docx": ("application", "vnd.openxmlformats-officedocument.wordprocessingml.document"),
    ".txt": ("text", "plain"),
    ".md": ("text", "markdown"),
}


def mailbox(value: str) -> str:
    value = value.strip()
    if len(value) > 254 or not MAILBOX.fullmatch(value) or ".." in value:
        raise ValueError("Enter one email address, without a display name, CC/BCC, or line breaks.")
    return value


@dataclass(frozen=True)
class MailAccount:
    host: str
    port: int
    security: str
    username: str
    sender: str

    def validate(self) -> None:
        public_url("https://" + self.host)
        if not re.fullmatch(r"[a-zA-Z0-9.-]+", self.host):
            raise ValueError("Enter your provider's public SMTP hostname, without a URL or port.")
        if (self.security, self.port) not in {("ssl", 465), ("starttls", 587)}:
            raise ValueError("Use TLS: SSL on port 465 or STARTTLS on port 587.")
        if not self.username.strip() or len(self.username) > 320 or any(ord(char) < 32 for char in self.username):
            raise ValueError("Enter your mail account's SMTP username.")
        mailbox(self.sender)


def email_body(payload: dict) -> str:
    parts = [payload["cover_letter"].strip()]
    parts.extend(question.strip() + "\n" + answer.strip() for question, answer in payload["answers"].items())
    return "\n\n".join(parts) + "\n"


def validate_email_delivery(payload: dict) -> None:
    delivery = payload.get("delivery", {})
    if delivery.get("method") != "email":
        raise ValueError("This application is not configured for email delivery.")
    recipient = mailbox(delivery.get("recipient", ""))
    subject = delivery.get("subject", "")
    if not subject.strip() or len(subject) > 200 or any(ord(char) < 32 for char in subject):
        raise ValueError("Enter an email subject of 1-200 characters, without line breaks.")
    instructions = delivery.get("instructions", "")
    if len(instructions.strip()) < 20 or len(instructions) > 12000 or recipient.casefold() not in instructions.casefold():
        raise ValueError("Paste the employer's email-application instructions, including this recipient address.")
    if delivery.get("employer_accepts_email") is not True:
        raise ValueError("Confirm that the posting explicitly accepts applications by email and all required materials are included.")
    if not payload["cover_letter"].strip():
        raise ValueError("Write the application email message before approval.")
    if any(not question.strip() or not answer.strip() for question, answer in payload["answers"].items()):
        raise ValueError("Complete or remove blank application answers before approval.")
    if any(ord(char) < 32 for char in payload["candidate"]["name"]):
        raise ValueError("The sender name cannot contain line breaks or control characters.")


class EmailSender:
    """A single-recipient mail client, not a portal form or bulk-application bot."""

    def __init__(self):
        self._account: MailAccount | None = None
        self._password = ""
        self._lock = threading.Lock()

    def _session(self) -> tuple[MailAccount | None, str]:
        with self._lock:
            return self._account, self._password

    @property
    def account(self) -> MailAccount | None:
        return self._session()[0]

    @property
    def ready(self) -> bool:
        account, password = self._session()
        return account is not None and bool(password)

    def configure(self, account: MailAccount, password: str) -> None:
        account.validate()
        if not password or any(char in password for char in "\r\n\0"):
            raise ValueError("Enter a provider-approved SMTP/app password. It is kept only for this session.")
        with self._lock:
            self._account, self._password = account, password

    def clear(self) -> None:
        with self._lock:
            self._account, self._password = None, ""

    def validate(self, payload: dict) -> None:
        self._validate_session(payload, *self._session())

    @staticmethod
    def _validate_session(payload: dict, account: MailAccount | None, password: str) -> None:
        validate_email_delivery(payload)
        if account is None or not password:
            raise ValueError("Connect your mail account for this session before approving an email application.")
        account.validate()
        if payload["delivery"].get("account") != asdict(account):
            raise ValueError("The mail account changed. Update this application's delivery settings and review it again.")
        if mailbox(payload["candidate"]["email"]).casefold() != account.sender.casefold():
            raise ValueError("The candidate email must match the connected sender address.")

    def submit(self, payload: dict, attempt_key: str) -> SubmissionResult:
        account, password = self._session()
        self._validate_session(payload, account, password)
        if account is None:
            raise ValueError("Connect a mail account before sending.")
        if not re.fullmatch(r"[a-fA-F0-9-]{36}", attempt_key):
            raise ValueError("A valid durable application attempt ID is required.")
        resume = payload["resume"]
        path = Path(resume["path"])
        with path.open("rb") as stream:
            attachment = stream.read(MAX_RESUME_BYTES + 1)
        if len(attachment) > MAX_RESUME_BYTES or hashlib.sha256(attachment).hexdigest() != resume["sha256"]:
            return SubmissionResult("needs_input", "Resume changed before send. Import it again and renew approval; nothing sent.")
        content_type = RESUME_TYPES.get(path.suffix.lower())
        if content_type is None:
            return SubmissionResult("needs_input", "Unsupported email attachment. Import a PDF, DOCX or text resume; nothing sent.")
        recipient = payload["delivery"]["recipient"]
        message_id = f"<job-agent.{attempt_key}@{account.sender.rsplit('@', 1)[1]}>"
        message = EmailMessage(policy=SMTP_POLICY)
        message["From"] = Address(display_name=payload["candidate"]["name"], addr_spec=account.sender)
        message["To"] = recipient
        message["Subject"] = payload["delivery"]["subject"]
        message["Date"] = formatdate(localtime=False)
        message["Message-ID"] = message_id
        message.set_content(email_body(payload), cte="quoted-printable")
        message.add_attachment(attachment, maintype=content_type[0], subtype=content_type[1],
                               filename=Path(resume["filename"]).name)
        encoded = message.as_bytes()
        connection = None
        data_started = False
        try:
            context = ssl.create_default_context()
            if account.security == "ssl":
                connection = smtplib.SMTP_SSL(account.host, account.port, timeout=30, context=context)
            else:
                connection = smtplib.SMTP(account.host, account.port, timeout=30)
                connection.ehlo()
                connection.starttls(context=context)
            connection.ehlo()
            connection.login(account.username, password)
            code, _ = connection.mail(account.sender)
            if code != 250:
                return SubmissionResult("rejected", f"Outgoing server refused the sender (SMTP {code}); nothing sent.")
            code, _ = connection.rcpt(recipient)
            if code not in {250, 251}:
                return SubmissionResult("rejected", f"Outgoing server refused the recipient (SMTP {code}); nothing sent.")
            data_started = True
            code, _ = connection.data(encoded)
            if code == 250:
                return SubmissionResult(
                    "sent", "Email accepted by your outgoing mail server. Employer delivery/receipt is not verified.",
                    f"SMTP 250 accepted; Message-ID: {message_id}")
            if 400 <= code <= 599:
                return SubmissionResult("rejected", f"Outgoing server rejected the email (SMTP {code}); renew approval before retrying.")
            return SubmissionResult("unknown", "Unexpected mail-server reply after sending. Check your mailbox/employer; do not resend blindly.")
        except smtplib.SMTPAuthenticationError:
            return SubmissionResult("needs_input", "Mail sign-in failed. Check your provider/app password or account policy, reconnect, then approve again. Nothing sent.")
        except smtplib.SMTPResponseException as exc:
            if 400 <= exc.smtp_code <= 599:
                return SubmissionResult("rejected", f"Mail server refused the request (SMTP {exc.smtp_code}); renew approval before retrying.")
            return self._connection_failure(data_started)
        except (smtplib.SMTPException, OSError, ssl.SSLError):
            return self._connection_failure(data_started)
        finally:
            if connection is not None:
                try:
                    connection.close()
                except OSError:
                    LOG.warning("Mail connection cleanup failed; recorded delivery outcome is unchanged.")

    @staticmethod
    def _connection_failure(data_started: bool) -> SubmissionResult:
        if data_started:
            return SubmissionResult("unknown", "Connection lost during email submission. It may have been accepted; verify with your provider/employer before any retry.")
        return SubmissionResult("needs_input", "Could not securely connect/sign in to your mail provider. Check SMTP/TLS settings or provider access policy, then approve again. Nothing sent.")
