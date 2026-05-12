# Incremental Archives + Catalog Policy — Design Spec

**Status:** Approved (2026-05-12)
**Scope:** Add dar-based incremental backup support to bd-archiver, plus a catalog-storage policy change that reduces per-disc overhead.

## Problem statement

bd-archiver currently produces one-shot full archives. Two real-world use cases need more:

1. **Family-photo archive** — content accumulates slowly over years; user wants to extend an existing set instead of re-burning everything when new photos land. Late-arriving photos from old years (someone shares a folder) cannot currently be added to a "closed" year without redoing it.
2. **Time-lapse archive** — large and growing collection. The user has worked around the lack of incrementals by splitting into batches (separate archive sets), which is awkward and inflates the catalog footprint.

Additionally, the current code writes the isolated dar catalog onto **every** disc of an archive set. For the user's current 130 MB catalog × 20 discs that's 2.3 GB of redundancy; for incremental setups where catalogs grow with cumulative file count over years, this overhead would balloon unboundedly.

## Design summary

### Catalog policy (independent of incrementals)

- Isolated catalog persists locally as `<output>/<name>-gen<N>-catalog.dar` (+ `.sha512` sidecar) alongside `images/`.
- On discs: only **Disc 1** of each generation carries the isolated catalog.
- Discs 2..N-1 contain only slice + par2 + README.
- Disc N (last) implicitly holds the master catalog at the end of its slice (dar default — not under our control).
- For 1-disc sets the isolated catalog ends up on the same disc as the embedded master catalog. Mildly redundant, not worth special-casing.

### Incremental archive support

User-facing CLI (Gen 1 stays as today; Gen 2+ adds `--base`):

```bash
# Full (Gen 1)
bd-archive create -s ~/photos -n photos -o ./gen1

# Incremental (Gen 2, base = Gen 1's local catalog)
bd-archive create -s ~/photos -n photos \
                  --base ./gen1/photos-gen1-catalog.dar \
                  -o ./gen2
```

**Chain identity = archive name.** The `-n` value is the chain ID. Users must use the *same* `-n` across all generations of a chain. This constraint is documented prominently in the project `README.md` and validated by `create` (passing `--base` whose parsed archive name differs from `-n` → hard error).

**Naming scheme internally**: dar archive name is `<name>-gen<N>` where N is derived as:
- No `--base` given → N = 1
- `--base <path>` given → parse `gen<K>` from base catalog filename; N = K+1
- Base catalog filename lacks `-gen<K>-` suffix (legacy pre-feature archive) → assume K=1, so N=2

This makes "extend a legacy archive" work without migration tooling: user copies `<name>-catalog.0001.dar` off an old disc and passes its path as `--base`.

### Volume label scheme

New format: `<truncated_name>_G<NN>_<NNNN>` (32-char ISO9660 max).

- 4 chars reserved for `_G<NN>` (gen 01-99, zero-padded)
- 5 chars for `_<NNNN>` (disc number)
- 23 chars max for name in the label

If user passes a name longer than 23 chars: `bd-archive create` issues a warning and truncates only in the volume label. Filenames inside the ISO retain the full archive name (e.g., `super-long-archive-name-here-gen2.0001.dar`).

This is a **format change** vs. current `<NAME>_<NNNN>` labels. Existing pre-feature Gen 1 discs keep their old label scheme; new discs use the new scheme. Visual mixing is fine — labels are hints, not technical IDs.

### Auto-defer (min-last-disc-fill)

Flag: `--min-last-disc-fill PERCENT` (integer 0-100, default 0 = disabled).

Semantics: enforce a minimum fill percentage on the last disc of the set by deferring "truly new" files to a later generation.

