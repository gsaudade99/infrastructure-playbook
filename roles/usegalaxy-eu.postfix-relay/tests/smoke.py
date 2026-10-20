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

    mapping = command("postconf", "-h", "sender_dependent_default_transport_maps")
    choices = {
        command("postmap", "-q", "sender@example.org", mapping)
        for _ in range(100)
    }
    assert choices == {
        "galaxy_first:[smtp.example.org]:587",
        "galaxy_second:[smtp.example.org]:587",
        "galaxy_third:[smtp.other.example.org]:587",
    }, choices
    for name in ("first", "second"):
        credential = command(
            "postmap", "-q", "[smtp.example.org]:587",
            f"texthash:/etc/postfix/galaxy-sasl-{name}",
        )
        assert credential == f"{name}:fake-{name}"
    assert command(
        "postmap", "-q", "sender@example.org",
        "texthash:/etc/postfix/galaxy-sender-canonical",
    ) == "bounces@example.org"
    metrics = command("/usr/local/bin/galaxy-mail-metrics")
    assert metrics.startswith("galaxy_mail_queue ")
    assert "spool_free_bytes=" in metrics
    print("PASS: SMTP -> Maildir -> IMAP, random routing, isolated credentials, sender map, metrics")


if __name__ == "__main__":
    main()
