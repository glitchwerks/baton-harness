# CodeReeve live acceptance VM: provisioning handoff

This document tells the infrastructure manager what to provision on the user's
AMD-HALO instance for the CodeReeve 0.2.0 live systemd release gate. It defines
the required guest properties and handback information; it does not prescribe a
hypervisor, Linux distribution, SSH endpoint, or provider-specific snapshot
command. Provisioning and live acceptance have not yet occurred (#396).

## Guest and resource criteria

Provision a disposable Linux guest on AMD-HALO with:

- PID 1 systemd and a usable system manager;
- a complete native `/proc` view and a cgroup v2 hierarchy rooted at
  `/sys/fs/cgroup`;
- a native local POSIX filesystem for the source checkout, virtual environments,
  managed test repository, service HOME, systemd units, and cutover journals;
- enough isolation that the guest can be restored or destroyed without touching
  any current AMD-HALO host service; and
- restorable VM snapshots that cover every disk or volume holding those paths,
  `/etc/systemd/system`, systemd enablement state, and relevant boot state.

The systemd, process, and filesystem requirements are release-gate properties,
not generic VM preferences (`docs/codereeve-release-gate.md:L240-L250`;
`src/codereeve/service_cutover/systemd.py:L211-L240`; #396). Snapshot and restore
implementation remains infrastructure-owned.

As an initial provisioning proposal, allocate **4 vCPU, 8 GiB RAM, and 60 GiB
disk**. These are planning values from the
[#396 provisioning proposal](https://github.com/glitchwerks/baton-harness/issues/396#issuecomment-5647545855)
(fetched 2026-09-10), not measured product minima. The
portable container result used 4 vCPU and 4 GiB and observed 1.677 GiB while four
modeled matrices ran, but that result does not size a real Claude agent workload
or establish a production capacity requirement (#396).

## Accounts and isolation

Provide two distinct identities:

1. An operator account that can connect over SSH and obtain root through the
   guest's approved `sudo` policy.
2. A dedicated, non-root service account, provisionally named `codereeve`, with
   an explicit home directory and no interactive session while acceptance runs.

The root coordinator must be able to inspect and control system units. The
service account must own the managed repository and run the service. During
quiescence checks, there may be no process with the service UID outside the
controlled service or bounded verification cgroups; this includes SSH and login
shells (`docs/codereeve-service-cutover.md:L67-L76`;
`src/codereeve/service_cutover/systemd.py:L567-L655`; #396).

Keep these test resources separate from all operator and production resources:

- a candidate source checkout and immutable candidate wheel;
- distinct legacy and candidate virtual environments;
- one disposable managed GitHub repository cloned to a local project root;
- a dedicated service HOME and XDG configuration selection; and
- credentials authorized only for the disposable repository.

Within the legacy-upgrade scenario, the old and candidate services deliberately
use the same test HOME, XDG selection, and project root so the cutover verifies a
real in-place service transition. Their virtual environments must remain
different (`docs/codereeve-service-cutover.md:L78-L83`;
`src/codereeve/service_cutover/systemd.py:L787-L802`; #396). None of these paths
may be shared with an existing AMD-HALO service.

## Network, tools, repository, and credentials

Allow outbound DNS, TLS, and HTTPS access needed by `git`, GitHub, the selected
GitHub App key provider, and Claude Code. Install `systemd`, `findmnt`, `procps`,
`getent`, `git`, `uv`, GitHub CLI (`gh`), and Claude Code (`claude`). Install
Bitwarden Secrets CLI (`bws`) only if the selected App-key or optional secret
configuration uses Bitwarden. The project supports Python 3.10 or newer; the
release runbook selects Python 3.13 for the candidate environment
(`pyproject.toml:L9-L19`; `docs/system-setup.md:L22-L40`;
`docs/codereeve-release-gate.md:L290-L299`).

Provision an empty, disposable managed GitHub repository that contains no open
issue eligible for CodeReeve dispatch. Do not use a real project. The acceptance
run exercises live repository and credential checks, but should not start an
unintended agent job (`docs/repository-onboarding.md:L15-L19`;
`docs/repository-onboarding.md:L32-L59`; #396).

Select and securely provision the complete test authentication set described in
`docs/authentication.md:L68-L187`: GitHub App identity and private key, the
standard worker-hook fine-grained PAT, and Claude subscription OAuth; include a
Bitwarden machine token only when the chosen provider needs it. Store secrets in
an infrastructure-managed credential location readable in the required service
context. Never put credential values, PEM data, OAuth material, private paths
that reveal secrets, or complete environment dumps in this document, a public
proof, a ticket, or chat. Hand back only the credential mechanism and the secure
location from which the release operator can retrieve the private setup details
(`docs/codereeve-release-gate.md:L571-L580`; #396).

## Snapshot sequence

The infrastructure manager must prove that each named snapshot can be restored
and must give the release operator the provider-specific restore procedure. The
operator will use this sequence:

1. **Baseline:** guest tools, accounts, source checkout, wheel, empty managed
   repository, and secure credential delivery are present; neither managed unit,
   managed state, nor either scenario environment has been seeded.
2. **Render:** restore Baseline, prepare the selected scenario, and snapshot
   immediately before `--print-unit`. Restore this snapshot after render-state
   inspection.
3. **Install-only:** from the restored Render snapshot, run `--no-start`, retain
   its evidence, and snapshot that installed-but-inactive state. Restore Render
   again before activation.
4. **Fresh:** prepare a scenario with no legacy or candidate unit or state,
   snapshot it, and use it for the full fresh-install cutover.
5. **Upgrade:** restore Baseline, seed only the supported direct legacy launcher,
   separate legacy environment, legacy unit, and fixture state, then snapshot it
   for the full upgrade cutover.
6. **Recovery:** restore the applicable Fresh or Upgrade scenario snapshot before
   every interruption attempt. After an incomplete or unexecuted attempt, restore
   it again before retrying.

This ordering prevents the render and install-only probes from contaminating the
activation scenarios and gives each recovery attempt the same starting state
(`docs/codereeve-release-gate.md:L301-L362`;
`docs/codereeve-release-gate.md:L390-L401`; #396).

## Read-only infrastructure preflight

The infrastructure manager may run the following commands after substituting the
real paths and service account. They only inspect prerequisites; they do not
install, configure, enable, start, stop, or authenticate anything.

```bash
export PROJECT_ROOT=/absolute/path/to/disposable-managed-repository
export SERVICE_HOME=/absolute/path/to/dedicated-service-home
export SERVICE_USER=codereeve

uname -a
test "$(ps -p 1 -o comm=)" = systemd
test -d /run/systemd/system
findmnt --noheadings --output SOURCE,FSTYPE,OPTIONS --target /proc
findmnt --noheadings --output SOURCE,FSTYPE,OPTIONS --target /sys/fs/cgroup
test -f /sys/fs/cgroup/cgroup.controllers
findmnt --noheadings --output SOURCE,FSTYPE,OPTIONS --target "$PROJECT_ROOT"
findmnt --noheadings --output SOURCE,FSTYPE,OPTIONS --target "$SERVICE_HOME"
getent passwd "$SERVICE_USER"
id "$SERVICE_USER"
systemctl --version
command -v sudo git uv gh claude
sudo -V
git --version
uv --version
gh --version
claude --version
uv python find 3.13
git -C "$PROJECT_ROOT" rev-parse --show-toplevel
git -C "$PROJECT_ROOT" remote -v
ps -eo pid=,uid=,user=,cgroup=,args=
```

The `/proc` and `/sys/fs/cgroup` filesystem types must report `proc` and
`cgroup2`. Review the project and HOME mount results to confirm native local
POSIX storage. The final process listing must show no unmanaged process using the
service UID when the acceptance window begins. Do not include raw `remote -v` or
process output in public evidence; retain it privately for the handoff.

## Provisioning acceptance criteria

Provisioning is ready for release acceptance only when all of these statements
are true:

- the guest is disposable, isolated on AMD-HALO, and has the required systemd,
  `/proc`, cgroup v2, and local-filesystem visibility;
- the operator SSH/sudo identity and dedicated non-root service identity work,
  and the service UID can be kept free of unmanaged processes;
- the candidate source, wheel, old and candidate environments, service HOME,
  managed repository, and credentials are isolated as described above;
- the empty managed repository has no eligible work and the provisioned
  credentials are limited to the test purpose;
- outbound access and every required tool are available;
- Baseline, Render, Install-only, Fresh, Upgrade, and Recovery restore points can
  be identified and restored, including every relevant filesystem and systemd
  state; and
- the infrastructure manager has completed the handback below without recording
  a secret value.

This handoff establishes prerequisites only. Passing release evidence still
requires the fresh, upgrade, interruption, recovery, identity, cgroup, heartbeat,
and artifact checks in `docs/codereeve-release-gate.md:L240-L587`. Modeled process
tests do not prove live systemd activation or power-loss durability
(`docs/codereeve-service-cutover.md:L90-L103`; #396).

## Infrastructure handback

Complete this form and deliver secret-bearing details through the agreed secure
channel. Leave a field marked `pending` rather than inserting a credential.

| Field | Infrastructure response |
|---|---|
| AMD-HALO guest ID/name | |
| Hypervisor/provider | |
| SSH host or alias | |
| Operator user | |
| Sudo method confirmed | |
| Service user, UID, HOME | |
| vCPU / RAM / disk allocation | |
| Linux distribution and version | |
| Kernel version | |
| systemd version | |
| cgroup mode and `/proc` visibility confirmed | |
| Project and HOME filesystem type/source | |
| Python, `uv`, `git`, `gh`, `claude`, optional `bws` versions | |
| Disposable repository owner/name and local path | |
| No eligible work confirmed at | |
| Credential mechanism/provider | |
| Secure credential handoff location | |
| Baseline snapshot ID | |
| Render snapshot ID | |
| Install-only snapshot ID | |
| Fresh snapshot ID | |
| Upgrade snapshot ID | |
| Recovery restore source(s) | |
| Restore procedure reference | |
| Restore test completed at | |

The release operator records the exact candidate commit, wheel hash, lock
identity, installed provenance, tool versions, and snapshot IDs in the final
sanitized proof. Do not hardcode an artifact digest in this provisioning handoff:
the documentation commit itself changes the candidate revision, so those values
must be captured from the final candidate used for execution
(`docs/codereeve-release-gate.md:L290-L299`;
`docs/codereeve-release-gate.md:L569-L580`; #396).
