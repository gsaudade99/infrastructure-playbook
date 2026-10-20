#!/usr/bin/python3
"""Emit Postfix queue metrics without logging recipients or message content."""

import collections
import json
import shutil
import subprocess
import time


def main():
    result = subprocess.run(
        ["/usr/sbin/postqueue", "-j"],
        check=True,
        capture_output=True,
        text=True,
        timeout=8,
    )
    counts = collections.Counter()
    oldest = 0
    now = time.time()
    for line in result.stdout.splitlines():
        message = json.loads(line)
        counts[message["queue_name"]] += 1
        oldest = max(oldest, int(max(0, now - message["arrival_time"])))
    disk = shutil.disk_usage("/var/spool/postfix")
    fields = {
        "total": sum(counts.values()),
        "active": counts["active"],
        "deferred": counts["deferred"],
        "hold": counts["hold"],
        "oldest_age_seconds": oldest,
        "spool_free_bytes": disk.free,
    }
    print("galaxy_mail_queue " + ",".join(f"{k}={v}i" for k, v in fields.items()))


if __name__ == "__main__":
    main()
