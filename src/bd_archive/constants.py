import re

MiB = 1024 * 1024

# Refuse to burn if disc capacity exceeds staging size by more than this
# factor — guards against wasting a larger disc on a smaller archive
# (e.g. a 50 GB BD-DL when the archive was sized for 25 GB BD-R).
DISC_OVERSIZE_TOLERANCE = 1.05

# Per-disc overhead beyond slice + par2 recovery: par2 index file, par2
# packet headers, par2 block rounding, sha512 hash files, README. The
# isolated dar catalog (usually the dominant item) scales with file
# count and is computed separately by scan_source.
PAR2_AND_MISC_OVERHEAD = 4 * MiB

# Tiny extra margin between the sizing target and the format-aware
# writable capacity, to absorb ISO9660+UDF metadata growth that exceeds
# the slice estimate. The hard limit remains the ISO file size check
# against raw_capacity post-build.
DISC_END_MARGIN = 1 * MiB

# Seconds to wait for a freshly burned disc to become mountable before
# giving up (drive needs to finalise + re-read TOC).
POST_BURN_MOUNT_TIMEOUT = 60

# ISO9660 caps the Primary Volume Descriptor's Volume Identifier at 32
# bytes. mkisofs/growisofs reject longer labels outright. Volume labels
# here are "<archive_name>_NNNN", so archive_name must leave room for
# the 5-char disc suffix.
ISO9660_VOLUME_LABEL_MAX = 32

# PAR2 recovery volumes are named "<base>.volNNN+NN.par2"; the index file
# is plain "<base>.par2". This pattern matches recovery volumes only.
PAR2_RECOVERY_RE = re.compile(r"\.vol\d+\+\d+\.par2$")
