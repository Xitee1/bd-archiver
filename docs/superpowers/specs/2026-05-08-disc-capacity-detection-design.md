# Disc Capacity Detection — Design

Replace hardcoded BD-25/50/100 disc sizes in `bd-archive.py` with runtime detection of the actual disc capacity. Add a pre-burn fit check so prepared discs don't get burned to the wrong-sized media. Drop the now-redundant `bd-archive.json` metadata file.

## Motivation

The current script hardcodes three Blu-ray sizes in `DISC_CAPACITY` and forces the user to pick one with `-d 25|50|100`. This:

- Wastes space when the actual disc is slightly smaller or larger than the spec value.
- Requires manual disc-size knowledge from the user.
- Doesn't catch the case where the user prepared an archive for BD-25 but inserts a BD-50 at burn time (or vice-versa).

The metadata file `bd-archive.json` exists only to bridge `create` → `burn`, but every value `burn` actually consumes is derivable from the staging directory contents. Dropping it removes a contract that has to be maintained for no real benefit.

## Scope

In scope:
- Auto-detect raw writable disc capacity at `create` time via `dvd+rw-mediainfo`.
- Manual override via `-b/--bytes <int>` (raw bytes, no GB shorthand).
- Pre-burn fit check (disc must hold staging, no more than 5% larger).
- `--skip-fit-check` escape hatch on `burn`.
- Remove `bd-archive.json` and the `DISC_CAPACITY` constant entirely.
- Update README.txt on each disc to show actual capacity bytes instead of "BD-25" label.

Out of scope:
- Detecting whether the inserted disc is blank vs. partially-written (we trust `Free Blocks`).
- Backward compatibility with existing workdirs that have `bd-archive.json` — none have shipped beyond the user's own local use.

## Components

### `detect_disc_capacity(device: str) -> int | None`

New top-level helper. Single source of truth for capacity detection.

- Runs `dvd+rw-mediainfo <device>` via the existing `run()` wrapper with `capture=True, check=False`.
- Parses the line `Free Blocks:           NNNN*2KB` from stdout (regex on `Free Blocks:\s+(\d+)\*2KB`).
- Returns `NNNN * 2048` (raw writable bytes) on match.
- Returns `None` on any of: command exits non-zero, no `Free Blocks:` line found, parse fails. The caller decides how to handle `None` (hard error in `create`, soft warn in `burn`).

`dvd+rw-mediainfo` ships with `dvd+rw-tools`, which is already a runtime dependency (it provides `growisofs`). `check_deps()` for both `create` and `burn` gets `dvd+rw-mediainfo` added.

### `cmd_create` changes

CLI:
- **Removed:** `-d/--disc-size {25,50,100}`.
- **Added:** `-D/--device` (default `/dev/sr0`).
- **Added:** `-b/--bytes <int>` (raw writable bytes; overrides detection).

Capacity resolution at the start of `cmd_create`:

```
if args.bytes is not None:
    raw_capacity = args.bytes
    log.info(f"Using manual capacity: {human_bytes(raw_capacity)}")
else:
    raw_capacity = detect_disc_capacity(args.device)
    if raw_capacity is None:
        log.error(f"No disc detected at {args.device}.")
        log.info("Insert a blank disc, or specify capacity manually with -b/--bytes <int>.")
        sys.exit(1)
    log.info(f"Detected {human_bytes(raw_capacity)} free space, splitting with this size")

disc_bytes = raw_capacity - 2 * MiB  # ISO/UDF filesystem overhead, same as before
```

Slice-size math from `disc_bytes` downward (overhead, redundancy split, MiB floor) is unchanged. `DISC_CAPACITY` is deleted.

The "Configuration" log block and the per-disc size summary use `human_bytes(disc_bytes)` instead of the `BD-{n}` label.

### `cmd_burn` changes

CLI additions:
- `--skip-fit-check`: bypass the new pre-burn capacity check (still warns).

Metadata removal — replace the `load_metadata(work_dir)` call at the top with directory-derived state:

```python
staging_root = work_dir / "staging"
disc_dirs = sorted(
    d for d in staging_root.iterdir()
    if d.is_dir() and d.name.startswith("disc_")
)
if not disc_dirs:
    log.error(f"No staging directories found in {staging_root}.")
    log.info("Run 'create' first to prepare the archive.")
    sys.exit(1)
disc_count = len(disc_dirs)

# archive_name from the first non-catalog .dar file in disc_1
first_dar = next(
    p for p in disc_dirs[0].glob("*.dar")
    if "-catalog" not in p.name
)
archive_name = first_dar.stem.rsplit(".", 1)[0]  # "name.001" -> "name"
```

