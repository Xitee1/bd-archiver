# UDF multisession research and deferred implementation plan

Date: 2026-09-14. Code reviewed at `6c8a368`.

Status: **deferred by the user on 2026-09-14**. This document preserves the research,
discussion outcomes and a conditional implementation outline; it is not an active
implementation assignment. Publishing this document is authorized. No application
implementation, dependency installation, or media writes have been authorized or
performed for this investigation. The old May append plan remains superseded; its
commands and implementation snippets must not be reused.

## Final decision

Keep the current single-session ISO9660+UDF workflow. The user will collect files
on a hard drive and burn the complete batch in one operation. Existing DAR
`--pack-with` remains useful for combining unburned archive contents before burning;
it is not an append operation on an already recorded disc.

No sufficiently simple, demonstrated open-source Linux solution meeting the full
requirements was found. This is a research result, not proof that no such software
can exist. The proprietary SDK alternative does not fit this open-source tool,
and developing or extending a UDF author plus the application's media, capacity,
verification and recovery handling is too large a change for the current need.
Moving the burn step to Windows is not being pursued either.

Do not remove ISO9660 or add a proprietary dependency as a result of this research.
Revisit only if a suitable open-source Linux author becomes available, or the user
explicitly reopens the topic and accepts the development scope. The requirements
and acceptance criteria below preserve what a future solution must demonstrate.

## Decision and scope

Pure UDF is feasible as a filesystem format. UDF also specifies multisession
recording. Further research found a concrete native Linux candidate: PrimoBurner's
official Blu-ray sample combines pure UDF authoring with previous-track import and
later additions. It is a proprietary SDK with significant licensing constraints.
Windows IMAPI independently documents cumulative UDF sessions on BD-R. Neither
candidate has been tested on this project's media, Linux readers or player.
The current writer cannot simply be switched to UDF-only append. Treat the first
milestone below as a feasibility gate, not as an already validated integration.

Requirements established during the investigation, should the topic be reopened:

- UDF must expose all committed old and new files. Pure UDF is preferred, but the
  user subsequently accepted retaining ISO9660 if its metadata cost is modest.
  An ISO9660 bridge must not substitute for updating the UDF view.
- Burn part of a BD-R now and add files later, initially in raw mode.
- Linux should show all committed files together through the mounted filesystem.
- Modern Windows/macOS and Panasonic DP-UB820 playback are desirable. The earlier
  willingness to accept first-session-only playback does not authorize knowingly
  leaving UDF stale while making only ISO9660 cumulative. Player session-selection
  limitations still require hardware testing, separately from filesystem validity.
- Complete recovery must be possible; special reading and mounting procedures
  are acceptable instead of one default ddrescue invocation.

Deferred initial implementation proposal: new BD-R discs in sequential recording
mode, additive raw sessions, rejecting file-path collisions. Updating/deleting
existing files, appending to existing hybrid discs, and DAR append need separate
scope decisions. If pure UDF replaces the shared filesystem writer, creation must
cover both raw and DAR. DAR append can follow after the raw path is proven.

Removing ISO9660 does not inherently remove optional image output: an image file
can contain only UDF, even if its suffix is `.iso`. CLI/image naming is a separate
decision; this plan does not interpret the request as removing image output.

## Findings from the repository

| Area | Current behavior | Consequence |
| --- | --- | --- |
| `tools/mkisofs.py:22–35` | Always `-iso-level 3 -udf`; raw additionally requests `-R` | Both modes create hybrid filesystems; Rock Ridge is an ISO9660 extension, not a third payload copy. |
| `tools/mkisofs.py:71–80` | Documents shared payload blocks | Removing ISO9660 saves its metadata, not a duplicate set of files. |
| `tools/growisofs.py:88–95` | Always writes through `-Z` | No application-level append workflow exists. |
| `tools/mediainfo.py` | First matching Free Blocks, then Track Size fallback | Not a trustworthy multi-track append-state model. |
| `commands/create_raw.py` | Automatic PAR2 sizing uses the available capacity | A small first batch may consume the remainder as recovery data unless given a smaller session budget. |
| `archive/disc_folder.py` | Measurement and manifest depend on mkisofs and `rock_ridge` | The UDF backend needs an explicit format/measurement contract. |
| `archive/verify.py:33–55` | Raw verification selects one root recovery index | Additional payload could escape verification if this layout were merely extended. |
| `archive/disc.py`, `commands/verify.py` | General device/loop mounting | A successful mount alone does not prove the intended latest session was selected. |

