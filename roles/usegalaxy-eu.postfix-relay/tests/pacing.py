#!/usr/bin/python3
"""Exercise delivery pacing and SMTP 451 recovery inside a disposable target.

Requires the fixture configuration, a stopped Postfix instance, and root.
Temporarily substitutes a loopback-only plaintext SMTP sink for the university
upstream; restores all overridden settings and stops Postfix on completion.
Never run against a production queue. No external email is sent.
"""

import json
import smtplib
import socketserver
import subprocess
import threading
import time


def command(*args):
    return subprocess.check_output(args, text=True).strip()


class Sink(socketserver.StreamRequestHandler):
    accepted = []
    rejected_once = False

    def handle(self):
        self.wfile.write(b"220 localhost test sink\r\n")
        recipient = None
        while line := self.rfile.readline():
            verb = line.split(b" ", 1)[0].strip().upper()
            if verb == b"RCPT":
                recipient = line.decode().strip()
                if "deferred@example.net" in recipient and not Sink.rejected_once:
                    Sink.rejected_once = True
                    self.wfile.write(b"451 4.7.0 Temporary rate limit fixture\r\n")
                    continue
            elif verb == b"DATA":
                self.wfile.write(b"354 End with dot\r\n")
                while self.rfile.readline() != b".\r\n":
                    pass
                Sink.accepted.append((time.monotonic(), recipient))
            elif verb == b"QUIT":
                self.wfile.write(b"221 Bye\r\n")
                return
            self.wfile.write(b"250 OK\r\n")


def wait_for(predicate, timeout=75):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.25)
    raise AssertionError("Timed out waiting for delivery state")


def queue():
    return [json.loads(line) for line in command("postqueue", "-j").splitlines()]


def main():
    if queue():
        raise RuntimeError("Refusing to test with a nonempty queue")
    keys = ["relayhost", "smtp_tls_security_level", "smtp_tls_wrappermode",
            "smtp_sasl_auth_enable", "master_service_disable"]
    original = {key: command("postconf", "-h", key) for key in keys}
    with socketserver.ThreadingTCPServer(("127.0.0.1", 0), Sink) as sink:
        sink.daemon_threads = True
        threading.Thread(target=sink.serve_forever, daemon=True).start()
        port = sink.server_address[1]
        started = False
        try:
            command("postconf", "-e", f"relayhost=[127.0.0.1]:{port}",
                    "smtp_tls_security_level=none", "smtp_tls_wrappermode=no",
                    "smtp_sasl_auth_enable=no", "master_service_disable=smtp/inet")
            command("postconf", "-M", "2525/inet=2525 inet n - n - - smtpd")
            command("postfix", "start")
            started = True

            def submit(recipient):
                with smtplib.SMTP("127.0.0.1", 2525) as smtp:
                    smtp.sendmail("sender@example.org", [recipient],
                                  "Subject: pacing fixture\r\n\r\nTest\r\n")

            for recipient in ("one@example.net", "two@example.com", "three@example.org"):
                submit(recipient)
            wait_for(lambda: len(Sink.accepted) == 3)
            delays = [b[0] - a[0] for a, b in zip(Sink.accepted, Sink.accepted[1:])]
            assert all(delay >= 5.5 for delay in delays), delays
            wait_for(lambda: not queue())

            submit("deferred@example.net")
            wait_for(lambda: any(message["queue_name"] == "deferred" for message in queue()))
            assert Sink.rejected_once
            command("postqueue", "-f")
            wait_for(lambda: len(Sink.accepted) == 4)
            wait_for(lambda: not queue())
            print(f"PASS: cross-domain delivery spacing {delays}; 451 retained and retried")
        finally:
            if started:
                command("postfix", "stop")
            command("postconf", "-e", *(f"{key}={value}" for key, value in original.items()))
            command("postconf", "-MX", "2525/inet")
            sink.shutdown()


if __name__ == "__main__":
    main()