Pre-burn fit check, inserted after `prompt_disc()` and before `dio.burn()`:

```python
stage_size = sum(f.stat().st_size for f in stage.iterdir() if f.is_file())

if not args.skip_fit_check:
    actual = detect_disc_capacity(args.device)
    if actual is None:
        log.warn("Could not detect disc capacity — skipping fit check")
    elif actual < stage_size:
        log.error(
            f"Disc too small: {human_bytes(actual)} < "
            f"staging {human_bytes(stage_size)}"
        )
        log.info(f"Resume later with: bd-archive.py burn -w {work_dir} --start {i}")
        sys.exit(1)
    elif actual > stage_size * 1.05:
        log.error(
            f"Disc too large: {human_bytes(actual)} > "
            f"{human_bytes(stage_size)} + 5% — refusing to waste space"
        )
        log.info("Insert a smaller disc, or pass --skip-fit-check to override.")
        log.info(f"Resume later with: bd-archive.py burn -w {work_dir} --start {i}")
        sys.exit(1)
```

The check uses `staging_size` (not the original `create`-time disc capacity, which we no longer persist). With redundancy ≥ 1%, staging fills the prepared disc to ~99.9% of its raw capacity, so `staging_size * 1.05` reliably accepts the same disc tier and rejects the next-larger tier. A user who manually specifies a much smaller `-b` than the inserted disc will be told to use `--skip-fit-check`; that is the intended UX.

Detection failure (`actual is None`) is a soft warn at burn time — a transient detection problem should not break a multi-disc burn that's halfway done. At `create` time it's fatal because we need the number to compute slice sizes.

### `generate_readme` changes

Signature drops `disc_size: int`, gains `disc_bytes: int` (the post-overhead per-disc capacity). The first line of `README.txt` becomes:

```
BD-ARCHIVE | <name> | Disc N/M | <ts> | Capacity {human_bytes(disc_bytes)} | PAR2 r% | <comp>
```

### Removed code

- `DISC_CAPACITY` constant.
- `METADATA_FILE` constant.
- `save_metadata()` and `load_metadata()` functions.
- The `import json` line (no longer used anywhere).
- Any "BD-{n}" string formatting in logs/prompts/summaries.

## Data flow

```
create:
  args.bytes ──or── dvd+rw-mediainfo /dev/sr0 ──┐
                                                 ▼
                                      raw_capacity (bytes)
                                                 │
                                       - 2 MiB FS overhead
                                                 ▼
                                          disc_bytes
                                                 │
                              (- overhead, / (1+r/100), MiB floor)
                                                 ▼
                                          slice_bytes ── dar -s ─→ slices
                                                                     │
                                                                     ▼
                                                            staging/disc_N/

burn (per disc):
  staging/disc_N/ ─── stage_size = sum(file sizes) ───┐
                                                       │
  dvd+rw-mediainfo /dev/sr0 ──→ actual ────────────────┤
                                                       ▼
                                          fit check (5% tolerance)
                                                       │
                                                       ▼
                                                  growisofs
```

## Error handling

| Situation | Where | Behavior |
|---|---|---|
| No disc, no `-b` | `create` | Hard error, exit 1, suggest `-b` |
| `dvd+rw-mediainfo` not installed | `check_deps` | Hard error before either subcommand starts |
| Detection fails mid-burn | `burn` per-disc | Warn, skip fit check, proceed |
| Disc smaller than staging | `burn` | Hard error, suggest `--start N` to resume |
| Disc >5% larger than staging | `burn` | Hard error, suggest smaller disc or `--skip-fit-check` |
| `--skip-fit-check` set | `burn` | Skip detection entirely (no warn) |
| No staging dirs in workdir | `burn` | Hard error (replaces "no metadata" error) |

## Testing

This project has no test suite. Manual verification path:

1. **`create -b <bytes>` without disc** — should compute slices and succeed (proves manual override works without device).
2. **`create` with blank BD-R inserted** — should log "Detected ... free space" and produce same-sized slices as the old `-d 25` for a real BD-25.
3. **`create` with no disc and no `-b`** — should hard-error with the suggested fix.
4. **`burn` after dropping `bd-archive.json`** — should still work (proves metadata is fully derived).
5. **`burn` with the wrong-sized disc inserted** — should reject with the 5% rule.
6. **`burn --skip-fit-check`** — should burn without inspecting the disc.

## Versioning

`VERSION` bumps to `4.0.0` because the on-disc README format changes and the metadata-file contract is removed. Existing `bd-archive.json` files from `3.0.0` workdirs become silently ignored — `burn` no longer looks for them.
