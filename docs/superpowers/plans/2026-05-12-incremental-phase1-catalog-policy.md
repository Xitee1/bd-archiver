# Incremental Phase 1 — Catalog Policy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop replicating the isolated dar catalog onto every disc of an archive set. Place it on Disc 1 only and persist a local copy alongside the disc images.

**Architecture:** Two surgical edits in `commands/create.py`. The current code adds catalog files to every disc's ISO sources inside the per-disc build loop. We gate that addition on `i == 1`. Separately, before the final `shutil.rmtree(tmp_dir)` we copy the catalog files from `tmp/` into `<output>/` so the user has them for backup and (in later phases) as `--base` reference. No changes to `extract.py` — its catalog-acquisition logic already only consumes the catalog from the first intact disc that carries it, so it stays correct.

**Tech Stack:** Python 3.11+, standard library only (shutil, pathlib). Project has no test framework — verification is by manual end-to-end run, matching the project's existing convention documented in `CLAUDE.md` ("No tests").

**Commits:** Commit steps are included per the writing-plans skill template. Per the project's global CLAUDE.md, the executor MUST NOT run a `git commit` without the user's explicit go-ahead. Treat the commit step as "stage + propose commit message, wait for approval."

---

## File Structure

| Path | Responsibility |
|---|---|
| `src/bd_archive/commands/create.py` | Sole file touched. Two edits: (a) gate catalog inclusion to Disc 1 inside the build loop; (b) persist catalog to `output_dir` before tmp cleanup; (c) cosmetic summary update |

No new files. No extract.py changes (verified to be already graceful — see "Why no extract changes" note below).

### Why no extract changes

`src/bd_archive/commands/extract.py:33-41` (function `_copy_disc_data`) copies catalog files from a mounted disc only while `catalog_verified == False`. Once a disc yields an intact, sha512-verifying catalog, the flag flips and no later disc is queried for catalog. Discs without the catalog file are silently fine (the glob returns nothing).

`src/bd_archive/commands/extract.py:240-244` checks `has_catalog = any(...)` and passes `catalog_base=None` to `dar.extract_sequential` when nothing was staged. `tools/dar.py::extract_sequential` accepts `catalog_base=None` (line 43, conditional `-A` append). dar's `--sequential-read` mode then walks slices without an isolated catalog as rescue — slower but correct.

So: dropping the catalog from discs 2..N-1 changes nothing for extract. Task 4 below confirms this in a real run.

---

## Task 1: Gate catalog inclusion to Disc 1 only

**Files:**
- Modify: `src/bd_archive/commands/create.py:228-232`

- [ ] **Step 1: Read the current build-loop block**

Open `src/bd_archive/commands/create.py` and locate the block that assembles `sources` per disc inside `for i, slice_file in enumerate(slices, 1):`. Around lines 228-232 you will see:

```python
        for cat in dar_archive.catalog_files:
            sources.append(cat)
            cat_hash = Path(str(cat) + ".sha512")
            if cat_hash.exists():
                sources.append(cat_hash)
```

This appends every catalog file (and its sha512 sidecar) into the per-disc ISO source list — for every disc.

- [ ] **Step 2: Make the inclusion conditional on disc index == 1**

Replace the block above with:

```python
        # Catalog goes onto Disc 1 only. The master catalog at the end of
        # the last slice (dar default) plus this isolated copy on Disc 1
        # gives two spatially separated copies per archive set. Replicating
        # on every disc was redundant and grew unboundedly with file count.
        if i == 1:
            for cat in dar_archive.catalog_files:
                sources.append(cat)
                cat_hash = Path(str(cat) + ".sha512")
                if cat_hash.exists():
                    sources.append(cat_hash)
```

The indentation (8 spaces) matches the surrounding `for i, slice_file ...` block body.

- [ ] **Step 3: Quick syntax check**

Run from project root:

```bash
python3 -m py_compile src/bd_archive/commands/create.py
```

Expected: exit 0, no output. A SyntaxError here means the indentation drifted.

- [ ] **Step 4: Lint**

Run:

```bash
ruff check src/bd_archive/commands/create.py
```

Expected: `All checks passed!` or no findings on the modified lines.

- [ ] **Step 5: Stage**

```bash
git add src/bd_archive/commands/create.py
```

Do NOT commit yet — Task 2 makes a companion change and they belong in one commit.

---

