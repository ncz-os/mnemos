"""STYX — automated off-fleet backup of MNEMOS data to Google Drive.

STYX takes the CHARON MIF export a host can already produce, encrypts it to
an ``age`` recipient the fleet holds only the *public* half of, uploads it to
a Google Drive folder, verifies the bytes that landed, and prunes old
artifacts on a grandfather-father-son schedule.

Shape of the thing, and why
---------------------------

**Per-host, not a central puller.** Every MNEMOS host backs up its own data
on its own timer. A central puller would need network reach and credentials
to every instance, and — the deciding argument — it is the same shape as the
NFS backup job on this fleet that reported success while writing nothing for
five weeks: one process, one schedule, one place for a silent failure to
hide. Per-host runs fail visibly and independently (``systemctl
list-timers``, a non-zero exit, a per-run receipt).

**Complete, therefore encrypted.** A backup that omits the operator's own
credentials is not a backup, so STYX exports with ``include_vault=True`` and
the bundle contains live secrets before it ever reaches Drive. Client-side
encryption is not optional here: Drive's encryption-at-rest protects the
bytes from everyone except the party holding them.

**Asymmetric, with the private half off the fleet.** The disaster STYX
exists for is total fleet loss. A symmetric key would have to live on the
fleet to encrypt, and would burn with it, leaving Drive full of
undecryptable noise on exactly the day it mattered. STYX encrypts to an
``age`` X25519 *recipient*; the identity that can decrypt is generated once
by the operator and never touches a fleet host.

See ``deploy/styx/README.md`` for the key ceremony, the systemd units, and
the restore procedure.
"""

from mnemos.tools.styx.config import StyxConfig
from mnemos.tools.styx.errors import (
    StyxConfigError,
    StyxError,
    StyxIntegrityError,
    StyxUploadError,
)
from mnemos.tools.styx.runner import BackupReceipt, run_backup

__all__ = [
    "BackupReceipt",
    "StyxConfig",
    "StyxConfigError",
    "StyxError",
    "StyxIntegrityError",
    "StyxUploadError",
    "run_backup",
]
