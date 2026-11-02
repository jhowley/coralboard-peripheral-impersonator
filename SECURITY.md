# Security policy

## Reporting

Please report suspected vulnerabilities privately through GitHub's
**Security → Advisories → Report a vulnerability** flow for this repository.
Do not open a public issue containing an exploit, credentials, board serials,
host addresses or raw campaign captures.

Include the affected revision, a minimal reproduction, and whether hardware is
required. This is a volunteer research project; there is no formal SLA, but
credible reports will be acknowledged and triaged.

## Scope

In scope:

- host tooling bugs that could write outside the repository, escape the
  explicit device-path/serial selection, or mislabel simulated data as hardware
  evidence;
- firmware bugs that leave the I2C target enabled past its lease, drive pins at
  boot, or report a trace as complete when it is not;
- TESTER bugs that issue a bus transaction without an explicit command, or
  report success on a short/failed transfer;
- dependency or build-input issues in the pinned SDK / `pico_i2c_slave`
  integration (report upstream as well).

Out of scope:

- physical attacks or damage requiring unsupervised wiring, 5 V on a 3.3 V
  pin, or joining power rails;
- the absence of features listed as deferred in `docs/scope-m1.md`;
- the security of a real target device that is not part of this repository;
- findings that require credentials or data not present in the repository.

## Handling of sensitive data

The repository deliberately excludes evidence archives, build outputs, logs,
board serial numbers, LAN addresses and machine paths. If you find any of those
in a commit, report it privately so it can be removed from history rather than
just deleted in a later commit.

## Supported versions

This is an experimental prototype with no released version line. Security fixes
land on the default branch; older snapshots are not maintained.
