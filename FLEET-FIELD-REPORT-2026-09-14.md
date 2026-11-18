# Fleet (Swarm Phase 2) — First Live-Host Field Report

**Date:** 2026-09-14
**Commit:** `5ae9c27376d8749d3db1bec1ef01a2fe58eb7e9a` (branch `edge`)
**Controller:** macOS, conch 0.6.0
**Target host:** Ubuntu 26.04.1 LTS, x86_64, Python 3.14.4, systemd 259, cgroups v2,
user systemd manager running, no Docker, GPUs: RTX A6000 (49 GB) + RTX 5080 (16 GB)

This is the "live Linux/SSH host" verification that `README.md` / `PLAN.md` flagged as
outstanding for Swarm Phase 2. The full lifecycle was exercised end to end:

```
probe → trust-install → artifact-put ×3 → deploy → activate → worker-start → rpc
```

**Result:** the chain works and the security posture holds up. One blocking bug makes the
`systemd` profile unusable, one documented command produces a broken artifact, and two
smaller issues are worth fixing. The worker is currently running on the `process` profile.

---

## What worked well (worth keeping)

- **Fail-closed signature verification is real.** `deploy` verified the `sshsig` manifest
  signature and reported `"verified_principal": "fleet@thom"`. Digest + size checks passed.
- **Content-addressed artifact store** behaved correctly, including `"duplicate": false`
  on first put.
- **`deploy` / `activate` idempotency and receipts** worked; `activate` correctly reported
  `"previous": ""` on a first install.
- **RPC protocol validation fails closed, informatively.** A deliberately malformed request
  (`{"kind":"ping"}`) returned:
  `protocol error: RpcRequest: unknown field(s) ['kind'] — failing closed`
  with `error_class: "bug"`, `rpc_id`, `protocol_version`, `schema_version`. This is exactly
  the behavior you want, and the error message named the offending field.
- **A valid `worker.status` RPC round-tripped over SSH stdio** with `ok: true`,
  `heartbeat_seq: 193`, `incarnation: 1`, and queue limits. No worker network port involved.
- **`probe` output is genuinely useful** — arch/OS, Python, systemd features, cgroups,
  Docker, disk, available profiles, and GPU enumeration via `nvidia-smi`.
- **Bootstrap requirement is honest.** A bare host needed only `python3` plus the distro's
  venv package.

---

## Finding 1 — BLOCKER: `systemd` profile can never start (user-scope unit uses system-scope directives)

**Severity:** blocking — the `systemd` profile is 100% non-functional as written. Not
environment-specific.

**Location:** `conch/fleet/hostctl.py`, `systemd_unit_text()` (def at line 880), template
lines **935–936**:

```
CapabilityBoundingSet=
AmbientCapabilities=
```

**Why it always fails:** the unit is *always* installed and managed in **user scope** —
`systemctl --user` is hardcoded at lines 865, 1010, 1011, and 1089. An unprivileged
`systemd --user` manager cannot manipulate capabilities, so these two directives are
system-unit-only. Because the codebase never installs a system-scope unit, this template
can never succeed on any host.

**Observed:** unit enters a 5-second restart loop.

```
(python3)[13467]: conch-worker-burt-1.service: Failed to drop capabilities: Operation not permitted
(python3)[13467]: conch-worker-burt-1.service: Failed at step CAPABILITIES spawning python3: Operation not permitted
systemd[5217]: conch-worker-burt-1.service: Main process exited, code=exited, status=218/CAPABILITIES
systemd[5217]: conch-worker-burt-1.service: Failed with result 'exit-code'.
```

`worker-start` **reported success** (`{"ok": true, "already_running": false,
"profile": "systemd", "unit": "..."}`) while the unit was already failing. `worker-status`
then showed `"running": false, "pid": 0`. See Finding 4.

**Important: this cannot be fixed with a systemd drop-in.** Re-specifying
`CapabilityBoundingSet=` as empty means "drop all capabilities" — the very operation that
EPERMs. The directives must be *absent* from the rendered unit.

