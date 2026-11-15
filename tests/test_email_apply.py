from dataclasses import asdict, replace
from email import policy
from email.parser import BytesParser
from pathlib import Path
import smtplib
import ssl
from unittest.mock import patch

from job_agent.email_apply import EmailSender, MailAccount, email_body
from job_agent.models import digest
from job_agent.service import Applications
from tests.test_agent import StorageCase, job


ACCOUNT = MailAccount("smtp.example.invalid", 465, "ssl", "candidate@example.invalid", "candidate@example.invalid")


class FakeSMTP:
    def __init__(self):
        self.operations = []
        self.message = None
        self.error_at = ""
        self.on_data = None
        self.response = 250

    def ehlo(self):
        self.operations.append("ehlo")
        return 250, b"synthetic EHLO"

    def starttls(self, *, context):
        self.operations.append("starttls")
        if self.error_at == "tls":
            raise ssl.SSLError("synthetic TLS failure")
        return 220, b"synthetic TLS"

    def login(self, username, password):
        self.operations.append(("login", username))
        if self.error_at == "login":
            raise smtplib.SMTPAuthenticationError(535, b"synthetic denied")
        return 235, b"synthetic login"

    def mail(self, sender):
        self.operations.append(("from", sender))
        return 250, b"synthetic sender accepted"

    def rcpt(self, recipient):
        self.operations.append(("to", recipient))
        return (550 if self.error_at == "recipient" else 250), b"synthetic recipient response"

    def data(self, message):
        self.operations.append("data")
        self.message = message
        if self.on_data:
            self.on_data()
        if self.error_at == "data":
            raise TimeoutError("synthetic timeout after body")
        return self.response, b"synthetic queued"

    def close(self):
        self.operations.append("close")


