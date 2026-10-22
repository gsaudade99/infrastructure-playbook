#!/usr/bin/python3
"""Run inside a disposable host configured by configure.yml, with services up.

SMTP must listen on localhost:25 (or the port given as the first argument).
Only example.org fixtures are used; no mail is sent to an upstream relay.
"""

import imaplib
import smtplib
import subprocess
import sys
import time


def command(*args):
    return subprocess.check_output(args, text=True).strip()


def main():
    local_mailbox = bool(command("postconf", "-h", "virtual_alias_maps"))
    if local_mailbox:
        marker = f"bounce-smoke-{time.time_ns()}"
        with smtplib.SMTP("127.0.0.1", int(sys.argv[1]) if len(sys.argv) > 1 else 25) as client:
            client.sendmail(
                "",
                ["bounces@example.org"],
                "From: MAILER-DAEMON@example.net\r\n"
                "To: bounces@example.org\r\n"
                f"Subject: {marker}\r\n\r\nDelivery failure fixture\r\n",
            )
        for _ in range(30):
            with imaplib.IMAP4("127.0.0.1", 1143) as mailbox:
                mailbox.login("galaxy-bounces", "integration-password")
                mailbox.select("INBOX")
                _, messages = mailbox.search(None, "SUBJECT", marker)
                if messages[0]:
                    break
            time.sleep(1)
        else:
            raise AssertionError("The submitted bounce did not arrive over IMAP")

    assert command("postconf", "-h", "relayhost") == "[smtp.example.org]:587"
    assert command("postconf", "-h", "sender_dependent_default_transport_maps") == ""
    assert command("postconf", "-h", "smtp_transport_rate_delay") == "6s"
    assert command("postconf", "-h", "smtp_destination_recipient_limit") == "1"
    assert command("postconf", "-h", "smtp_tls_security_level") == "secure"
    if len(sys.argv) > 2:
        assert command("postconf", "-h", "smtp_tls_wrappermode") == sys.argv[2]
    assert command("postconf", "-h", "-F", "smtp/unix/process_limit") == "1"
    assert command(
        "postmap", "-q", "[smtp.example.org]:587", "texthash:/etc/postfix/galaxy-sasl",
    ) == "galaxy:fake-password"
    assert command(
        "postmap", "-q", "sender@example.org",
        "texthash:/etc/postfix/galaxy-sender-canonical",
    ) == "bounces@example.org"
    metrics = command("/usr/local/bin/galaxy-mail-metrics")
    assert metrics.startswith("galaxy_mail_queue ")
    assert "spool_free_bytes=" in metrics
    print("PASS: single relay, pacing configuration, credentials, sender map, metrics")
    if local_mailbox:
        print("PASS: SMTP -> Maildir -> authenticated IMAP")


if __name__ == "__main__":
    main()
