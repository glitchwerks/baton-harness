# CodeReeve service cutover

The installer replaces `bh-daemon.service` with `codereeve.service` through
a recoverable systemd transaction. It never installs into or changes the old
virtual environment. Install a release wheel into a separate environment first:

```bash
uv venv /opt/codereeve --python 3.13
uv pip install --python /opt/codereeve/bin/python ./codereeve-*.whl
/opt/codereeve/bin/codereeve doctor --phase installation --strict
/opt/codereeve/bin/codereeve doctor --phase configuration --strict
```

The candidate must contain the complete runtime dependency closure. The staged
0.2 cutover also requires the configuration-state migration delivered by PR
#400. Keep the old environment, legacy unit, retained journals, and private
backups through the 0.3 release line.

Inspect the exact permanent unit without writing files, invoking systemd, or
reading a fresh token:

```bash
bin/install-daemon-service.sh \
  --environment /opt/codereeve \
  --project-root /srv/codereeve-project \
  --user codereeve \
  --print-unit
```

Install the reversible unit and secrets without enabling or starting it:

```bash
sudo --preserve-env=BWS_ACCESS_TOKEN \
  bin/install-daemon-service.sh \
  --environment /opt/codereeve \
  --project-root /srv/codereeve-project \
  --user codereeve \
  --no-start
```

Omit `--no-start` to perform the cutover. A BWS-backed configuration needs a
literal `BWS_ACCESS_TOKEN` only when neither selected secrets file exists. The
installer passes those bytes directly to the transaction coordinator; it does
not write the secrets file. File-provider deployments without optional BWS
secret IDs neither read nor create a BWS secrets file.

Every result prints a retained journal path. A failed transaction reports
`failed` and exits nonzero after complete rollback. An ambiguous or interrupted
restoration reports `incomplete`, leaves the services stopped, and exits
nonzero. Recover only from the reported journal:

```bash
sudo bin/install-daemon-service.sh \
  --environment /opt/codereeve \
  --recover /srv/codereeve-project/.codereeve-cutover/<transaction>/journal.jsonl
```

After a committed cutover, verify the exact live invocation:

```bash
/opt/codereeve/bin/codereeve doctor --phase installation --strict
/opt/codereeve/bin/codereeve doctor --phase configuration --strict
/opt/codereeve/bin/codereeve doctor --phase live --strict
systemctl --system --no-pager status codereeve.service
```

## Supported Linux deployment

Automatic activation requires PID 1 systemd, a system manager, complete `/proc`
visibility, and a cgroup v2 mount rooted at `/sys/fs/cgroup`. Run the coordinator
as root and run the service as a dedicated non-root account. Both known units,
their complete cgroup subtrees, systemd restart guards, and the project writer
lease must be observable. Any same-UID process outside the controlled service or
bounded verification cgroups, including a login shell, blocks automatic cutover.
Unexpected unit ownership, drop-ins, wrappers, cgroups, or external private gate
settings also block it.

The supported old launcher is a standard direct `bh-daemon` console script with
its matching virtual-environment interpreter. Shell, Python, and arbitrary
wrapper launchers require manual remediation. The old and new deployments must
use the same HOME and XDG configuration selection. Runtime outputs must remain
under the selected project `.baton-harness` or `.codereeve` state roots. A
cross-HOME move or external runtime-output path is refused before shutdown.

Successful finalization retains the manager start baseline for that invocation.
A later ordinary systemd restart without a matching persisted baseline is
conservatively reported as incomplete; recovery does not invent a start time or
silently restart a stopped finalized service.

## Disposable systemd acceptance

Use a disposable Linux VM with cgroup v2 and a dedicated throwaway account.
Install the exact wheel in a separate environment, configure a disposable local
repository, and first run `--print-unit`, then `--no-start`. Confirm that neither
mode starts or enables either unit. Seed a standard legacy unit, run the ordinary
installer, and verify that only `codereeve.service` is active, its cgroup contains
all service processes, and all three strict doctor phases pass. Finally interrupt
a disposable transaction at a recorded boundary and run `--recover` from its
journal. Destroy the VM afterward.

The automated suite uses disposable paths and injected service control. It does
not activate host services or write real `/etc` or `/run` paths, and the Windows
test run is not evidence of live Linux activation or crash durability.