class EmailApplicationTests(StorageCase):
    def setUp(self):
        super().setUp()
        self.mailer = EmailSender()
        self.mailer.configure(ACCOUNT, "synthetic-session-only-password")
        self.apps = Applications(self.store, email_sender=self.mailer)
        self.smtp = FakeSMTP()
        self.ssl_patch = patch("job_agent.email_apply.smtplib.SMTP_SSL", return_value=self.smtp)
        self.ssl_factory = self.ssl_patch.start()
        self.addCleanup(self.ssl_patch.stop)
        self.tls_patch = patch("job_agent.email_apply.smtplib.SMTP", return_value=self.smtp)
        self.tls_factory = self.tls_patch.start()
        self.addCleanup(self.tls_patch.stop)

    def delivery(self, application_id, **changes):
        values = dict(email=True, account=ACCOUNT, recipient="jobs@example.invalid",
                      subject="Synthetic application - never sent to a real server",
                      instructions="To apply for this synthetic role, email your resume to jobs@example.invalid.",
                      employer_accepts_email=True)
        values.update(changes)
        self.apps.set_delivery(application_id, **values)

    def ready(self, posting=None):
        application_id = self.prepare(posting)
        self.delivery(application_id)
        return application_id

    def test_exact_approved_email_and_resume_are_sent_once_with_tls(self):
        application_id = self.ready()
        self.edit(application_id, cover_letter="Synthetic reviewed email body.",
                  answers={"Authorization": "Synthetic truthful answer"})
        self.approve(application_id)
        approved = self.apps.get(application_id)["payload"]

        def verify_durable_attempt():
            saved = self.apps.get(application_id)
            self.assertEqual("submitting", saved["state"])
            self.assertTrue(saved["attempt_key"])
        self.smtp.on_data = verify_durable_attempt
        state, message = self.apps.dispatch(application_id)
        self.assertEqual("email_sent", state)
        self.assertIn("not verified", message)
        context = self.ssl_factory.call_args.kwargs["context"]
        self.assertTrue(context.check_hostname)
        self.assertEqual(ssl.CERT_REQUIRED, context.verify_mode)
        email = BytesParser(policy=policy.default).parsebytes(self.smtp.message)
        self.assertEqual(approved["delivery"]["recipient"], email["To"])
        self.assertEqual(approved["delivery"]["subject"], email["Subject"])
        self.assertEqual(ACCOUNT.sender, email["From"].addresses[0].addr_spec)
        self.assertEqual(email_body(approved).replace("\r\n", "\n"),
                         email.get_body(preferencelist=("plain",)).get_content().replace("\r\n", "\n"))
        attachment = list(email.iter_attachments())[0]
        self.assertEqual(approved["resume"]["filename"], attachment.get_filename())
        self.assertEqual(Path(approved["resume"]["path"]).read_bytes(), attachment.get_payload(decode=True))
        self.assertNotIn("synthetic-session-only-password", self.smtp.message.decode())
        with self.assertRaises(ValueError):
            self.apps.dispatch(application_id)
        self.assertEqual(1, self.smtp.operations.count("data"))
        self.assertEqual("sent_unconfirmed", self.apps.tracker.summary(self.apps.get(application_id))["stage"])

    def test_no_smtp_before_concrete_approval_or_with_stale_approval(self):
        application_id = self.ready()
        with self.assertRaisesRegex(ValueError, "approved"):
            self.apps.dispatch(application_id)
        reviewed = digest(self.apps.get(application_id)["payload"])
        self.delivery(application_id, recipient="other@example.invalid",
                      instructions="Apply by email to other@example.invalid with the requested resume.")
        with self.assertRaisesRegex(ValueError, "changed"):
            self.apps.approve(application_id, reviewed)
        self.ssl_factory.assert_not_called()
        self.tls_factory.assert_not_called()

    def test_international_text_round_trips_without_requiring_8bit_smtp_support(self):
        application_id = self.ready()
        self.edit(application_id, name="Synthetic Zo\u00eb", cover_letter="Synthetic r\u00e9sum\u00e9 message: \u4f60\u597d.")
        self.approve(application_id)
        self.assertEqual("email_sent", self.apps.dispatch(application_id)[0])
        self.assertTrue(self.smtp.message.isascii())
        email = BytesParser(policy=policy.default).parsebytes(self.smtp.message)
        self.assertIn("r\u00e9sum\u00e9", email.get_body(preferencelist=("plain",)).get_content())
        self.assertEqual("Synthetic Zo\u00eb", email["From"].addresses[0].display_name)

    def test_email_permission_recipient_and_required_content_are_mandatory(self):
        application_id = self.prepare()
        for changes in ({"employer_accepts_email": False}, {"instructions": "Contact the general office."},
                        {"recipient": "jobs@example.invalid,other@example.invalid"},
                        {"subject": "Synthetic\r\nBcc: other@example.invalid"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.delivery(application_id, **changes)
        self.delivery(application_id)
        self.edit(application_id, cover_letter="")
        with self.assertRaisesRegex(ValueError, "message"):
            self.approve(application_id)
        self.ssl_factory.assert_not_called()

    def test_changed_sender_account_or_resume_blocks_send(self):
        application_id = self.ready()
        self.approve(application_id)
        self.mailer.configure(replace(ACCOUNT, host="other-smtp.example.invalid"), "synthetic-password")
        with self.assertRaisesRegex(ValueError, "account changed"):
            self.apps.dispatch(application_id)
        self.ssl_factory.assert_not_called()

    def test_starttls_is_required_before_login(self):
        account = replace(ACCOUNT, port=587, security="starttls")
        self.mailer.configure(account, "synthetic-password")
        application_id = self.prepare()
        self.delivery(application_id, account=account)
        self.approve(application_id)
        self.assertEqual("email_sent", self.apps.dispatch(application_id)[0])
        self.assertLess(self.smtp.operations.index("starttls"), self.smtp.operations.index(("login", ACCOUNT.username)))
        self.ssl_factory.assert_not_called()
        self.assertTrue(self.tls_factory.called)

    def test_failed_auth_or_tls_pauses_without_sending_or_blind_retry(self):
        for failure in ("login", "tls"):
            with self.subTest(failure=failure):
                self.smtp = FakeSMTP()
                self.smtp.error_at = failure
                self.ssl_factory.return_value = self.smtp
                self.tls_factory.return_value = self.smtp
                account = replace(ACCOUNT, port=587, security="starttls")
                self.mailer.configure(account, "synthetic-password")
                application_id = self.prepare(job(source_id=failure, company=failure,
                                                   url=f"https://example.invalid/{failure}"))
                self.delivery(application_id, account=account)
                self.approve(application_id)
                self.assertEqual("needs_input", self.apps.dispatch(application_id)[0])
                self.assertIsNone(self.smtp.message)
                self.assertIsNone(self.apps.get(application_id)["approved_digest"])
                with self.assertRaises(ValueError):
                    self.apps.dispatch(application_id)

    def test_timeout_during_data_is_durable_unknown(self):
        application_id = self.ready()
        self.approve(application_id)
        self.smtp.error_at = "data"
        self.assertEqual("unknown", self.apps.dispatch(application_id)[0])
        with self.assertRaises(ValueError):
            self.apps.dispatch(application_id)
        with self.assertRaises(ValueError):
            self.approve(application_id)
        self.assertEqual("unknown", Applications(self.store).get(application_id)["state"])

    def test_recipient_or_explicit_data_refusal_is_not_reported_as_sent(self):
        application_id = self.ready()
        self.approve(application_id)
        self.smtp.error_at = "recipient"
        self.assertEqual("rejected", self.apps.dispatch(application_id)[0])
        self.assertIsNone(self.smtp.message)
        self.smtp.error_at, self.smtp.response = "", 554
        self.approve(application_id)
        self.assertEqual("rejected", self.apps.dispatch(application_id)[0])
        self.assertEqual("", self.apps.get(application_id)["receipt"])

    def test_password_is_session_only_and_reopening_requires_authorization(self):
        application_id = self.ready()
        self.approve(application_id)
        self.store.set("email_account", asdict(ACCOUNT))
        with self.store.connect() as db:
            saved = "\n".join(row[0] for row in db.execute("SELECT payload FROM applications"))
            saved += "\n".join(row[0] for row in db.execute("SELECT value FROM metadata"))
        self.assertNotIn("synthetic-session-only-password", saved)
        self.mailer.clear()
        with self.assertRaisesRegex(ValueError, "mail account"):
            self.apps.dispatch(application_id)
        self.ssl_factory.assert_not_called()

    def test_closed_listing_or_failed_supported_status_check_blocks_email(self):
        application_id = self.ready(job(source="jobicy", source_id="123"))
        self.approve(application_id)
        from job_agent.connectors import FeedError
        with patch("job_agent.tracking.Jobicy.check_status", side_effect=FeedError("Synthetic offline")):
            state, _ = self.apps.dispatch(application_id)
        self.assertEqual("needs_input", state)
        self.ssl_factory.assert_not_called()