The wiki agrees with the hybrid format and explicitly limits `--pack-with` to
preparing a new disc from unburned contents. Reviewed pages: Storage-and-layout,
Raw-data-discs, Incrementals-and-packing, Installation, and CLI-reference.

### Why the earlier append plan was abandoned

The opening review notes in the local planning files
`2026-05-20-multi-session-append.md` and
`2026-06-10-pack-with-foldered-layout.md`
record a stale-UDF/ISO9660-only append hazard: mounting could select the old UDF
view and verification could then validate only the old archive. They also record
incorrect session-offset ordering, track-state parsing, capacity arithmetic, and
rewritable-media regressions. Most decisively, the remainder was not burned yet,
so packing it into a new single-session disc solved the actual requirement.
Those older files are untracked local working documents, not published repository
references. Their relevant conclusions are preserved here; their implementation
snippets are intentionally not republished because the May design was rejected.

Those notes do not identify ddrescue as the recorded decisive reason. The reported
experience of seeing only the first session in an image is nevertheless consistent
with a session-discovery problem. Do not repeat the old plan's blanket statement
that no Linux tool can create any multisession UDF structures: mkudffs now documents
session-offset support. That is still not a complete file-import/append solution.

The old review also corrects the claim that `-dvd-compat` finalizes BD-R. Its mere
presence is not evidence that an existing BD-R cannot accept more data. Actual
medium state must decide that; software support for importing such discs is a
different question.

## External evidence and remaining gap