## Task 2: Persist isolated catalog to output directory

**Files:**
- Modify: `src/bd_archive/commands/create.py` — add a new block between the per-disc build loop and `shutil.rmtree(tmp_dir)` (around line 267)

- [ ] **Step 1: Locate the insertion point**

Open `src/bd_archive/commands/create.py` and find the comment block:

```python
    # Final cleanup: drop the entire tmp/ tree (catalog, dar internals).
    # If workdir is the default hidden one, also remove it — the only
    # thing inside was tmp/, so leaving it would just be cruft. A
    # user-supplied workdir is left alone so they can keep tmpfs mounts
    # etc. exactly as configured.
    shutil.rmtree(tmp_dir)
```

We insert the catalog-persistence block *immediately before* this `# Final cleanup:` comment.

- [ ] **Step 2: Insert the catalog-persistence block**

Add this block, indented 4 spaces (function-body level):

```python
    # Persist the isolated catalog alongside images/ for two reasons:
    #   1. It survives `output_dir` being burned + the local images/
    #      being deleted — user keeps the catalog as part of their
    #      regular backup.
    #   2. Future incremental generations will reference this file via
    #      `--base` (not implemented yet in this phase, but the artifact
    #      needs to exist from this phase onward).
    for cat in dar_archive.catalog_files:
        shutil.copy2(cat, output_dir / cat.name)
        cat_hash = Path(str(cat) + ".sha512")
        if cat_hash.exists():
            shutil.copy2(cat_hash, output_dir / cat_hash.name)
    catalog_persisted = sorted(output_dir.glob(f"{cfg.name}-catalog.*.dar"))
    if catalog_persisted:
        log.info(f"Catalog persisted: {catalog_persisted[0].parent}/{cfg.name}-catalog.*.dar")

```

(Leave a blank line after the block so it visually separates from the `# Final cleanup:` comment that follows.)

- [ ] **Step 3: Syntax check**

```bash
python3 -m py_compile src/bd_archive/commands/create.py
```

Expected: exit 0.

- [ ] **Step 4: Lint**

```bash
ruff check src/bd_archive/commands/create.py
```

Expected: clean.

- [ ] **Step 5: Stage**

```bash
git add src/bd_archive/commands/create.py
```

---

## Task 3: Mention the persisted catalog in the summary output

**Files:**
- Modify: `src/bd_archive/commands/create.py` — summary block around lines 280-288

- [ ] **Step 1: Locate the summary block**

At the bottom of `cmd_create`:

```python
    log.step("Summary")
    print(f"\n  Source:       {human_bytes(scan.total_bytes)}")
    print(f"  Archive:      {human_bytes(total_archive)} ({ratio}%)")
    print(f"  Discs:        {slice_count} x {human_bytes(raw_capacity)}")
    print(f"  PAR2:         {cfg.redundancy}% per disc")
    print(f"  Compression:  {cfg.comp_str}")
    print(f"  Images:       {images_dir}")
    print(f"\n  Next step:    bd-archive burn -i {output_dir}")
    print(f"  Cleanup:      rm -rf {output_dir}\n")
```

- [ ] **Step 2: Add a line between "Images:" and the blank line**

Insert one line so the block reads:

```python
    log.step("Summary")
    print(f"\n  Source:       {human_bytes(scan.total_bytes)}")
    print(f"  Archive:      {human_bytes(total_archive)} ({ratio}%)")
    print(f"  Discs:        {slice_count} x {human_bytes(raw_capacity)}")
    print(f"  PAR2:         {cfg.redundancy}% per disc")
    print(f"  Compression:  {cfg.comp_str}")
    print(f"  Images:       {images_dir}")
    print(f"  Catalog:      {output_dir}/{cfg.name}-catalog.*.dar")
    print(f"\n  Next step:    bd-archive burn -i {output_dir}")
    print(f"  Cleanup:      rm -rf {output_dir}\n")
```

The new line tells the user where the persisted catalog lives so they know to keep it with their regular backup.

- [ ] **Step 3: Syntax + lint**

```bash
python3 -m py_compile src/bd_archive/commands/create.py && ruff check src/bd_archive/commands/create.py
```

Expected: exit 0, clean lint.

- [ ] **Step 4: Stage**

```bash
git add src/bd_archive/commands/create.py
```

---

## Task 4: End-to-end manual verification

