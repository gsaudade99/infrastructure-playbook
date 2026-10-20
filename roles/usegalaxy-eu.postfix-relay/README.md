# Galaxy satellite mail relay

Local Ansible role for [issue #1028](https://github.com/usegalaxy-eu/issues/issues/1028).
It owns Postfix's `main.cf` and Dovecot's `dovecot.conf` on a **dedicated host**.
Supported targets: Debian 12, Enterprise Linux 9 and Rocky Linux 10, Postfix >= 3.1 with
`randmap` and `texthash`, and Dovecot 2.3. Dovecot 2.4 is not supported.

## Deployment

Add the selected host to a `mailrelay` inventory group. Supply variables in
`group_vars/mailrelay.yml` (or an external variables file); credentials and the
IMAP password hash should come from Vault. No production inventory, credentials,
or Galaxy SMTP settings are supplied by this role.

```yaml
postfix_relay_hostname: mail-relay.example.org
postfix_relay_interfaces: [127.0.0.1, 10.0.0.20]
postfix_relay_trusted_networks: [127.0.0.0/8, 10.0.0.10/32]
postfix_relay_bounce_address: galaxy-bounces@example.org
postfix_relay_imap_password_hash: "{{ vault_mailrelay_imap_hash }}"
postfix_relay_envelope_senders:
  - galaxy-no-reply@example.org
postfix_relay_accounts:
  - name: primary
    host: smtp.example.org
    port: 587
    username: galaxy-one
    password: "{{ vault_mailrelay_primary_password }}"
    rate_delay: 600
    weight: 2
  - name: secondary
    host: smtp.example.org
    port: 587
    username: galaxy-two
    password: "{{ vault_mailrelay_secondary_password }}"
    rate_delay: 900
    weight: 1
  - name: third
    host: smtp.other.example.org
    port: 587
    username: galaxy-three
    password: "{{ vault_mailrelay_third_password }}"
    rate_delay: 1200
    weight: 1
```

Generate a hash using `doveadm pw -s SHA512-CRYPT`. The system mailbox user stays
locked; IMAP uses a separate password file with only this account. IMAP listens
on `127.0.0.1:143` (port configurable); use an SSH tunnel for remote readers.
Do not expose this unencrypted IMAP listener on a public interface.

```console
ansible-playbook mail-relay.yml --check --diff --limit <staging-host>
ansible-playbook mail-relay.yml --limit <staging-host>
```

The role does not configure a firewall or inbound SMTP TLS. Keep submission on
a private network/loopback and restrict network access to the selected clients
and the upstream bounce forwarder. Upstream SMTP uses mandatory STARTTLS with
certificate and hostname verification against the OS CA bundle. Port 465
implicit TLS is not supported by this configuration.

### Rocky Linux 10 and SELinux

Rocky 10 uses the Red Hat-family package names and CA bundle. Its packaged
Postfix 3.8 and Dovecot 2.3 use the same configuration templates as EL 9.
The role installs `postfix-lmdb` and uses LMDB for local aliases on Red Hat-family
hosts; Rocky 10 does not provide the legacy `hash` backend in its base Postfix
package. Alias databases are rebuilt when Postfix configuration changes.
On SELinux-enabled Red Hat-family hosts the role installs the SELinux management
bindings, records persistent `user_home_dir_t` / `mail_home_rw_t` mappings for the
bounce home and Maildir, and applies labels with `restorecon`. It also assigns
`pop_port_t` to the configured IMAP port and `smtp_port_t` to upstream ports.
Choose ports not already assigned to an unrelated SELinux service. SELinux is
left enabled; no broad allow-all boolean or generated policy module is used.

The controller needs `community.general`, already declared in this repository's
`requirements.yaml`. Containers with SELinux disabled skip policy management.

## Routing and pacing

`sender_dependent_default_transport_maps = randmap:{...}` selects a named
transport with an explicit upstream next hop. Each account has its own SMTP
process pool, log identity and root-only `texthash` credential table, allowing
multiple accounts on the **same** SMTP hostname without credential collisions.
The original visible From and Reply-To headers are preserved. All accounts
must be authorized to send both the configured envelope sender and visible
sender identities. The role deliberately does not rotate visible identities.

`weight` repeats a result in `randmap`; it is a statistical preference, not
round-robin distribution. Routing lookups are not guaranteed to happen once per
message, and a deferred message may select another account on retry.

`galaxy_<name>_transport_rate_delay` spaces deliveries across the entire account,
regardless of recipient domain. Each delivery contains at most one recipient.
For example, 600 seconds corresponds to roughly 144 delivery attempts per day
under continuous operation. Retries also consume delivery opportunities.

**This is pacing, not strict daily-quota enforcement.** There are no durable
quota counters, exhausted-account exclusion, or guarantees across restarts.
Use limits below provider ceilings, accounting for other applications using
these accounts. Provider 4xx responses defer mail; 5xx responses may bounce it.
Test provider behaviour before submitting a large campaign. Strict daily quotas
need a separate stateful scheduler/controller. Random selection does not provide
immediate failover when one account is unavailable.

Temporary errors remain queued until `postfix_relay_queue_lifetime` (default
10 days); expiry generates a failure notification. Size the spool for the
maximum admitted backlog and **batch campaign submission to fit the drain
capacity and queue lifetime**. Do not enqueue 170,000 messages at once when only
hundreds can be sent each day. Postfix retries an existing message; recurring
monthly warnings must be scheduled by the application.

## Bounce return path

1. Provision `postfix_relay_bounce_address` at the actual domain's mail service.
2. Configure that service to forward this recipient to the relay while
   preserving the public recipient address, or rewrite to
   `galaxy-bounces@<postfix_relay_hostname>`. Arrange routing/DNS accordingly.
   A local alias cannot receive remote bounces on its own.
3. Have the sender use this address as SMTP MAIL FROM, or list exact existing
   envelope senders in `postfix_relay_envelope_senders` for rewriting. Null
   senders used by delivery-status notifications are not rewritten. `From:`
   headers alone do not determine bounce routing.
4. Ensure **every upstream account permits the bounce address** and the domain's
   SPF/DKIM/DMARC configuration matches its actual outbound mail service.
5. Deliver an actual external bounce and verify it appears in
   `/var/lib/galaxy-bounces/Maildir/new`, then read it through IMAP.

Only the exact public bounce recipient is additionally permitted for inbound
delivery; the role does not claim authority for the rest of that address's
domain. Other forwarding destinations still require trusted submission or local
recipient status. `bounce_notice_recipient` is not used as a substitute for the
real envelope return path.

## Logs and monitoring

Rsyslog retains mail-facility records in `/var/log/galaxy-mail.log`, rotated daily
and compressed, retaining 180 rotations by default. Protect and back up these
logs according to the required audit retention. They correlate queue ID,
Message-ID, envelope sender, recipient, chosen account, timestamps and SMTP
results. `status=sent` means **the next-hop relay accepted the message**, not
proof of delivery or reading. Subjects and application notification IDs are not
automatically recorded. Tracking databases, bounce parsing and deletion decisions
remain application-level work.

`/usr/local/bin/galaxy-mail-metrics` emits Influx line protocol using
`postqueue -j`: total/active/deferred/held queue counts, oldest message age and
spool free bytes. It needs no sudo with Postfix's default `authorized_mailq_users`.
Set `postfix_relay_telegraf_enabled: true` only on a host with Telegraf already
installed. Configure alerts for deferred mail, age approaching queue expiry,
and low spool space in your monitoring system.

## Operations and staging checklist

* Run syntax checking, then apply twice and confirm the second run is unchanged.
* Run `postfix check`, `postconf -n`, `postconf -M` and `doveconf -n`.
* Test all accounts, particularly separate credentials on a shared SMTP host.
* Observe a sufficiently large sample for weighted routing and confirm pacing
  across **different recipient domains**, including after reload/restart.
* Test transient SMTP failure, permanent rejection, externally generated DSNs,
  IMAP authentication and rejection of untrusted relay attempts.
* Switch Galaxy to this relay only after staging succeeds. Reserve capacity for
  registration/password-reset mail, preferably using a separate relay or pool.
* Inspect `postqueue -p` or `postqueue -j` before any queue operation. Avoid bulk
  flushes during quota exhaustion. A reload/restart preserves queued messages.
* Before rolling back, stop campaign submission and drain or explicitly retain
  the existing queue; changing Galaxy's SMTP endpoint does not move queued mail.

The role is kept in this repository. It does not create repositories, publish
roles, change upstream account configuration or modify Galaxy settings.

### Reproducible smoke test

`tests/configure.yml` supplies three fake accounts (two on the same host) for a
**disposable** target. Apply it twice with an explicit test inventory; the second
run should report `changed=0`. Supply this repository's `roles` directory through
`ANSIBLE_ROLES_PATH` if necessary. For containers without an init system, pass
`-e postfix_relay_manage_services=false`; configuration validation still runs,
but start Postfix, Dovecot and rsyslog manually before testing delivery.

Run `tests/smoke.py` inside that target after configuration, e.g.
`python3 smoke.py 2525` if the test SMTP listener uses port 2525. The script sends
a bounce to the fixture's local mailbox, reads it through authenticated IMAP,
checks all random routing outcomes and same-host credential isolation, and runs
the queue metrics command. It never sends to the fake upstream addresses.

Validated in Debian 12 (Postfix 3.7 / Dovecot 2.3) and Rocky Linux 10
(Postfix 3.8.5 / Dovecot 2.3.21) containers, including repeat-apply idempotence
and the delivery smoke test. Rocky SELinux context and port definitions were
checked against its targeted policy offline. Enforcing-mode runtime behaviour
and systemd service startup still require validation on a Rocky VM; the test
containers have SELinux disabled and services were started manually.
The fixture does not
simulate upstream TLS/authentication, provider quotas, or delivery delays;
exercise those against a staging SMTP service before production deployment.