UDF 2.60 section 6.11.3 describes a latest session whose structures can reference
data and metadata in earlier sessions. Sections 6.11.2 and 6.11.3 distinguish VAT
incremental recording from session handling. Thus UDF is not inherently limited
to one burn. For BD-R without logical overwrite, UDF 2.50 is a candidate target;
choosing 2.60 merely because its number is newer is unjustified. The multisession
ban in section 6.16.4.1 concerns BDAV/BDMV application usage, not every MKV data disc.
[UDF specification](https://www.13thmonkey.org/documentation/UDF/udf260.pdf)

| Component | Evidence | Assessment |
| --- | --- | --- |
| mkisofs | Its manual explicitly says it always creates ISO9660. | Cannot author the requested pure-UDF format. [Manual](https://cdrtools.sourceforge.net/private/man/cdrecord/mkisofs.8.html) |
| growisofs 7.1 | The `dev_found == 'M'` path checks for `CD001` before accepting even an image input. | Its normal append path cannot be the pure-UDF transport. [Source](https://sources.debian.org/src/dvd%2Brw-tools/7.1-14/growisofs.c/) |
| cdrskin/libburn | Burns preformatted data and supports BD-R multisession. | Preferred transport candidate; it does not create the UDF directory tree. [Upstream](https://scdbackup.sourceforge.net/cdrskin_eng.html) |
| xorriso/libisofs | Provides ISO9660 authoring, import and multisession facilities, but no UDF author. | Does not fill the cumulative-UDF authoring gap; replacing growisofs with xorriso alone would not meet the requirement. [Upstream](https://www.gnu.org/software/xorriso/) |
| mkudffs 2.3 | Has `--startblock`, BD-R and VAT support; revisions above 2.01 are marked experimental. It creates a filesystem rather than importing a source tree plus previous extents. | Useful formatter/reference, not a demonstrated complete authoring backend. [Manual](https://manpages.debian.org/unstable/udftools/mkudffs.8.en.html) |
| Linux UDF driver | Explicitly refuses writable mounts of virtual partitions. | Mounting VAT-based BD-R read/write and copying files into it is not a solution. [Source](https://raw.githubusercontent.com/torvalds/linux/master/fs/udf/super.c) |
| UDFclient/newfs_udf | Source 0.8.21 explicitly returns an error for compiling/writing VAT; virtual-sector allocation is also unimplemented. | Not a complete VAT-based BD-R append writer. This finding is about the userland code, not NetBSD's kernel driver. [Source archive](https://www.13thmonkey.org/udfclient/releases/UDFclient.0.8.21.tgz) |

The inspected `wrudf` code has a CD-R-oriented VAT writer emitting revision 1.50;
that is not proof of a suitable modern BD-R pipeline.
[Source](https://raw.githubusercontent.com/pali/udftools/master/wrudf/wrudf-cdr.c)

The earlier network limitation was resolved by retrieving the public source into
memory. The targeted UDFclient audit found `udf.c:4629` reporting VAT write-out as
not implemented and `udf_bmap.c` rejecting virtual allocation. This was a focused
append-path inspection, not a complete audit. No physical-media or generated-image
experiments were performed in this planning task.

### Concrete alternatives found in the follow-up investigation

**PrimoBurner, native Linux:** the vendor's public C++ `samples/linux/bluray-data`
README explicitly demonstrates a first burn followed by an append. `BurnerApp.cpp`
sets `ImageTypeFlags::Udf`; `Burner.cpp` sets the new session address, finds the
previous complete track, requests its import through `setLayoutLoadTrack`, adds
the new source tree, and writes the disc. `main.cpp` leaves the disc open. This is
stronger evidence than independently advertised UDF and multisession features.
[Linux example and instructions](https://github.com/primoburner/primoburner-cpp/tree/main/samples/linux/bluray-data)
[Import/write path](https://github.com/primoburner/primoburner-cpp/blob/main/samples/linux/bluray-data/Burner.cpp#L529-L560)
[Pure UDF selection](https://github.com/primoburner/primoburner-cpp/blob/main/samples/linux/bluray-data/BurnerApp.cpp#L209-L222)

The sample selects UDF 1.02, so it is not itself a demonstrated UDF 2.50 append
test. The SDK advertises revisions through 2.60 and exposes revision selection;
2.50 remains a proposed test target. The SDK supplies Linux shared libraries for
Ubuntu 22.04 and later on amd64/aarch64. Its internals are proprietary; inspecting
the MIT-licensed sample does not amount to auditing the filesystem implementation.
[Features](https://primoburner.com/features/)

Standard commercial licensing is USD 6,000 per year, per product and platform.
There is a conditional free license for qualifying noncommercial open-source
projects, with annual renewal and restrictions including non-copyleft licensing
and project activity requirements. Eligibility and distribution terms for this
project are unconfirmed. No vendor contact or paid dependency is authorized.
[License terms](https://primoburner.com/license/)

**Windows IMAPI:** Microsoft's documentation explicitly describes UDF multisession
on sequential Blu-ray media, including a UDF 2.50 layout. Its BD-R append workflow
imports the existing filesystem before adding files; automatic import prefers UDF
on a hybrid disc. This is a concrete alternative authoring/burning API, but requires
a Windows writing environment and therefore changes the existing Linux workflow.
No Windows environment or drive passthrough has been provisioned or proposed as an
implicit dependency.
[Layout](https://learn.microsoft.com/en-us/windows/win32/imapi/imapi-multisession-layout)
[BD-R import and append workflow](https://learn.microsoft.com/en-us/windows/win32/imapi/creating-a-multisession-disc)

**Open-source Linux development:** current schilytools `mkisofs/udf.c` still sets
its partition origin at the new file-set descriptor and computes file addresses
by subtracting that origin from their stored block addresses. Imported earlier
extents therefore require a deliberate partition/addressing design. This is a
source-based diagnosis of a development obstacle, not proof of a one-line fix.
Updating a revision field alone cannot implement UDF 2.50. Extending mkisofs would
also retain its ISO9660 output unless separately changed, which is acceptable only
if UDF becomes cumulative as well.
[Current source](https://codeberg.org/schilytools/schilytools/src/branch/master/mkisofs/udf.c)

NetBSD's kernel writer is another research lead, but its manual explicitly labels
Blu-ray writing experimental. Current NetBSD makefs can create populated UDF
images including revision 2.50; that alone does not demonstrate previous-session
import. Neither is presently a proven replacement for this application's writer.
[NetBSD mount_udf](https://man.netbsd.org/mount_udf.8)
[Current makefs documentation source](https://github.com/NetBSD/src/blob/trunk/usr.sbin/makefs/makefs.8)

**Other authoring leads:** pycdlib exposes UDF image-authoring APIs, but this review
did not establish a ready optical append workflow that imports previous sessions
and reuses their on-disc payload extents. Its ability to create a UDF image alone
does not close that gap. This is an unproven candidate, not a finding that such an
extension would be impossible.
[API documentation](https://clalancette.github.io/pycdlib/pycdlib-api.html)
[Project source](https://github.com/clalancette/pycdlib)

The investigation initially suggested evaluating the proprietary Linux SDK's
licensing before investing in author development. The user's final decision
rejects that route for this project. An open-source Linux solution would require
a separate feasibility effort unless a suitable existing author is found later.
Windows IMAPI remains documented here as evidence that cumulative UDF multisession
is implementable, not as the selected workflow.

### Compatibility and space

The user's Panasonic playback test establishes that the current hybrid discs work
with the tested MKVs. The UB820 manual lists MKV on BD-R/BD-R DL but does not name
the filesystem selected from a hybrid disc or guarantee UDF multisession behavior.
Pure-UDF first-session playback and behavior after additions still need testing.
[UB820 manual, printed pages 6 and 41](https://eww.pavc.panasonic.co.jp/bd/UB820/DP-UB820_PC_EN_TQBS0255-5.pdf)

The ISO9660 bridge costs descriptors, directory records, path tables and, in raw
mode, Rock Ridge metadata. Payload is stored once. Savings depend on file/directory
counts and are generally modest for large MKVs or a few DAR slices. A read-only
inspection of existing local comparison images identified 34 KiB of allocated
ISO9660/Rock Ridge structures for 123 raw files in four directories, and 12 KiB
for five DAR-layout files in two directories. Counts include the root directory.
These are example metadata footprints, not exact savings from changing writers:
a different UDF layout, alignment or revision can change other overhead too.

The raw example's breakdown was 4 KiB of descriptors, 4 KiB of path tables,
24 KiB of directory blocks and 2 KiB of Rock Ridge continuation storage. The DAR
example was 4 KiB each for descriptors, path tables and directory blocks. These
local comparison fixtures and the inspection were not a multi-session burn test;
the fixtures are not part of the published repository. Treat the measurements as
illustrations, not a universal per-file constant or reproducible benchmark suite.

No conclusion about which filesystem the Panasonic actually chose can be drawn
from successful playback of the hybrid disc alone. Also, normal Linux mounting
does not automatically union independent session trees: the selected filesystem
must itself describe the full namespace. A reader's support for a recent UDF
revision does not by itself establish its support for selecting the latest session.

## Proposed architecture

Archived proposal only: all sections below are conditional on reopening the topic
and explicitly authorizing implementation. No backend has been selected.

Separate filesystem authoring from optical transport at the application boundary;
a selected SDK may implement both behind that boundary:

1. A UDF authoring adapter inspects the previous committed filesystem, plans a
   cumulative namespace, assigns extents, calculates exact output size, and emits
   only the next session's bytes. Existing file data remain at their recorded LBAs.
2. A BD-R transport queries media geometry and writes that prepared stream at the
   expected next writable address, closing the session while keeping the disc
   appendable. Select and test one transport; avoid speculative fallback backends.
3. A versioned disc/session manifest binds preparation to the prior disc state and
   describes payload coverage, recovery sets and commit identity.
4. Verification proves the expected commit is visible and checks all referenced
   data. Recovery reconstructs the address space and selects a known valid commit.

Use a cumulative namespace: after adding B to A, Linux normally exposes A and B.
Separate independent UDF sessions requiring users to switch mounts do not meet
that target. A VAT implementation is not mandatory if a standards-conforming
session-based author can reference earlier extents; this is a backend selection
question, not something to decide from the term “multisession” alone.

## Implementation milestones

### 0. Prove the authoring and recovery path before product integration

After implementation authorization, inspect candidate writer source and pin its
version, supported revision, licensing and distribution requirements. Require it
to populate arbitrary raw files and import existing extents without rewriting old
payload. A formatter that creates empty session structures does not pass.

Produce a three-commit fixture containing Unicode names, nested and empty
directories, zero-length files, a file larger than 4 GiB, and later additions to an
existing directory. Validate descriptor CRCs, partition-relative addresses,
anchors, end-of-session placement and filesystem integrity using an independent
reader and Linux. Force UDF reading to prove the cumulative UDF view; if pure UDF
is selected, additionally confirm there is no ISO9660 filesystem.

Reconstruct the full address space for offline append-image tests. A next-session
fragment is not necessarily independently mountable, and replacing referenced old
payload with zeros cannot prove data correctness.

Then, on explicitly designated disposable BD-R media, write three sessions with
the candidate transport. Reinsert and mount normally after each: all previous and
new files must be visible and byte-correct. Test both old and new player-compatible
files on the UB820 after each addition; record any session-selection limitation
without treating a working ISO9660 view as UDF success. Record drive, firmware,
medium, kernel and tool versions. BD-RE experiments alone cannot validate
sequential BD-R behavior.

Finally reproduce the recovery procedure below, including recovery when the local
preparation directory and session log are absent.

**Exit gate:** publish the exact tested toolchain, fixture hashes, session geometry,
normal-mount result, and recovery result. If no suitable author exists, stop and
present the cost of extending an upstream implementation or maintaining a limited
UDF author. A new filesystem writer is a materially larger project, not an implicit
helper to add while wiring the CLI. Do not substitute ISO9660 silently.

### 1. Define the format and media-state contract

Affected areas: `tools/mediainfo.py`, `archive/disc.py`, new session/domain module.

- Model profile, blank/appendable/complete/incomplete status, sessions and tracks,
  next writable LBA, writable blocks, recorded extent, and formatting mode.
- Use track-scoped values and cross-check against the selected burner. Handle
  compound states; never treat an already recorded track length as free space.
- Derive alignment and closure budgets from the chosen transport and measurements.
  Do not carry over the old plan's fixed inter-session-overhead arithmetic.
- Bind preparation to a disc identity plus prior manifest digest, commit ID and
  geometry. Re-read these immediately before writing; a swapped or changed disc
  requires re-preparation. A matching volume label is insufficient.
- Specify cumulative manifests, per-addition hashes/recovery sets, and how readers
  detect a missing or incomplete latest commit. Record geometry both locally and
  on the medium where possible; define reconstruction without the local copy.

### 2. Integrate cumulative UDF authoring; select pure UDF or an optional bridge

Affected areas: `tools/mkisofs.py` replacement, `archive/disc_folder.py`,
`commands/create_raw.py`, `commands/create.py`, `archive/sizing.py`, dependencies.

- Give sizing and emission the same deterministic layout plan and explicit UDF
  revision. Preserve source-change checks and hard padded-write capacity gates.
- Replace `rock_ridge` as the filesystem selector with a versioned format contract.
  Measure actual output size; account for the new transport's padding and anchors.
- Cover default disc folders and optional complete images. Account explicitly for
  any new scratch-space cost or inability to stream directly; do not silently add
  a full payload staging copy to the current folder workflow.
- Verify Unicode names, large extents, directory layout, timestamps and intended
  access permissions. Do not assume Rock Ridge and the current rationalized UDF
  view had identical ownership/permission behavior.
- If pure UDF is selected, remove ISO9660-only validation and dependencies where
  obsolete; replace volume identifier validation with the chosen UDF rules.
  Do not change unrelated archive
  naming constraints merely because one label limit disappears.
- Before changing readers, decide the treatment of existing hybrid discs, images
  and prepared folder manifests. No automatic migration or extra compatibility
  implementation is authorized by the new-write-format preference alone.

### 3. Integrate additive raw sessions

Affected areas: `cli.py`, `commands/create_raw.py`, `commands/burn.py`,
`archive/raw.py`, session metadata, new transport wrapper.

Proposed CLI shape, to finalize with the backend contract: explicit `create
--append-to DEVICE`, an explicit session byte budget or reserved remainder, and
`burn` consuming the prepared session manifest. Preparation identifies the disc;
burn revalidates it. No append decision based merely on nonblank media.

Each addition gets its own immutable recovery/checksum metadata. The latest
manifest references all committed additions, while payload retains normal paths
in the cumulative tree. Reject colliding file paths in the initial additive scope;
directory merging is supported. No implicit replacement, deletion or reclaimed
space on BD-R.

Automatic PAR2 uses the session budget, not every free block on the medium. Preview
must show payload, recovery, filesystem/session overhead, bytes written now and
space left for later. Appending new payload must not require regenerating PAR2 for
all previously recorded payload.

Port relevant burn guarantees to the selected transport: exclusive access, process
failure handling, two-press interruption behavior, cache invalidation and reinsertion,
then post-burn verification. Do not copy growisofs-specific flags blindly.

### 4. Make verification and rescue session-aware

Affected areas: `archive/verify.py`, `commands/verify.py`, `archive/disc.py`,
checksum/recovery discovery, on-disc README and recovery documentation.

- Verify the expected commit ID before accepting any payload verification result.
- Verify cumulative payload coverage, missing metadata and every designated PAR2
  set, including mixed protected/unprotected additions. Payload files named `.par2`
  remain ordinary data unless explicitly designated as recovery metadata.
- Standalone verification checks media/session state as well as the mounted view.
  A valid earlier snapshot must not imply that a failed later append succeeded.
- Validate loss/corruption of latest descriptors and partial append data. Recovery
  should locate the previous valid commit; ordinary verification must report the
  incomplete state rather than silently claim the entire disc is healthy.

The rescue procedure must preserve absolute 2048-byte LBA placement. Capture track
and session geometry, determine all readable recorded ranges, and use ddrescue
with an appropriate domain/mapfile. If the block device reports only an earlier
extent, prove an alternate readable path or range-based procedure on the hardware.
Account for session gaps without treating them as missing payload automatically.

A flat image does not carry an optical drive's session table. Derive and validate
UDF mount parameters against the full-address image, including anchor and last-block
locations as needed; do not confuse session numbers with LBA offsets. Linux provides
recovery overrides, but their successful use on our output remains an acceptance
test. Do not transplant ISO9660 `sbsector` or xorriso header-patching recipes to UDF.
[Linux UDF mount options](https://www.kernel.org/doc/html/latest/filesystems/udf.html)

ddrescue copies a selected byte domain; it does not inherently mean “first session
only.” The recorded extent, device access and subsequent filesystem interpretation
must be tested separately. Recover all old and new files and compare their hashes,
including a run without the original source/preparation data.
[GNU ddrescue manual](https://www.gnu.org/software/ddrescue/manual/ddrescue_manual.html)

### 5. DAR append and documentation

Only after raw acceptance, decide whether to extend the same transport to DAR
append. Reuse existing archive/generation folders and first-slice budgeting; adapt
chain discovery and extraction to cumulative sessions. Test several generations on
one disc and sets spanning multiple discs. Shared-writer changes to DAR creation
are already covered by milestone 2; DAR payload itself requires no new
compression/archive format.

Decide the role of `--pack-with` separately: it combines unburned contents, whereas
append extends recorded media. Neither silently remove it nor expand its behavior
as an incidental filesystem change.

For implementation, update and publish affected wiki pages alongside the code:
Installation, Docker, CLI-reference, Storage-and-layout, Raw-data-discs,
DAR-archives, Incrementals-and-packing, and Troubleshooting. Update generated
READMEs and concise top-level README essentials. Publish only implemented, tested
behavior. This planning task does not change the live usage documentation.

## Release acceptance

Automated tests must cover meaningful failure modes: incorrect NWA, disc swaps,
stale manifests, third and later sessions, alignment/capacity edges, incomplete
writes, missing latest metadata, and false success from an old mounted view.
Integration tests must inspect real generated UDF structures and mount results,
not just assert command argument strings. Run the project test suite after changes.

Hardware acceptance requires multiple append cycles on actual target BD-R media,
full-file reads beyond 4 GiB, and a second reader where available. Record which
capacities and drive combinations were tested; do not extrapolate SL results to
BDXL. Linux cumulative visibility and complete recovery are release requirements.
Player visibility of both old and new files after append and Windows/macOS
observations are reported separately, with untested combinations explicitly
identified. Deliberately leaving UDF stale while relying on ISO9660 is rejected.

The authoring/recovery proof is the critical path and the main uncertainty. Until
it passes, a reliable total implementation estimate or production-readiness claim
would be premature.