This task is the proof. It must pass before commit.

**Files:** none modified. Uses a scratch directory.

- [ ] **Step 1: Prepare a small source tree that will produce ≥3 discs**

Pick a tiny disc size so we get multi-disc output without consuming real storage. Slice size of 5 MiB means a 16 MiB source produces ~3-4 slices.

```bash
SCRATCH=$(mktemp -d /tmp/bd-archive-phase1-XXXXXX)
mkdir -p "$SCRATCH/src"
# Generate ~16 MiB of incompressible content across multiple files
for i in $(seq 1 8); do
  dd if=/dev/urandom of="$SCRATCH/src/file_$i.bin" bs=1M count=2 status=none
done
echo "$SCRATCH"
```

- [ ] **Step 2: Run `bd-archive create` with a small manual capacity**

```bash
cd /home/mato/projects/_Privat/bd-archiver
source .venv/bin/activate  # ensure editable install is active
bd-archive create \
  -s "$SCRATCH/src" \
  -n phase1test \
  -o "$SCRATCH/out" \
  -b $((5 * 1024 * 1024)) \
  --ratio 1.0 \
  -y
```

Expected: completes, prints a "Catalog persisted: …" line, prints "Catalog: …" in the summary.

- [ ] **Step 3: Verify the persisted catalog lives in output_dir**

```bash
ls -l "$SCRATCH/out/phase1test-catalog."*.dar
ls -l "$SCRATCH/out/phase1test-catalog."*.dar.sha512 2>/dev/null || echo "(no sha512 sidecar — that is OK)"
```