**Eligibility (which files are deferrable)**:
- For incrementals (`--base` given): files whose relative path is **not present** in the base catalog. Determined authoritatively via `dar -l <base-catalog>` parse; mtime is **not** used for eligibility (avoids losing files whose mtime drifted on disk). mtime *is* used for ordering — newest first.
- For full archives (no `--base`): all files (with prominent warning that deferred files won't be archived until a future incremental).

**Algorithm**:
1. Build pool of deferrable files (per rules above), sort newest-first by mtime.
2. Estimate disc count and last-disc fill with full source.
3. While last-disc-fill < threshold AND pool non-empty: pop newest, add to defer set, recompute.
4. Show user the resulting plan (file count, total bytes deferred, oldest deferred mtime).
5. Confirmation prompt with full picture.
6. Pass defer set as `-P <relative-path>` flags to dar.

**Edge cases**:
- Pool exhausted before threshold met → proceed with what's achievable, log "threshold not reachable, deferring N files brings last-disc-fill to M%".
- All source files deferred → abort with explanatory error.

### Modified-file & bit-flip behavior

Out of scope for this feature; **dar's default semantics apply unchanged**:
- Intentional modifications (any change to ctime/mtime/size) → archived as modified entry in incremental.
- Silent bit flips on source (data corruption with mtime unchanged) → NOT detected; file treated as unchanged.

Mitigation is the user's responsibility (filesystem-level checksumming, separate integrity scans).

### Verify

No chain-aware mode. `verify` continues to operate per-disc / per-ISO / per-mount as today.

### Extract — whole-chain mode

Single `bd-archive extract -o <output>` call handles a complete chain.

**Flow**:
1. User inserts any disc to start. Tool reads filename pattern and catalog, identifies archive name and current generation.
2. Tool prompts: "How many generations does this chain have?" (1-99). This is the only piece of state we can't derive from disc contents.
3. Tool iterates generations 1..N. For each generation: prompts for each disc 1..M_gen (M derived from catalog of that gen).
4. All slices land in a flat staging dir.
5. Single `dar -x <staging>/<name>-gen<N> -R <output> -O --sequential-read` (highest gen as entry point — dar walks back through generations via internal references).

**Legacy (pre-feature) archives** detected by missing `-gen<N>` suffix in slice filenames:
- Treated as a standalone Gen 1 with no chain (current behavior preserved).
- If user wants to extract a chain that mixes legacy Gen 1 + new Gen 2+: the new generations carry `-gen<N>` suffix, the legacy doesn't. Extract handles both name patterns within the same staging dir; dar's chain-walking still works because `--base` at create time recorded the legacy archive's name as the predecessor.

**Damage handling** is unchanged: SHA-512 verify in staging, PAR2 repair on failure, per-slice not per-disc. Catalog verified once on first intact arrival.

## Code impact

Layering remains intact: `commands/` → `archive/` → `tools/` → `shell/`. No new top-level layer.

### Affected modules

| Module | Change | Phase |
|---|---|---|
| `commands/create.py` | Catalog placement on Disc 1 only; persist local catalog copy; `--base` handling; base-aware preview; auto-defer block; new volume-label scheme | 1, 2, 3, 4 |
| `commands/extract.py` | Refactor: outer-loop over generations, inner-loop over discs; handle `<name>-gen<N>.*.dar` and legacy `<name>.*.dar` patterns | 5 |
| `archive/dar_archive.py` | Naming scheme `<name>-gen<N>`; `excludes` and `ref_catalog` pass-through | 2, 3, 4 |
| `archive/config.py` | `ArchiveConfig` gains `generation: int`; README text updates | 2 |
| `archive/source_scan.py` | New helper for listing source files with mtime, for auto-defer | 4 |
| `tools/dar.py` | `create_sliced(ref_catalog=None, excludes=None)`; new `list_catalog_paths(catalog)` parsing `dar -l` output | 3, 4 |
| `cli.py` | `--base PATH` and `--min-last-disc-fill INT` on `create` | 3, 4 |
| `README.md` (project) | Document chain-name discipline; document `--base` and `--min-last-disc-fill`; document catalog persistence path | 2, 3, 4 |

### Phases

Each phase is independently shippable.

**Phase 1 — Catalog policy** (~20 LOC)
- `commands/create.py`: catalog files added to Disc 1 sources only (loop conditional); copy catalog to `<output>/<name>-gen<N>-catalog.dar` before tmp cleanup
- `commands/extract.py`: graceful fallback when catalog file present only on first intact disc
- Tests: by hand on a small archive

**Phase 2 — Naming scheme + label change** (~25 LOC)
- `archive/dar_archive.py`: name becomes `<name>-gen1` for Full
- `archive/config.py`: `generation` field
- `commands/create.py`: volume label `<truncated>_G<NN>_<NNNN>` + truncation warning
- Project `README.md`: chain-name discipline section

**Phase 3 — Incremental `create`** (~45 LOC)
- `cli.py`: `--base PATH`
- `tools/dar.py`: `create_sliced(ref_catalog=...)`; `list_catalog_paths()` helper
- `archive/dar_archive.py`: pass-through
- `commands/create.py`: parse gen from base, base-aware preview, validate `-n` matches base's archive name
- Project `README.md`: incremental workflow section

**Phase 4 — Auto-defer** (~55 LOC)
- `cli.py`: `--min-last-disc-fill INT`
- `archive/source_scan.py`: file-list helper
- `commands/create.py`: defer algorithm between preview and confirm prompt
- `tools/dar.py`: `excludes` pass-through (`-P` flags)
- Project `README.md`: auto-defer behavior

**Phase 5 — Extract chain-mode** (~80 LOC)
- `commands/extract.py`: generation iteration; multi-gen staging; legacy-pattern handling
- Project `README.md`: extract workflow for chains

**Total ~225 LOC, 5 PRs.**

## Out of scope

- `dar_xform` / `dar -+` consolidation of older generations into a fresh full
- Hash-based change detection (`--strict-change-detection`) for bit-flip protection
- Automated migration helper for legacy single-gen archives (manual copy is sufficient)
- `verify --chain` mode for whole-chain integrity check
- Mixing multiple independent chains in one `extract` run

## Open questions

None as of approval. Phase-1 implementation can start.