**Suggested fix:** gate both lines on unit scope. Since only `--user` is used today,
simplest correct change is to omit them entirely, with a comment explaining that an
unprivileged user manager already cannot grant capabilities (an unprivileged process
can't gain caps, so the hardening value is ~zero here). If system-scope units are planned,
pass a `scope` parameter into `systemd_unit_text()` and emit them only for `system`.

**Also note:** same run logged
`ProtectHostname=yes is configured, but UTS namespace setup is prohibited (container manager?), ignoring namespace setup`
— non-fatal, but it suggests other directives in the template deserve a user-scope audit
(e.g. `ProtectHostname`, `ProtectClock`, `ProtectKernelTunables` behave differently or
are ignored in user scope).

**Regression test worth adding:** render the unit for user scope and assert it contains no
`CapabilityBoundingSet`/`AmbientCapabilities`; ideally a CI job that actually
`systemd-analyze verify`s the rendered unit, or starts it under a user manager.

---

## Finding 2 — HIGH: the documented build command produces a malformed `allowed_signers`

**Severity:** high — following the README verbatim yields a broken trust anchor.

**Location:** `tools/build_worker_artifact.py` lines **61–67** (human-readable summary via
`print()` → stdout) vs. `--emit-allowed-signers` (signer line → also stdout).
Documented command at `README.md` line **687**.

README says:

```bash
python tools/build_worker_artifact.py --out dist/conch-worker.pyz \
    --sign-key ~/.config/conch/fleet/signing-key \
    --principal fleet@you --emit-allowed-signers > allowed_signers
```

**Actual contents of `allowed_signers` after running exactly that:**

```
artifact: dist/conch-worker.pyz
sha256:   13b49c61bbd83259fcbae6bc3e4645d548362f1ea1aa2be992e235adcb3f688c
size:     1747825
manifest: dist/conch-worker.pyz.manifest.json
signature: dist/conch-worker.pyz.manifest.json.sig
fleet@thom namespaces="conch-fleet-artifact" ssh-ed25519 AAAAC3Nza...
```

Five lines of build summary precede the one real signer line. That file is not a valid
OpenSSH allowed-signers file. I worked around it with
`grep -E "^fleet@" allowed_signers > allowed_signers.clean` before `trust-install`.

**Suggested fix (either):**
- send the human summary to **stderr** so `> allowed_signers` yields only the signer line
  (preferred — makes the documented command correct as written); or
- add `--allowed-signers-out PATH` and write the anchor directly to a file.

**Not verified:** whether `trust-install` would reject the malformed file or silently
install it and fail later at `deploy`. I pre-cleaned the file, so I did not exercise that
path. If it installs silently, that's a second fail-closed gap worth checking — a
validation pass in `trust-install` (assert every non-comment line parses as an
allowed-signers entry) would be cheap insurance.

---

## Finding 3 — MEDIUM: workers silently don't survive logout (`Linger=no`), never surfaced

**Severity:** medium — violates the "keeps missions running when the terminal closes"
promise, and nothing warns you.

`loginctl show-user burt` reports `Linger=no`. Without `loginctl enable-linger <user>`,
the user systemd manager is torn down at logout, so a user-scope worker (and the `process`
profile too) dies when the SSH session ends. Nothing in `probe`, `worker-start`, or
enrollment mentions this.

**Suggested fix:**
- have `probe` record linger state alongside the systemd block (it already reports systemd
  version and features, so this fits naturally);
- have `worker-start` warn (or refuse without `--force`) when `profile=systemd`/`process`
  and linger is off;
- document `loginctl enable-linger <user>` as an enrollment step. Note it typically needs
  polkit/sudo, so it belongs in the interactive enrollment flow rather than an unattended
  path.

---

## Finding 4 — MEDIUM: `worker-start` reports success for a unit that is already failing

**Severity:** medium — a lifecycle tool that returns `ok: true` for a broken deploy is
misleading, and in an automated controller loop it would mask the failure.

`worker-start --profile systemd` returned `{"ok": true, "already_running": false, ...}`
while the unit was in a restart loop (Finding 1). The immediately following
`worker-status` correctly returned `"running": false, "pid": 0`.

**Suggested fix:** after `systemctl --user enable --now`, poll briefly (~2–5 s) and confirm
the unit reached `active (running)`; on failure return `ok: false` including the
`systemctl` result and the last few journal lines. That would have made Finding 1
self-diagnosing instead of requiring a manual `journalctl` dig.

---

## Finding 5 — LOW (docs): `conch-hostctl rpc` argument form

`README.md` line **712** describes the transport as ``conch-hostctl rpc <worker>``, which
reads as a positional argument. The actual CLI requires a flag:

```
usage: conch-hostctl rpc [-h] --worker WORKER [--timeout TIMEOUT]
conch-hostctl rpc: error: the following arguments are required: --worker
```

I hit this on first use. Either accept a positional worker name or update the prose to
`conch-hostctl rpc --worker <worker>`.

**Related DX note:** constructing a valid RPC by hand requires knowing that `rpc_id` must
be a canonical `rpc-{unix_ms:013x}-{16 hex}` ID (`new_id()` in
`conch/swarm/protocol.py:138`) and that `op` must come from `RPC_OPS`
(`protocol.py:115`). A tiny `conch-hostctl rpc --op worker.status` convenience that mints
`rpc_id` itself would make the relay usable for debugging without writing a Python
one-liner first.

---

## Finding 6 — INFO: model catalog / fallback

On the deployed host, `provider = anthropic` with `model = claude-opus-5` produced:

```
conch: anthropic/claude-opus-5 failed, trying anthropic/claude-sonnet-5...
```

`claude-opus-5` is present in `conch/providers.py`'s audited catalog. This is most likely
account-level access on that particular key rather than a bad model string (the same
`model_not_found` shape appears in this machine's session history for `zai-glm-4.7`), so
it may not be a conch bug at all. Flagging only because the **fallback behaved well** —
it degraded to a working model and said so clearly, which is the right UX.

---

## Reproduction summary

```bash
# controller
ssh-keygen -t ed25519 -f ~/.config/conch/fleet/signing-key -N ""
python3 tools/build_worker_artifact.py --out dist/conch-worker.pyz \
  --sign-key ~/.config/conch/fleet/signing-key \
  --principal fleet@you --emit-allowed-signers > dist/allowed_signers
grep -E "^fleet@" dist/allowed_signers > dist/allowed_signers.clean   # Finding 2 workaround

# host (conch-hostctl from an installed conch 0.6.0)
conch-hostctl probe
conch-hostctl trust-install --op-id trust-$(date +%s)  < allowed_signers.clean
conch-hostctl artifact-put --digest <pyz sha256>       < conch-worker.pyz
conch-hostctl artifact-put --digest <manifest sha256>  < conch-worker.pyz.manifest.json
conch-hostctl artifact-put --digest <sig sha256>       < conch-worker.pyz.manifest.json.sig
conch-hostctl deploy   --worker w1 --op-id d1 --artifact-digest A --manifest-digest M \
                       --signature-digest S --profile systemd
conch-hostctl activate --worker w1 --op-id a1 --digest A --memory-max 2G --cpu-quota 100% --tasks-max 256
conch-hostctl worker-start --profile systemd --worker w1   # ok:true, but unit dies 218/CAPABILITIES
conch-hostctl worker-status --worker w1                    # running:false, pid:0
conch-hostctl worker-start --profile process --worker w1   # works
```

## Priority suggestion

1. **Finding 1** — unblocks the `systemd` profile (the intended production path).
2. **Finding 2** — one-line stdout/stderr change; makes the documented flow correct.
3. **Finding 4** — start-time verification; would have made #1 self-diagnosing.
4. **Finding 3** — linger surfacing, needed for the durability promise.
5. **Findings 5/6** — docs + DX polish.

Happy to re-run the whole lifecycle against this host once #1 lands, including a
`systemd`-profile start and a rollback test (`rollback` was not exercised — only one
revision existed).