Expected: at least one `phase1test-catalog.0001.dar` file present. sha512 sidecar may or may not exist (depending on dar's behavior — dar produces sha512 for slices, not necessarily for the catalog).

- [ ] **Step 4: Verify Disc 1 ISO contains the catalog file**

```bash
udisksctl loop-setup -f "$SCRATCH/out/images/disc_0001.iso" 2>&1 | tee /tmp/disc1-loop.log
LOOP=$(grep -oE '/dev/loop[0-9]+' /tmp/disc1-loop.log | head -1)
udisksctl mount -b "$LOOP"
MNT=$(udisksctl info -b "$LOOP" | awk -F': ' '/MountPoints:/ {print $2; exit}' | tr -d ' ')
ls -l "$MNT"
udisksctl unmount -b "$LOOP"
udisksctl loop-delete -b "$LOOP"
```

Expected: `ls` shows `phase1test-catalog.0001.dar` among the disc contents (alongside the slice, par2 files, README).

- [ ] **Step 5: Verify Disc 2 ISO does NOT contain the catalog**

```bash
udisksctl loop-setup -f "$SCRATCH/out/images/disc_0002.iso" 2>&1 | tee /tmp/disc2-loop.log
LOOP=$(grep -oE '/dev/loop[0-9]+' /tmp/disc2-loop.log | head -1)
udisksctl mount -b "$LOOP"
MNT=$(udisksctl info -b "$LOOP" | awk -F': ' '/MountPoints:/ {print $2; exit}' | tr -d ' ')
ls -l "$MNT"
udisksctl unmount -b "$LOOP"
udisksctl loop-delete -b "$LOOP"
```

Expected: `ls` shows the Disc 2 slice + its par2 + README, but **no** `phase1test-catalog.*.dar` file. This is the central check of Phase 1.

- [ ] **Step 6: Verify the last disc's ISO also does NOT contain the isolated catalog**

```bash
LAST_ISO=$(ls "$SCRATCH/out/images"/disc_*.iso | tail -1)
udisksctl loop-setup -f "$LAST_ISO" 2>&1 | tee /tmp/disc-last-loop.log
LOOP=$(grep -oE '/dev/loop[0-9]+' /tmp/disc-last-loop.log | head -1)
udisksctl mount -b "$LOOP"
MNT=$(udisksctl info -b "$LOOP" | awk -F': ' '/MountPoints:/ {print $2; exit}' | tr -d ' ')
ls -l "$MNT"
udisksctl unmount -b "$LOOP"
udisksctl loop-delete -b "$LOOP"
```

Expected: the last disc has its slice + par2 + README, but no isolated `phase1test-catalog.*.dar`. The dar slice itself embeds the master catalog at its end — that is unchanged.

- [ ] **Step 7: Verify `bd-archive extract` still recovers correctly with the new layout**

Use the ISOs as input (no real burning needed):

```bash
mkdir -p "$SCRATCH/restored"
# Verify each ISO is internally consistent first
for iso in "$SCRATCH/out/images"/disc_*.iso; do
  echo "=== $(basename "$iso") ==="
  bd-archive verify "$iso" || echo "VERIFY FAILED: $iso"
done
```

Expected: every disc reports OK (exit 0). The verify path reads the catalog from Disc 1's ISO only and that suffices — confirms extract.py will be happy too.

- [ ] **Step 8: Verify source-vs-restore byte-identity (optional but recommended)**

Extracting from ISO files via `bd-archive extract` requires a physical drive in the current implementation (it prompts for disc inserts). For Phase 1 manual verification, the ISO-level verify in Step 7 is sufficient — extract.py's behavior with respect to catalog placement is determined entirely by `_copy_disc_data`, which we have not touched, and `verify` exercises the same catalog-acquisition path.

- [ ] **Step 9: Cleanup the scratch dir**

```bash
rm -rf "$SCRATCH"
```

---

## Task 5: Commit

Only proceed after Task 4 passes. Per the project's global CLAUDE.md, the executor must obtain user approval for the commit message before running `git commit`.

- [ ] **Step 1: Show the staged diff to the user**

```bash
git diff --staged src/bd_archive/commands/create.py
```

- [ ] **Step 2: Propose this commit message to the user**

```
refactor(create): place isolated catalog on Disc 1 only; persist locally

Previously, the isolated dar catalog was duplicated into every disc's
ISO. For archives with thousands of files (and growing over incremental
generations) this added unbounded per-disc overhead — 130 MB per disc
in the user's photo archive, scaling with file count not data size.

The dar slice on the last disc still embeds the master catalog at its
end (dar default, unchanged), so we always have two spatially separated
copies per archive set: the isolated copy on Disc 1, and the embedded
master on the last disc. Discs 2..N-1 carry only their slice + par2.

The isolated catalog is now also persisted to <output>/<name>-catalog.*.dar
alongside images/, so the user can keep it in their normal digital
backup. Phase 3 will use this file as the `--base` reference for
incremental generations.

No extract.py changes needed: its catalog-acquisition logic already
copies the catalog only from the first intact disc carrying it.
```

- [ ] **Step 3: After user approves, commit**

```bash
git commit -m "$(cat <<'EOF'
refactor(create): place isolated catalog on Disc 1 only; persist locally

Previously, the isolated dar catalog was duplicated into every disc's
ISO. For archives with thousands of files (and growing over incremental
generations) this added unbounded per-disc overhead — 130 MB per disc
in the user's photo archive, scaling with file count not data size.

The dar slice on the last disc still embeds the master catalog at its
end (dar default, unchanged), so we always have two spatially separated
copies per archive set: the isolated copy on Disc 1, and the embedded
master on the last disc. Discs 2..N-1 carry only their slice + par2.

The isolated catalog is now also persisted to <output>/<name>-catalog.*.dar
alongside images/, so the user can keep it in their normal digital
backup. Phase 3 will use this file as the `--base` reference for
incremental generations.

No extract.py changes needed: its catalog-acquisition logic already
copies the catalog only from the first intact disc carrying it.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 4: Verify the commit landed cleanly**

```bash
git log -1 --stat
```

Expected: shows the commit, lists `src/bd_archive/commands/create.py` modified.

---

## Self-Review Done

- Spec coverage: Phase 1 of the spec specifies "Catalog files added to Disc 1 sources only (loop conditional); copy catalog to `<output>/<name>-gen<N>-catalog.dar` before tmp cleanup; extract.py graceful fallback." Tasks 1+2+3 cover the create.py changes; Task 4 Step 7 verifies extract.py graceful behavior is preserved (no code change needed there as analyzed in the architecture note). The Phase-1 catalog name omits `-gen<N>-` intentionally because the gen-naming scheme arrives in Phase 2; Phase 1 uses dar's existing `<name>-catalog.*.dar` naming.
- Placeholder scan: no TBD/TODO/handwave.
- Type consistency: only `Path` objects from `pathlib` used; existing `dar_archive.catalog_files` API consumed unchanged.
- LOC estimate from spec: ~20. Actual diff size of Tasks 1+2+3 ≈ 15 lines added, 5 lines wrapped — on target.
