# Exporter → Single Executable

**Status:** Plan of record. Supersedes the ad-hoc Python + Blender + Rust prerequisite chain.
**Milestone 0 is the owner-decision gate; §12 Q1–Q11 must be answered in writing before Milestone 1 starts.**

> **Citation convention.** Every `file:line` in this document was re-verified against the working tree at commit `46fb5b2`. Line numbers drift; re-grep before editing. Sizes are marked **(m)** measured on this machine or from a published artifact, **(e)** estimate that must be replaced with a measurement in Milestone 8.

> **Independently re-verified at authoring time**, because they are the claims the whole plan rests on: `anarqz/fodpak` is `"visibility":"PUBLIC"`, 1,256,969 KB (§9.4); `Builds/SteamOS/…/cod_asset_importer.abi3.so` is `Mach-O 64-bit … arm64` (§2.2); `Builds/Windows/Exporter/tools/` has no `cod-asset-importer` **and** no `BetterBlenderCOD` (§2.2); 18 `audit_/report_/diagnose_/render_/test_` tools plus `blender_asset_gen/`, `ui_asset_gen/` and `build_importer.py` ship to customers (§2.2); `.gitignore:89` ignores the importer and `git ls-files` returns 0 while `XMODEL_LOD_API_VERSION` exists only in an uncommitted nested clone (§8.1); `FriendsOfDutyMacBuild.cs:281` signs `--force --deep --sign -` (§8.3); `/Applications/Blender.app` already fails `codesign --verify --deep --strict` with added `__pycache__` files and `stapler validate` reports no ticket (§4.2); `--factory-startup` appears nowhere in `exporter/pipeline.py` (§6.1).

---

## 1. Status / Goal

Friends of Duty ships with zero Call of Duty assets. The player supplies their own lawfully-owned CoD1 + United Offensive install and the bundled Exporter converts it into a runtime `fodpak`. Today that conversion requires the player to independently install CPython 3.10+, Pillow, numpy and Blender 4.2+, and — because every bundled importer prebuilt is rejected by the production prop path — the Rust toolchain plus a platform C/C++ linker on **all four** targets. That is not a shippable onboarding experience, and on Windows it is not shippable at all: the current Windows depot payload contains no importer extension of any kind (`Builds/Windows/Exporter/tools/` has no `cod-asset-importer` directory — verified).

> ## SCOPE DECISION (owner, 2026-08-10): the exporter ships on WINDOWS ONLY, unsigned.
>
> macOS and Linux players use the remote-import path. This is sound rather than a concession, because **a fodpak carries no platform-specific data** — a package built on any Windows machine mounts unchanged everywhere, so a player without Windows can use one a friend built. The game already ships that import path; it stops being a fallback and becomes the supported route on two of three platforms.
>
> **What this retires outright**, and it is most of the hard part of this plan:
>
> | Retired | Why it is gone |
> |---|---|
> | Q1 macOS arch strategy | No macOS exporter. universal2 / arm64-only / Rosetta all moot. |
> | Q2 Apple Developer Program | No macOS exporter to notarize. **This was never about the exporter anyway** — see the note below. |
> | Q6 Windows signing service | Owner accepts an unsigned binary. No FIPS-hardware token, no cloud signing subscription. |
> | M0's notary spike | Nothing to notarize. |
> | Risk 5 (notarization at scale), risk 8c (no stapled ticket) | No macOS bundle. |
> | Risk 7 (Blender's X11 deps, Steam Linux Runtime, SteamOS immutable root) | No Linux exporter. This was one of only two "Unknown" risks. |
> | Risk 20 (Intel Macs lose the exporter) | Every non-Windows platform uses remote import by design. |
> | §4.2, §4.3 (macOS `.app` and Linux ELF layouts), the `ditto`/`hdiutil`/`codesign`/`spctl` machinery, `packaging/sign_macos.sh`, `entitlements.plist` | Not built. |
>
> **One correction, because it matters for what "unsigned" can mean.** On macOS this was never a user-approval question: Apple Silicon requires every executable to carry **at least an ad-hoc signature**, and an unsigned arm64 binary is SIGKILLed by the kernel — a restriction Apple documents as not bypassable by any user action. Ad-hoc signing is free and this repo already does it (`FriendsOfDutyMacBuild.cs:281`). Separately, Steam has required new macOS applications to be notarized since 2019-10-14, which is a Valve policy about **the game**, not about this tool, and it predates this work. Neither fact constrains a Windows-only exporter.
>
> On Windows unsigned is genuinely fine: SmartScreen's reputation prompt keys off Mark-of-the-Web, which Steam-delivered depot files do not carry, and the exporter is started by the game via `CreateProcess` rather than by Explorer — so the "Run anyway" dialog will typically never appear at all. **The real cost is not the prompt, it is antivirus heuristics** (risk 8a): an unsigned binary that downloads ~365 MB, extracts several thousand files including ~200 executables, and then spawns one of them is a textbook dropper shape, and a code signature is the main thing that buys reputation against that. That remains the argument for signing later; it is not an argument against shipping now.
>
> **What is still true and still required:** the Windows payload, the Blender provisioner, the importer, the progress protocol, resumability, and the fodpak contract. Milestones M1–M7 and M10–M16 are unchanged. M8 shrinks to one PyInstaller build on one runner with no signing step.
>
> **Kept deliberately cross-platform anyway:** the exporter's own Python. It costs nothing — the development machine is a Mac, every export in this project has been produced there, and `ExporterLauncher` already falls back to the repo's `exporter/` directory in the editor. "Ships on Windows" and "runs on the developer's machine" are different claims.

**Success criterion, stated once and used as the acceptance test for every milestone below:**

> A player installs Friends of Duty from Steam on Windows x64, Linux x64 (SteamOS, no Proton) or macOS, clicks exactly one thing, points a folder picker at their Call of Duty install, watches a determinate progress bar, and ends with a validated `fodpak` mounted by the game — **having installed nothing, chosen no versions, and visited no websites.**

**One honest qualifier on that criterion, because it is the consequence of the owner decision in §5.2.** The exporter does not *bundle* Blender; on first use it **downloads the pinned Blender 4.5.1 LTS build itself**, verifies it against a compiled-in SHA-256, and extracts it into a per-user directory. The player still installs nothing and makes no choices — but the first export requires a working internet connection and a ~350–390 MB one-time transfer that Steam does not deliver, mirror, resume or delta-patch on our behalf. Every "installs nothing" claim in this document is to be read as "installs nothing *by hand*", and §10 risks 5–8 exist precisely because that transfer is now ours to make reliable.

Two secondary criteria that are part of "done", not nice-to-haves:

- **The pak a player builds is the pak we tested.** Today `MIN_BLENDER = (4, 2)` (`exporter/friends_of_duty_exporter.py:39`) with no ceiling. A player on the declared minimum produces a 25,782,332-byte / 84,628-accessor `player.glb`; the shipped reference (generator `Khronos glTF Blender I/O v4.5.47`) is 21,111,852 bytes / 53,162 accessors / 351 animations. Both are "supported" today. That must stop.
- **Every native artifact in every depot must be correct for that depot's platform**, verified at build time by file magic, not at player-report time.

> **MEASURED 2026-08-10 — the reproducibility question is now partly answered, and better than expected.**
> A real export of the `viewmodels` step, run through the runtime-provisioned Blender 4.5.1, with `--factory-startup`, the scrubbed `child_env`, and the CI-built v3.6 importer, produced **all 39 viewmodel GLBs byte-identical** to `Content/Content/current` (same generator `Khronos glTF Blender I/O v4.5.47`, same sha256, 39/39). These files carry animations, so this is not a static-geometry-only result.
>
> Three things this settles empirically rather than by assumption: the pin is correct (4.5.1 *is* what produced the reference), `--factory-startup` does **not** move the oracle for this step (risk 17 is smaller than feared), and a CI-built importer produces identical geometry to a locally built one. §12 Q8 is answered for the Blender half.
>
> It does **not** generalise yet. `players` (4 profiles, 351 clips each), `props` (341 GLBs) and the map half are untested, and `tools/fod_export_common.py:245` still says evaluation is not bit-reproducible in general. Treat byte-equality as the observed result for viewmodels and the *aspiration* elsewhere; the M10 harness still needs its tolerance path for the cases that turn out to drift.

> **MEASURED 2026-08-11 on the Windows build machine — the oracle is PLATFORM-SPECIFIC, and this changes M10.**
> The same `viewmodels` export was run on Windows (Python 3.12.10, the pinned Blender 4.5.1 windows-x64, the CI-built `.pyd`) against the same Call of Duty install. **All 39 GLBs differ from the macOS-produced reference** — so the byte-identical result recorded above is *same-platform* reproducibility, not portability, and nothing in this plan may assume otherwise.
>
> Diagnosed rather than assumed. Node names, node order, node count, animation names, and per-animation sampler/channel counts are **identical**; `asset.generator` is identical (`Khronos glTF Blender I/O v4.5.47`). What differs is node TRS on 25–28 of 73 nodes, with worst-case absolute deltas of **1.8e-07 (rotation), 2.3e-10 (translation), 3.6e-07 (scale)** — at or below float32 epsilon (1.19e-07), on leaf bones like `bip01 l finger0nub` whose values are themselves ~1e-08. The eye-catching "relative" deltas are an artifact of dividing by near-zero.
>
> The accompanying **+6 accessors and +6 bufferViews** are a direct consequence, not a second defect: `tools/fod_export_common.py:324-341` monkeypatches `__append_unique_and_get_index`, which *is* the exporter's accessor-dedup path. Values that were bit-identical on macOS and collapsed into one accessor are no longer bit-identical on Windows, so six fewer dedup hits occur.
>
> **What this does and does not mean.**
> - It does **not** undermine the Windows-only decision. That rests on a fodpak being *loadable* everywhere, which is unaffected: a sub-float32-epsilon rotation on a fingertip bone is not observable in game, and the pak format carries nothing platform-specific.
> - It **does** mean the golden harness cannot be a `sha256` comparison against a pak built on another OS. Two consequences for M10, both now mandatory rather than optional: the oracle must be **regenerated on the shipping platform (Windows)** and committed as such, and the harness needs the structural-plus-tolerance comparison §11 M10 already specifies — exact on names/order/counts/material binding, tolerant on sampler and TRS floats, with **1.8e-07 absolute** as the measured floor rather than a guessed 1e-06 relative.
> - It confirms `tools/fod_export_common.py:245` ("Blender's evaluation is not bit-reproducible") is describing something real, and that the earlier same-platform byte-equality was fortunate rather than guaranteed.

> **M5 DONE — full 19-step export on the Windows build machine, 2026-08-11.**
> Produced a validated, atomically promoted package: **4,490 files / 804,277,590 bytes**, `fodpak.json` `gameContentVersion` 4, from the retail install at `E:\Games\Call Of Duty 1 & UO\Call of Duty` (14 CoD1 + 8 UO official pk3).
>
> **Measured wall clock — `maps` is no longer an unknown.** Total **1,240 s (20.7 min)**; `maps` 507.8 s (40.9%), `players` 419.1 s (33.8%), `viewmodels` 89.2 s, `extract_cod1` 61.0 s, `extract_mp_models` 51.4 s, `props` 37.3 s, `package` 25.3 s, everything else under 16 s. `packaging/step_weights.json` is now these numbers rather than estimates. The estimates were badly wrong in the ratios that matter: `props` was weighted equal to `viewmodels` and is in fact a twentieth of `maps`.
>
> **Equivalence against the reference pak: 4,283 of 4,490 byte-identical (95.4%).** Every one of the 207 differences was diagnosed, and none is a content defect:
>
> | Class | Count | Finding |
> |---|---:|---|
> | PNG textures (`maps/shared`, `fx`, `ui/hud`) | ~56 | **Pixel streams byte-identical** after inflate. Only the last ~7 bytes of the deflate stream differ — a zlib encoder difference between platforms. Chunk layout identical. |
> | Blender GLBs (`viewmodels` 39, `worldmodels` 11, props) | ~54 | Node TRS drift at float32-epsilon scale, as diagnosed above. Names, order, counts, animation structure identical. |
> | `scripts/mp/*.json` | 12 | Embeds `sha256` of the PNGs above. Same 88 entries, same paths, different hashes — purely downstream of the zlib difference. |
> | Remaining | ~85 | `maps/cod1`, `maps/uo` textures and a few audio files, same classes. |
>
> So the Windows-built package is **content-equivalent** to the macOS-built reference, with every difference confined to encoder-level noise. That is the strongest available evidence for the Windows-only shipping decision.
>
> **The 81-file gap is RESOLVED, and 73 of it was a defect worth having found.**
>
> My first characterisation of those 81 was wrong — they were not mostly ambience. **73 were weapon world-model, projectile and shell texture PNGs**, and they were missing because *the resume mechanism faithfully preserved broken output*. The importer's `_set_hashed_alpha` recursion bug had made every material import fail during an earlier run of `worldmodels`, `shellcasing` and `projectiles`; fixing it touched only `importer.py`, the native `.pyd` was unchanged, and five of the six importer-dependent steps hashed **only the native** in their toolchain key. Their markers stayed valid, the next run skipped all three, and the textureless output survived into the finished package. Only `props` happened to list `importer.py` in `signature_files`.
>
> `toolchain_for()` now includes `importerPythonSha256` for every importer step. Proven on the Windows box: with no `--force`, the rerun re-ran all six importer steps and **correctly skipped the 507-second `maps` step**, which is exactly the per-step behaviour §6.1 argued for over a global build stamp. The package went 4,490 → **4,563 files** and all 73 textures returned.
>
> **The remaining 8 are stale surplus in the reference, confirmed not a shortfall.** One is `maps/catalog.json.bak`, plainly a leftover. The other seven are ambience clips the current exporter does not produce *and does not reference*: five are referenced zero times anywhere in the new package, and the apparent `MG42_loop` reference turned out to be a substring match on `mg42_loop02.wav`, which **is** produced. The current exporter emits its own ambience set (`MG42_Cooldown.wav`, `mg42_loop02.wav`, …), every file referenced, **no dangling references anywhere**. The reference simply carries clips from an older exporter revision.
>
> **The committed oracle is therefore safe to regenerate from the Windows package**, and should be — it is the shipping platform, and §1's cross-platform finding means an oracle built anywhere else will never byte-match. Final state: 4,563 files, **4,356 byte-identical to the reference (95.5%)**, 0 files produced that the reference lacks, and every remaining difference accounted for as encoder-level noise.

**One qualifier on "the pak we tested", stated up front because two acceptance gates depend on it.** Blender's dependency-graph evaluation is *not* bit-reproducible, and the codebase says so at `tools/fod_export_common.py:245`: "Sample values still vary in the last float bits between runs, as they did before this patch — Blender's evaluation is not bit-reproducible," reinforced at `:378` where a re-export came out "structurally identical with fewer differing floats than two runs of the same code produce on their own." Pinning Blender therefore buys a **structurally reproducible** oracle — identical accessor counts, node names, joint order, material bindings, animation names — with byte-exact static geometry and bounded float drift in animation sampler outputs. Every acceptance gate in this document is written to that standard, not to `sha256`-of-everything. Any milestone whose definition of done says "hash-match" applies it only where hashes are actually stable (static meshes, texture PNGs, JSON manifests).

---

## 2. What exists today, and exactly why it fails

### 2.1 The dependency chain

A full export is **19 ordered steps** (`exporter/pipeline.py:1027-1396`, keys at `:1031`, `:1046`, `:1063`, `:1079`, `:1097`, `:1113`, `:1130`, `:1152`, `:1170`, `:1192`, `:1206`, `:1231`, `:1248`, `:1269`, `:1286`, `:1304`, `:1320`, `:1338`, `:1368`):

| Runner | Count | Steps | Mechanism |
|---|---|---|---|
| Host Python | **11** | `extract_cod1`, `extract_mp_models`, `stage_ordnance`, `presentation`, `footsteps`, `impacts`, `mp_scripts`, `map_previews`, `hud`, `ordnance`, `maps` | `_python(cfg, tool, *args)` → `[sys.executable, TOOLS_DIR/<x>.py, …]` (`pipeline.py:222-223`) |
| Blender | 6 | `viewmodels`, `worldmodels`, `shellcasing`, `projectiles`, `players`, `props` | `_blender(cfg, tool, *args)` → `[<blender>, --background, --python, TOOLS_DIR/<x>.py, --, …]` (`pipeline.py:226-232`) |
| In-process | 1 | `weapon_data` | `_copy_weapon_data` (`pipeline.py:957-1024`) |
| Host Python (package) | 1 | `package` | `[sys.executable, EXPORTER_DIR/package.py, …]` (`pipeline.py:1367-1376`, `sys.executable` at `:1371`) |

11 + 6 + 1 + 1 = 19. (The earlier draft said 12 host steps and summed to 20; corrected.)

Third-party surface splits cleanly:

- **Host interpreter** needs only **Pillow + numpy**. Top-level PIL at `tools/extract_cod1_mp_gsc_content.py:18`, `tools/extract_cod1_map_previews.py:28`, `tools/import_cod_multiplayer_maps.py:50`, `tools/fod_decal_alpha.py:18`, `tools/build_pavlov_sky_panorama.py:10` — five files inside the shipped closure, not three; lazy in four more (`extract_cod1_hud.py:172`, `extract_cod1_impacts.py:238`, `extract_cod1_ordnance.py:253`, `extract_cod1_weapon_presentation.py:165`). numpy at `tools/import_cod_multiplayer_maps.py:49`, `tools/fod_glb_writer.py:35`, `tools/fod_decal_alpha.py:17`, `tools/build_pavlov_sky_panorama.py:9`.
- **Blender's interpreter** needs `bpy`, `mathutils`, Blender's own `io_scene_gltf2` (monkey-patched at three *private* entry points: the dedup hook `_GlTF2Exporter__append_unique_and_get_index` named at `tools/fod_export_common.py:246` and installed at `:324-341`, the `gltf2_io.Animation.__init__` counter at `:347-358`, and `sampling_cache.get_range` replaced by `_patch_gltf_sampling_range` at `:365-411`), plus the compiled Rust `cod_asset_importer` abi3 extension, injected via `sys.path.insert` of an argv-supplied real directory (`tools/export_cod1_demo_viewmodels.py:38-43` and three siblings).
- **The only painful stdlib module is `tkinter`** (`friends_of_duty_exporter.py:353-354`, `:910`). No ssl, ctypes, multiprocessing, sqlite3, lzma, bz2 or zoneinfo anywhere. `zipfile`+`zlib` are load-bearing for pk3 reading in **10 payload files** (`cod1_archive_policy`, `cod1_script_exploder`, `extract_cod1_footsteps`, `extract_cod1_hud`, `extract_cod1_impacts`, `extract_cod1_ordnance`, `extract_cod1_weapon_presentation`, `import_cod_multiplayer_maps`, `exporter/package.py`, `exporter/build_importer.py`; an 11th, `extract_cod1_pavlov_entities`, is excluded from the payload). `wave` is used only by footsteps.

### 2.2 Why it fails the goal

**Blender is not present.** Six steps hard-fail without it (`pipeline.py:227-228`). Discovery is a hardcoded candidate list plus PATH with a 30 s `--version` probe (`friends_of_duty_exporter.py:58-109`). The game gates the EXTRACT button on `FodToolchainProbe.Ready` — Python 3.10+ **and** Blender 4.2+ found on the machine (`Assets/Scripts/Frontend/FodBootFlow.cs:1679`, `Assets/Scripts/Content/FodToolchainProbe.cs:135`).

**Every bundled importer prebuilt is LOD-API-0, and the depot payloads are cross-platform wrong.**

- ~~`tools/cod-asset-importer/` is gitignored; the v3.6 patch exists only as uncommitted working-tree state on one Mac.~~ **RESOLVED 2026-08-10 (M1).** The fork is published under GPLv3 at <https://github.com/anarqz/cod-asset-importer> (tag `v3.6.0`, a GitHub fork of `mauserzjeh/cod-asset-importer` so attribution is visible), and `tools/cod-asset-importer` is now a submodule of this repo. The gitignore entry is gone, `.gitmodules` carries the entry, and `MODIFICATIONS.md` plus per-file §5(a) notices record the changes. **Every clone, CI job and `SteamPipe/build_all.sh` must now pass `--recurse-submodules`.**
- **All five prebuilts in `tools/cod-asset-importer/release/` exist and are usable archives** — including a Windows `cod_asset_importer.pyd` (2,297,344 B) inside `cod_asset_importer_v3.5.0.zip` and a Linux arm64 `.abi3.so` (3,491,664 B) inside `cod_asset_importer_linux_arm64_v3.5.0.zip`. (The earlier draft claimed the Windows prebuilt "does not exist". It does; that claim is withdrawn.) What is true is that **every one is v3.5.0 with `XMODEL_LOD_API_VERSION` absent from the native** — verified by symbol scan across all five. `exporter/build_importer.py:236-248` therefore deletes and skips them under `require_lod`, then falls through to `_cargo_build` (`:261-340`), which raises a `BuildError` naming rustup, VS Build Tools, Xcode CLT or build-essential. So the prerequisite is a Rust toolchain on **all four** targets, not three.
- Production prop export refuses to run on API 0: `tools/export_cod_multiplayer_props.py:723-733` raises `SystemExit`. Both entry points request `require_lod=True` (`friends_of_duty_exporter.py:114`, `:298-301`).
- `Builds/Windows/Exporter/tools/` contains **no `cod-asset-importer` directory at all** (verified). `Builds/SteamOS/Exporter/tools/cod-asset-importer/python/cod_asset_importer/cod_asset_importer.abi3.so` is a **Mach-O 64-bit arm64** library shipped to Linux (verified by `file`). Cause: `FriendsOfDutyExporterPayload.CopyBesideBuild` (`Assets/Editor/FriendsOfDutyMacBuild.cs:556-607`) recursively mirrors the macOS build host's working tree into every platform's payload, and on that host the importer directory is gitignored and present only locally.
- Only macOS verifies its payload, and only after the copy (`FriendsOfDutyMacBuild.cs:332-347` inside `VerifyMacBundle` at `:307`, called from `:119` after the copy at `:114`). Windows and Linux run `VerifyWindowsBuild` / `VerifyLinuxBuild` *before* `CopyBesideBuild` (`FriendsOfDutyWindowsBuild.cs:70-71`, `FriendsOfDutySteamOsBuild.cs:82-83`) and assert nothing about it.
- `tests/test_build_importer.py:43` calls `build_importer.build(importer_root)` **without** `require_lod` from the helper `_install_bundled_extension` (`:18-46`), so `test_bundled_release_extensions_cover_common_desktop_hosts` (`:48-75`) passes green against binaries the production path rejects. CI cannot see the ship-blocking gap.
- `Docs/STEAM_RELEASE.md:87-89` says the payload "includes architecture-checked importer extensions for macOS arm64/x86_64, Windows x86_64, and Linux arm64/x86_64". The archives exist, but none is *usable* — all are LOD-API-0 and refused by production prop export — and the Windows depot ships none of them. The doc must say that.

**Dev-only tooling ships to every customer today.** `Builds/*/Exporter/tools/` contains 24 unreachable dev modules plus `blender_asset_gen/` and `ui_asset_gen/` (both verified present in the Windows and SteamOS payloads), plus `exporter/build_importer.py` and the five v3.5 release zips (6,614,931 B).

**Nothing here is packageable as-is.** There is no PyInstaller spec, no Nuitka config, no py2app, no AppImage recipe, no `.iss`, no `Info.plist`, and no `.github/` — `git ls-files` matches none of them. The Makefile has no packaging target; its only exporter-aware line is `test:` at `Makefile:106-107` (`python3 -m unittest discover -s tests`). `sys.executable` is used to spawn steps at 12 call sites plus the package step and would point at the bundle under any freezer.

**Progress is 19 coarse ticks.** `ProgressFn = Callable[[int,int,str,str], None]` (`pipeline.py:147`) fires once per step. The boot screen draws `DrawProgressBar(-1f, null)` (`FodBootFlow.cs:1839`) — indeterminate. The two dominant steps (`maps`, `props`) are single ticks that run for minutes.

**On Windows the in-game console is permanently empty.** `BuildStartInfo` (`ExporterLauncher.cs:616`) sets `UseShellExecute = true` with no redirection on the Windows branch (`:639`), so the capture handlers at `:698-702` and `:752-756` never arm and `s_status = line` at `:686` never fires.

---

## 3. Decision

### 3.1 The architecture

**Phase A — ship this.** One clickable artifact per platform sitting at the top of a payload directory the player never opens. The payload contains:

1. a **frozen CPython 3.13 onedir bundle** (PyInstaller) carrying the interpreter, stdlib, Tcl/Tk, numpy, Pillow, and — new, and load-bearing for the provisioner — `ssl`, `lzma` and a `certifi` CA bundle, plus a ~150-line launcher;
2. **no Blender.** Instead a **Blender provisioner** (`exporter/blender_provisioner.py`) that on first use downloads the pinned stock Blender 4.5.1 LTS build for the host platform from `download.blender.org`, verifies it against a SHA-256 compiled into the frozen binary, extracts it into a per-user directory outside the game install (§4.5), and thereafter invokes it as a child process over argv exactly as today;
3. the exporter's own Python (`exporter/*.py` and the **26-module reachable `tools/` closure**) as **loose, readable source files on disk**;
4. one **per-target `cod_asset_importer` v3.6 abi3 extension** with its LICENSE and complete corresponding source, as loose files;
5. `LICENSES/` and a `payload.json` build stamp.

**OWNER DECISION (2026-08-10): Blender is fetched at runtime, not conveyed.** The alternatives considered and rejected are recorded in §3.3. What this buys, and it is not marginal:

- **The depot payload drops from ~1.00 GB to ~66-68 MB per platform** — under half the current Windows game (147 MB (m)) rather than ~7× it.
- **We never convey Blender**, so no GPLv3 distribution obligation for it attaches to us at all (§9.1). The GPL surface shrinks to the `cod_asset_importer` fork and the `bpy`-importing `tools/*.py`, both of which we were conveying anyway.
- **The single highest-uncertainty engineering task in the plan disappears.** macOS notarization no longer has to succeed over ~200 nested Blender-Foundation-signed Mach-O files in an ~870 MB bundle; it is an ordinary ~65 MB app. Risk 5 collapses, risk 6 (Blender writing `.pyc` into our signed bundle and breaking the seal) becomes structurally impossible because Blender no longer lives inside our bundle, and the entire M9 strip milestone and its GPLv3-§5(a)/trademark question (old Q4) are deleted rather than deferred.

What it costs is a **network dependency on the first export** and a download we now own end-to-end. That is a real transfer of risk from "engineering we control" to "the player's connection and blender.org's availability", and §5.2 and §10 are written to take it seriously rather than wave at it.

**Every pipeline step remains a subprocess.** Host steps re-exec the frozen launcher as `argv[0] --fod-run-tool <abs path to tools/x.py> …`, which restores `sys.argv`, inserts the tool's directory at `sys.path[0]`, and calls `runpy.run_path(tool, run_name="__main__")`. Blender steps invoke the bundled Blender with `--background --factory-startup --python <abs path to tools/x.py> -- …`. Nothing on the player's machine is read, probed, installed, version-negotiated or required.

**Phase B — fund it now, land it incrementally.** Replace the six Blender steps with a direct GLB writer built on the Rust importer's already-exposed vertex/bone/weight API, one step at a time, each gated on a golden harness whose oracle is Phase A's own pinned-Blender output. When all six are green, **delete the provisioner** — and with it the network dependency, the ~390 MB first-run download, the ~1 GB of per-user disk, and every risk in §10 rows 5–8. The payload itself barely moves (~65 MB → ~60 MB); what Phase B buys under this decision is not size but **the removal of the only step in the export that can fail for reasons outside the player's machine**.

The two phases are mutually reinforcing, and that is the whole point of choosing this combination:

- **Phase A is what makes Phase B safe.** You cannot validate a from-scratch rewrite against a reference you cannot reproduce. Pinning Blender 4.5.1 — by SHA-256, whether it is bundled or fetched — gives every developer and every CI runner a *structurally* reproducible oracle: identical accessor and bufferView counts, node and joint names and order, primitive→material binding, animation names and sampler counts, and the exact LOD file set. Static geometry is byte-exact; animation sampler outputs carry a measured last-bits float delta that the harness treats as the tolerance floor (§3.3, M10). **The pin does the work here, not the bundling** — which is precisely why fetching at runtime costs nothing in reproducibility.
- **Phase B is what makes Phase A's first-run experience acceptable.** A ~390 MB download before the export can start is the price of shipping in a quarter instead of two. It is explicitly temporary and scheduled — subject to the fallback recorded in §12 Q3 for the case where Phase B's M13 gate fails, which under this decision means the download is permanent rather than a permanent ~1 GB payload.

### 3.2 Why "one clickable artifact", not "one file"

`Exporter/` is already a directory in every depot (`Docs/STEAM_RELEASE.md:82-92`). The player still clicks exactly one thing. Pursuing a literal single file costs:

- **SteamPipe delta patching — the strongest of the three arguments.** SteamPipe chunks at ~1 MB; a monolithic blob means every exporter code fix is a full multi-hundred-MB re-download on all three platforms, forever. A onedir tree means a `.py` fix re-downloads a few hundred KB.
- a full payload re-extraction to a fresh temp directory on **every** launch (PyInstaller onefile; requests for a persistent cache are open and unresolved: pyinstaller#1782, #4994, #7907). **This argument is much weaker under the runtime-fetch decision** — extracting ~65 MB costs a second or two, not the minutes a ~1 GB payload would have — and it is recorded as weakened rather than quietly dropped. It still argues against onefile, because the exporter re-execs *itself* once per host step (§6.1), so a onefile build pays that extraction 12 times per export, not once;
- a documented Defender/SmartScreen false-positive profile (pyinstaller#6754) with no user-side remedy after install.

**Correction to the earlier draft, recorded so the objection cannot recur.** The draft additionally claimed onefile "breaks `prop_export_fingerprint`, which globs its own directory and would re-export all 239 props on every run". That is **false**, and it is worth being explicit about why, because the same false premise appeared in §5.3 and §8.2. `tools/export_cod_multiplayer_props.py:148-203` builds its hashed payload from `{name, sha256}` pairs where `name` is `"source/" + path.relative_to(source_root)` when the file is under `source_root` and otherwise the bare `path.name` (`:176-186`). **No absolute path ever enters the hashed payload.** A changing extraction directory therefore does not change the fingerprint. The argument is deleted; the three real costs above carry the decision on their own.

### 3.3 Rejected alternatives

**`import bpy` in the exporter's own process (the MONOLITH proposal).** Rejected on four independent grounds, any one of which is sufficient:

- *Licensing.* `bpy` is published GPL-3.0. The FSF's stated criterion is that modules in the same executable file are definitely one program, while argv is normally two separate programs. In-process `bpy` makes the entire frozen exporter a GPLv3 work permanently, foreclosing an option the project owner has not yet been asked about. Out-of-process Blender is the FSF's own textbook aggregation case.
- *Tooling.* Neither PyInstaller core nor `pyinstaller-hooks-contrib` ships a `bpy` hook. Nuitka's fix (issue #2880, "Blender bpy package compiles but does not run") closed 2026-05-11 and was demonstrated only on Linux/CPython 3.10. Every artifact in that plan sits downstream of a hook nobody has written for a ~600 MB package with a nontrivial RPATH graph.
- *Correctness.* That plan's core mechanism, `importlib.import_module(name)` then `mod.main()`, **does not work on the viewmodels step**. `tools/export_cod1_demo_viewmodels.py` has no `main()`: `def args()` is at `:25`, the seven argv-derived globals are bound at module scope at `:37`, `sys.path` is mutated at `:38-40`, and the export executes at `:864-873`. 28 of the 50 tools have no `def main`. `tools/export_cod1_multiplayer_players.py:41` has the same shape, so even where a `main()` exists a second in-process invocation silently reuses the first call's argv. The `--fod-run-tool` + `runpy.run_path` mechanism chosen here handles all of them with zero tool source changes, which is exactly why it was chosen.
- *Execution model.* Blender documents that `bpy`-as-module cannot be reloaded, edits one `.blend` at a time, installs no signal handlers and writes no crash log. A locally installed `bpy` wheel segfaulted at interpreter teardown *after* writing correct output (record the exact wheel filename in the ADR — the earlier draft's "`bpy 4.2.23`" is not a version that exists in `bpy`'s PyPI numbering and is withdrawn); `pipeline.py:1443` treats any nonzero return code as a step failure. Losing per-step process isolation for six steps that each mutate `sys.path` at import is a live silent-wrong-output mechanism, not a theoretical one.

**Bundling `bpy` but running it in a re-exec'd child.** Saves ~200-400 MB per platform over a stock Blender and keeps process isolation, but the frozen binary still *contains* GPL Blender, so the licensing position is identical to in-process `bpy`, and the PyInstaller hook risk is unchanged. Not worth it.

**Converting host steps to in-process function calls.** Rejected for the same reason as above (28 tools have no `main()`), and because it would break `_step_signature`'s per-file hashing and eliminate the stdout/cancel plumbing that already works.

**AppImage on Linux.** Rejected. Type-2 AppImages require libfuse2 at runtime; SteamOS 3.x ships fuse3, and the exporter is spawned by a game process Steam may have started inside pressure-vessel. The documented fallback `--appimage-extract-and-run` unpacks the entire payload on every launch — precisely the onefile failure mode. Ship a plain ELF beside `_internal/`.

**Bundling Blender in the base depot (the original recommendation).** Rejected by owner decision. It is the most *robust* delivery — Steam mirrors it, resumes it, delta-patches it, and works offline after install — and it is the only option with no runtime failure mode outside the player's machine. It costs ~230–290 MB of first download for **every** player whether they ever extract or not, ~2.82 GB of new bytes to build, sign and upload per release, an ~870 MB macOS notarization submission spanning ~200 third-party signed Mach-O files whose acceptance is undocumented, and a live GPLv3 conveyance obligation. Recorded here in full because it remains the fallback if the provisioner proves unreliable in M8 acceptance: reverting to it is a packaging change, not an architecture change, since `_blender()` resolves a path either way.

**Shipping the exporter as a free, optional DLC depot with Blender still bundled.** Verified feasible against Steamworks documentation — a DLC can be flagged "Disable Steam automatically downloading this DLC", free DLC is supported, and the game triggers the transfer itself with `ISteamApps::InstallDLC` / `BIsDlcInstalled` / `GetDlcDownloadProgress`, with such downloads prioritized in the queue. This keeps the base game at 147 MB while retaining Steam's mirroring, resume and delta patching, and the player still chooses nothing. Rejected in favour of the runtime fetch, which achieves the same base-install size with no new DLC AppID, no depot re-mapping and no Steamworks plumbing — at the cost of owning the download ourselves. **This is the strongest of the rejected options and is the first thing to reconsider if §10 risks 5–8 prove worse in practice than estimated**, since it solves every one of them at once. (One config note if it is ever revisited: DLC depots live in the base app's depot list, not under the DLC AppID.)

**Requiring a player-installed Blender.** Rejected. It sounds like the smallest option and is the worst: blender.org's download button now serves **5.2 LTS** (2026-07-14), so "install Blender" in practice means "find the LTS archive, deliberately choose 4.5 rather than 5.2, and prevent it updating" — and `find_blender()` (`friends_of_duty_exporter.py:97-109`) returns on the *first* candidate at or above `MIN_BLENDER`, so a 5.2 in `/Applications` silently masks a valid 4.5. Version drift is then a correctness problem, not a UX nuisance: 4.2 and 4.5 on identical input produce a 25,782,332-byte / 84,628-accessor `player.glb` versus 21,111,852 / 53,162, and both are "supported" by today's `MIN_BLENDER = (4, 2)`. Accepting whatever the player has means shipping N content packages having tested one, which destroys the oracle Phase B is validated against; pinning hard instead returns to the awkward instruction. It also makes the player download *more* (Blender 5.2 is 348 MB Windows / 367 MB Linux, un-resumed and un-mirrored by us), keeps the "go install two things" screen whose deletion is the largest player-visible win in §6.5, and ends Steam Deck support — Gaming Mode cannot install Blender, and a Desktop-Mode Flatpak Blender is sandboxed away from the CoD directory.

In fairness to the option, one concern against it did **not** survive checking: both private `io_scene_gltf2` internals the pipeline monkey-patches — `GlTF2Exporter.__append_unique_and_get_index` and `sampling_cache.get_range` — still exist on `glTF-Blender-IO` `main`, so the patch *targets* are more durable across major versions than expected. The objection that stands is output identity across 4.5 → 5.2, which is untested and is the property that matters.

**Stripping the Blender tree.** Moot and deleted, along with the entire M9 milestone and old Q4. We do not convey Blender, so there is no modified-GPLv3 §5(a) obligation, no Blender Foundation trademark question for modified builds, no `codesign --remove-signature` → delete → re-sign → re-notarize sequence, and no conflict with `FriendsOfDutyMacBuild.cs:351`'s existing `--deep --strict` gate. The provisioner extracts the official archive verbatim; ~1 GB in a per-user cache directory is not worth engineering against, and stripping it would only re-open the questions the runtime fetch just closed.

**macOS universal in Phase A.** Under the runtime-fetch decision this is no longer expensive — two frozen shells, ~+62 MB, not two Blender trees at +802 MB. Still provisionally rejected, but now on CI cost and the deprecating `macos-15-intel` runner class rather than payload size; §12 Q1 reopens it on those narrower grounds.

**x86_64-only under Rosetta 2.** Rejected: Rosetta prompts an install (violates "installs nothing") and Blender under emulation on a ~7-minute Blender workload is unacceptable.

**Blender-free rewrite as the v1 plan (the Direct Path proposal, adopted as Phase B).** Its static-prop prototype is real and reproducible (`props=239 bit-exact=226 differ=0 skinned-skipped=13 wall=0.6s` against Blender's 16.7 s), and its end state is 18× smaller. It is rejected as *v1* because:

- The skinned/animated half — 43 GLBs carrying 114,004,776 B, every weapon the player holds and every soldier they see — has never been built. `tools/fod_glb_writer.py` is 237 lines writing single-node static meshes.
- Its texture claim does not survive measurement. Decoding 60 prop source DDS files with Pillow and comparing against the shipped Blender-produced PNGs: **0 of 60 pixel-identical**, all differing by 1-2 levels per channel (e.g. `wood@panzerfaust_box` maxdelta 2, mean 0.227). Substituting a Pillow decoder changes all 811 model PNGs, and the proposed golden harness — sorted POSITION triangle corners at 1e-3 tolerance — cannot see it.
- Its headline "props SOLVED" measures LOD0 geometry only. The shipped props step produces 341 GLBs (57 props carry `lod1.glb`, 45 carry `lod2.glb`), every per-prop texture, and `props.json`'s material/texture/cutout table plus an `exportFingerprint`. `exporter/package.py:607` opens `validate_prop_lods(`, which enforces LOD0-glb identity, non-empty `sourceSurface` (`:650-652`) and strictly increasing distances. Node naming is also unaddressed: shipped props are one node per surface with Blender's `.001`/`.002` collision suffixes and `Assets/Scripts/Content/FodAuthoredPropCatalog.cs:89` imports with `NameImportMethod.OriginalUnique`.
- It does not meet the success criterion until week 10-12; until then it still tells the player to install Blender.

None of that makes it wrong — it makes it Phase B, with a harness that actually covers what it needs to cover. See §11.

---

## 4. Shipped artifact layout

Under the runtime-fetch decision the depot payload is **the same on every platform apart from the frozen shell and one native extension**. Blender appears nowhere in any depot; §4.5 describes where it lands instead.

### 4.1 Windows x64 — `Builds/Windows/Exporter/` — ~68 MB installed (e)

```
Exporter/
├── FriendsOfDutyExporter.exe          ~1.3 MB (e)  PyInstaller onedir stub, Authenticode-signed
├── friends_of_duty_exporter.py        ~35 KB       real source; also a discovery-probe entry
├── pipeline.py  package.py  fod_launcher.py  fod_paths.py  build_stamp.py  progress.py
│   cod_autodetect.py  blender_provisioner.py
├── run_exporter.bat                                blocking shim, kept for one release
├── _internal/                         ~62 MB (e)
│   ├── python313.dll  python3.dll  vcruntime140.dll  base_library.zip
│   ├── numpy/  PIL/  tcl8.6/  tk8.6/  _tkinter.pyd  lib-dynload equivalents
│   ├── _ssl.pyd  libcrypto-3.dll  libssl-3.dll  certifi/cacert.pem   ← provisioner (§5.2)
│   └── _lzma.pyd  liblzma.dll                                        ← Linux tar.xz only, but
│                                                                       shipped everywhere so the
│                                                                       spec is one file
├── tools/                             ~4 MB
│   ├── <26-module manifest from packaging/tools_manifest.txt>.py
│   └── cod-asset-importer/
│       ├── LICENSE  PROVENANCE.md  MODIFICATIONS.md
│       ├── python/cod_asset_importer/{*.py, cod_asset_importer.pyd}   v3.6.0, LOD API 1, win-amd64
│       └── rust/                                   GPLv3 Corresponding Source (incl. valid_enum)
├── LICENSES/
└── payload.json                                    includes blenderPin (url, sha256, version)
```

**Removed from the payload relative to today** (see §8.4's post-install assertion): `build_importer.py`, `tools/cod-asset-importer/release/*.zip` (6,614,931 B), `tools/blender_asset_gen/` (84 KB), `tools/ui_asset_gen/` (152 KB), and the 24 unreachable dev tools.

### 4.2 macOS arm64 — `Builds/macOS/Exporter/` — ~66 MB installed (e)

```
Exporter/
├── Friends of Duty Exporter.app/                    ← THE clickable artifact
│   └── Contents/
│       ├── Info.plist                               CFBundleExecutable=FriendsOfDutyExporter,
│       │                                            LSMinimumSystemVersion 11.0
│       ├── MacOS/FriendsOfDutyExporter   ~1.35 MB (e)  universal2: arm64 slice is the PyInstaller
│       │                                            onedir bootloader (~1.3 MB); x86_64 slice is a
│       │                                            ~20 KB stub that shows a dialog pointing Intel
│       │                                            players at the remote-import path.
│       │                                            Our Developer ID, hardened runtime.
│       ├── Frameworks/                   ~62 MB (e)   CPython 3.13, numpy, Pillow, Tcl/Tk,
│       │                                              OpenSSL + certifi, liblzma
│       └── Resources/fod.icns
├── friends_of_duty_exporter.py  pipeline.py  package.py  fod_launcher.py  fod_paths.py
│   build_stamp.py  progress.py  cod_autodetect.py  blender_provisioner.py
├── run_exporter.command                             shim, one release
├── tools/                                           cod_asset_importer.abi3.so, macos-arm64
├── LICENSES/
└── payload.json
```

**What the runtime-fetch decision removed from this section, and it is the single largest simplification in the plan.** The bundled-Blender design nested an 802 MB (m) stock `Blender.app` inside `Contents/Resources/`, and three separate measured hazards came with it, all of which are now structurally impossible rather than mitigated:

1. **Notarization scale.** The submission was ~870 MB spanning 204 Mach-O files (m), ~200 of them third-party. It is now an ordinary ~66 MB app containing only binaries we built and signed. The M0 notary spike shrinks accordingly but is **not** deleted — see §8.3.
2. **The nested app carried no stapled ticket.** Measured: `xcrun stapler validate /Applications/Blender.app` → *"Blender.app does not have a ticket stapled to it."* Blender Foundation notarizes and staples the **DMG**, not the app; copying the app out leaves no ticket. That mattered only because we were conveying it. It does not matter for a runtime download either, because we never staple it and never submit it — Gatekeeper assesses the fetched Blender on its own Developer ID signature, which is intact. **This does need confirming on a clean machine (M0), because it is now the mechanism by which the fetched Blender is allowed to run at all.**
3. **Blender writing into its own signed bundle.** Measured: after a local run, `codesign --verify --deep --strict /Applications/Blender.app` reports *"a sealed resource is missing or invalid — file added: …/python/lib/python3.11/__pycache__/wave.cpython-311.pyc"*, and `spctl -a -vvv -t exec` fails. Inside our bundle that would have broken our own `FriendsOfDutyMacBuild.cs:351` gate and mutated the delivered app on the player's first export. In a per-user cache directory it breaks nothing — but `child_env(kind='blender')` still sets `PYTHONDONTWRITEBYTECODE=1` (§6.2), because a `spctl`-failing Blender in the cache would still be refused on some configurations, and re-running `spctl` is part of §5.2's integrity check.

`Contents/Frameworks` is hand-assembled from PyInstaller's onedir output — **not** via PyInstaller's `BUNDLE`, which creates symlinks between `Contents/Frameworks` and `Contents/Resources`. SteamPipe does not preserve symlinks, and a dereferenced symlink inside a signed bundle breaks the sealed signature and therefore Gatekeeper on the *delivered* copy. The build asserts zero symlinks in the produced `.app`.

macOS Phase A is **arm64-only** for the working exporter. Phase B restores Intel. See §12 Q1.

### 4.3 Linux x64 — `Builds/SteamOS/Exporter/` — ~66 MB installed (e)

```
Exporter/
├── FriendsOfDutyExporter              ~1.3 MB (e)  ELF x86_64, exec bit set via depot FileProperties
├── friends_of_duty_exporter.py  pipeline.py  package.py  fod_launcher.py  fod_paths.py
│   build_stamp.py  progress.py  cod_autodetect.py  blender_provisioner.py
├── run_exporter.sh                                 shim, one release
├── _internal/                         ~62 MB (e)   incl. _ssl, OpenSSL, certifi, _lzma
├── tools/                                          cod_asset_importer.abi3.so, ELF x86_64
├── LICENSES/
└── payload.json
```

Not an AppImage — see §3.3.

### 4.4 Where the fetched Blender lives

**Never inside the game install, and never inside our signed bundle.** Two independent reasons: writing into `Friends of Duty Exporter.app` invalidates its code signature (§4.2 hazard 3), and any file beside the game that Steam's manifest does not know about is a "Verify integrity of game files" hazard — the four `"FileExclusion" "Content/*"` lines cover the pak, and nothing covers a Blender tree.

| Platform | Path |
|---|---|
| Windows | `%LOCALAPPDATA%\Friends of Duty\Blender\4.5.1\` |
| macOS | `~/Library/Application Support/Friends of Duty/Blender/4.5.1/Blender.app` |
| Linux | `$XDG_DATA_HOME` (default `~/.local/share`)`/Friends of Duty/Blender/4.5.1/` |

Version-keyed by design: a future pin bump downloads beside the old tree rather than over it, so a failed migration cannot leave a half-replaced Blender, and the old one is deleted only after the new one passes its integrity check. `--blender <path>` and the GUI's Browse row (§6.3) point at any of this, which is also the offline escape hatch.

Resolution order at runtime, first hit wins: `--blender` argument → `FOD_BLENDER` environment variable → the version-keyed cache above → **fetch**. There is deliberately no PATH lookup and no `/Applications` scan in the frozen build: silently picking up a player's Blender 5.2 is exactly the failure §3.3 rejects.

### 4.5 Size accounting

**Install size is not download size, and no player downloads three depots.** Both errors were in an earlier draft's headline; both are corrected here.

| | today | Phase A (runtime fetch) | Phase A, bundled (rejected) | Phase B end state |
|---|---:|---:|---:|---:|
| macOS `Exporter/` installed | 11 MB (m) | **~66 MB** | ~869 MB | ~60 MB |
| Windows `Exporter/` installed | 1.4 MB (m, **broken**) | **~68 MB** | ~1.00 GB | ~60 MB |
| Linux `Exporter/` installed | 11 MB (m, **broken**) | **~66 MB** | ~950 MB | ~58 MB |
| **first-download transfer, per platform** (26–31% of install, measured) | — | **~20 MB** | ~230–290 MB | ~18 MB |
| all three depots, summed (built/signed/uploaded per release) | ~23 MB | **~200 MB** | ~2.82 GB | ~178 MB |
| **first-export network transfer, one time, per player** | 0 | **~350–390 MB** (m, from blender.org) | 0 | 0 |
| per-user disk for the extracted Blender | 0 | **~800 MB–1.05 GB** (e; 802 MB measured on macOS arm64) | 0 (it is in the install) | 0 |

The row that matters is the last two: **the bytes did not disappear, they moved from Steam's delivery network to blender.org's and from the install to a per-user cache**, and they are now paid only by players who actually extract. That is the whole trade, stated plainly.

Compression measurements retained because they still bound the *bundled* fallback in §3.3: `/Applications/Blender.app` = 802 MB on disk, `tar` = 845,282,304 B, `zstd -3` = 264,482,718 B (**31.3%**); a SteamPipe-shaped simulation (200 × 1 MiB chunks LZMA'd independently) yields **26.1%** versus 24.9% for whole-stream `xz -6`.

Comparison to the game: `du -sh Builds/Windows` = **147 MB** (m), so the exporter payload is now **under half the Windows game** rather than ~7× it.

**Disk during an export, and where the pak actually lands.** Peak is 810 MB live package (m) + 810 MB `.current.exporting` seed + 131 MB staging (m) ≈ **1.75 GB**, unchanged by this plan — but the extracted Blender now adds ~800 MB–1.05 GB in the user profile, which on Windows is frequently the same volume as `%LOCALAPPDATA%` and on a Steam Deck is the internal drive even when the game is on microSD. `Assets/Scripts/Content/FodContentPaths.cs:88-107` makes `PreferredInstallDir` the **portable** directory beside the executable whenever it is writable, and `PreferredMountDir` is `PreferredInstallDir/current` (`:114-115`). Milestone 6's precheck must therefore stat **two** volumes — the output volume (peak + 15% headroom) and the Blender cache volume (~1.1 GB) — and name each in its error.

Separately: all four depot vdfs carry `"FileExclusion" "Content/*"`, so the pak sits in the install directory as a file Steam's manifest does not know about. "Verify integrity of game files" against a populated `Content/current` has never been tested and is now a much more expensive thing to lose. It becomes an M8 acceptance item.


## 5. How each dependency is satisfied

### 5.1 Python

**Bundled:** CPython 3.13.x, frozen with PyInstaller 6.x in **onedir** mode, on a `python-build-standalone` interpreter pinned by SHA-256 so the CI runner's system Python is irrelevant. **The chosen python-build-standalone variant must be one that actually carries Tk** — the `manylinux_2_28` build container has no system Tcl/Tk, and `_tkinter` is not optional for us. Record the exact release tag and variant in `packaging/blender_pin.json`'s sibling `packaging/python_pin.toml`.

Because Blender is out-of-process with its own bundled CPython 3.11 (verified: `Blender.app/Contents/Resources/4.5/python/bin/python3.11`), our host interpreter version is free — the `bpy` wheel's interpreter constraint does not apply.

**Version pinning is not cosmetic.** The current reference pak was produced on this machine's host Python (3.13.5 (m)) with numpy 2.4.3 (m) and Pillow 12.1.1 (m). Pin exactly those, and gate acceptance on hash-diffing the `maps` step output — `tools/import_cod_multiplayer_maps.py` is 229 KB of numpy-heavy BSP/PVS/lightmap work producing 1,663 GLBs / 391 MB, and numpy 2.x integer-promotion changes are exactly the class of thing that would silently move bytes. If the diff is nonzero, fall back to CPython 3.11 + numpy 1.26.4 and re-diff.

Note that the `maps` step is a **host numpy step, not a Blender step** — all 1,663 map GLBs carry generator `FriendsOfDuty fod_glb_writer`, while exactly 429 of the pak's 2,092 GLBs (m) carry `Khronos glTF Blender I/O v4.5.47`. The numpy pin and the Blender pin are therefore *independent* risks needing *independent* gates (§11 M4 and M5).

**Found at runtime:** it *is* the runtime. `sys.executable` never means "an interpreter" again — see §6.1.

**`tkinter`:** kept. The GUI is ~480 lines with no custom drawing across four frames — `RequirementsFrame` (`:399`), `PathsFrame` (`:566`), `ExportFrame` (`:648`), `DoneFrame` (`:768`) — PyInstaller's Tcl/Tk hooks are mature on all three platforms, and this project deletes **one of those four** (`RequirementsFrame`, along with the pip button, the Prepare-importer button and the Blender-path row inside it). The earlier draft said "two of four"; corrected, because a reader weighing "keep tkinter" deserves the real simplification, not an inflated one. Rewriting the GUI is real risk for no shipping benefit. Revisit after Phase B.

**Three modules are NEW to the frozen surface, and they exist solely for the provisioner (§5.2).** Today's codebase uses none of them (§2.1 records "no ssl, ctypes, multiprocessing, sqlite3, lzma, bz2 or zoneinfo anywhere"), so each is a fresh PyInstaller hook risk rather than something already exercised:

- **`ssl`** — plus OpenSSL (`libssl-3.dll`/`libcrypto-3.dll`, `libssl.so.3`, or the macOS equivalents). Required to fetch over HTTPS.
- **`certifi`** — a bundled CA bundle, pinned in the lockfile. Do **not** rely on the platform trust store: a frozen CPython on macOS does not find system roots without the `Install Certificates` step that only the python.org installer performs, and the failure mode is a `CERTIFICATE_VERIFY_FAILED` on first export with no useful message. Windows can additionally load the system store, which matters for TLS-intercepting corporate proxies — the provisioner tries certifi first and falls back to `load_default_certs()` with a logged warning rather than failing outright.
- **`lzma`** (plus `_lzma`/`liblzma`) — for the Linux `tar.xz`. Shipped on all three platforms so the spec stays one file; the smoke test asserts `import lzma` everywhere.

`urllib.request` is preferred over adding `requests`: it is stdlib, it supports the `Range` header the resume needs, and it avoids pulling a third-party HTTP stack and its own vendored CA handling into the payload.

**`hiddenimports` must be generated, never hand-maintained.** Because every tool is a loose file executed via `runpy`, PyInstaller analyses only `fod_launcher.py`, so the *entire* stdlib and third-party surface of the loose sources must be declared. An AST scan of `tools/*.py` + `exporter/*.py` finds, beyond the earlier draft's list: `csv`, `queue`, `datetime`, `struct`, `binascii`, `hashlib`, `base64`, `platform`, `tempfile`, `shutil`, `glob`, `argparse`, `dataclasses`, `subprocess`, `threading`, `contextlib`, `itertools`, `typing`. The draft's list also named `PIL.ImageOps`, which nothing imports — the actual PIL surface is exactly `Image`, `ImageDraw`, `ImageFilter`. Therefore:

- `packaging/build_exporter.py` runs every module in `packaging/tools_manifest.txt` under `modulefinder`, unions the top-level names, and **fails the build** if any name is absent from the spec's `hiddenimports`.
- Pillow's codecs are discovered at runtime by scanning `PIL/*ImagePlugin.py`. A frozen PIL missing `DdsImagePlugin`/`TgaImagePlugin` passes `import PIL` and then fails at step 17 of 19, minutes into a player's export. The smoke test therefore decodes a synthetic DXT1 `.dds`, an uncompressed type-2 `.tga`, a `.jpg` and a `.png` through `PIL.Image.open` and asserts `set(PIL.Image.ID) >= {"DDS","TGA","JPEG","PNG"}` and that `numpy.__version__` matches the lockfile (§8.2 step 7).

### 5.2 Blender — fetched at runtime, never conveyed

**Not bundled.** `exporter/blender_provisioner.py` (new, M6) acquires the pinned **stock, unmodified Blender 4.5.1 LTS** build for the host platform on first use and caches it per-user (§4.4). Nothing about the *pin* changes relative to the bundled design — the version, the SHA-256 and the reproducibility argument are identical; only the moment and the machine on which the archive is fetched change.

**The pin is compiled in, not a loose file.** `packaging/blender_pin.json` remains the checked-in, human-reviewable source of truth and records per platform: `url`, `sha256`, `archive_bytes`, `extracted_bytes`, and `gltf_generator` (`Khronos glTF Blender I/O v4.5.47`). `packaging/build_exporter.py` bakes it into `exporter/build_stamp.py` and mirrors it into `payload.json`. It is deliberately **not** shipped as an editable file: a loose pin is a supply-chain hole — anything that can rewrite it can point the exporter at an arbitrary archive and choose the hash it is checked against. `make blender-pin VERSION=4.5.x` regenerates the toml from the checksums blender.org publishes; the change is reviewed as a normal diff, and bumping it requires a full M8 smoke pass plus an M10-harness re-run, because the generator string is part of the pak's identity.

**Archive URLs and checksums — CONFIRMED, and `packaging/blender_pin.json` is written.** All four artifacts exist and their digests come from Blender's own published checksum file:

```
https://download.blender.org/release/Blender4.5/blender-4.5.1-windows-x64.zip   399,708,581 B
https://download.blender.org/release/Blender4.5/blender-4.5.1-macos-arm64.dmg   309,333,913 B
https://download.blender.org/release/Blender4.5/blender-4.5.1-macos-x64.dmg     335,762,279 B
https://download.blender.org/release/Blender4.5/blender-4.5.1-linux-x64.tar.xz  375,503,560 B
```

**Two corrections to what this document assumed.** (a) There are **no per-artifact `.sha256` siblings** — requesting one returns 404. Checksums live in a single per-release file, `blender-4.5.1.sha256`, listing every artifact for that release; `make blender-pin` must parse that. (b) The 4.5 LTS line has reached **4.5.12**, so 4.5.1 is eleven patches behind within its own LTS series. That is deliberate — 4.5.1 is the build that produced the reference package — but it means risk 5 is about a *specific old patch* remaining published, not about the 4.5 directory as a whole. Still unconfirmed: blender.org's retention policy for individual patch releases after an LTS line reaches EOL. **Never hand-compute the SHA-256.**

**Acquisition, in order, all of it in `blender_provisioner.py`:**

1. **Resolve** per §4.4 — `--blender`, `FOD_BLENDER`, the version-keyed cache, then fetch. A cache hit whose `.fod_blender_ok` stamp records the pinned archive SHA-256 skips straight to use.
2. **Download** over HTTPS to `<cache>/.tmp/<archive>.part`, with an HTTP `Range` resume on restart, three retries with exponential backoff, and a total-bytes/received-bytes progress callback wired into the `@fod` protocol (§7.2) so the game's boot screen and the Tk window both show a real bar. **This adds `ssl` to the frozen surface**, which today's codebase does not use anywhere (§2.1) — see §5.1 for what that pulls in.
3. **Verify** the SHA-256 against the compiled-in pin **before** touching the extractor. A mismatch is a hard failure that names the expected and actual digests and deletes the partial file; it is never retried silently, because the two causes are a corrupted transfer and a substituted archive, and the second must be loud.
4. **Extract** into `<cache>/4.5.1.incoming/`, per platform:
   - **Windows** — `zipfile` (stdlib, already required for pk3 reading).
   - **Linux** — `tarfile` + `lzma`. `lzma` is **new** to the frozen surface; PyInstaller must carry `_lzma` and `liblzma`, and the M8 smoke test asserts `import lzma` succeeds in the frozen shell on every platform, not just Linux.
   - **macOS** — `hdiutil attach -nobrowse -readonly -mountpoint <tmp>`, then **`ditto`** (never `cp -R`, which mishandles extended attributes and signed-bundle metadata), then `hdiutil detach`. Both binaries are OS-provided; neither is a new dependency. The mount point is explicit so two concurrent exporters cannot collide on `/Volumes/Blender`.
5. **Integrity-check the extracted tree** before it is ever used, and fail with a named reason rather than letting a broken Blender surface as a mystery step failure minutes later:
   - `<blender-resource-root>/4.5/scripts/addons_core/io_scene_gltf2/` and its `libextern_draco.{dll,so,dylib}` exist (verified path on this machine: `/Applications/Blender.app/Contents/Resources/4.5/scripts/addons_core/io_scene_gltf2/libextern_draco.dylib`);
   - a `--background --factory-startup --python-expr` probe reports `bpy.app.version == (4, 5, 1)`, `bpy.ops.export_scene.gltf` present, and **all three private monkeypatch targets resolve** — `GlTF2Exporter.__append_unique_and_get_index`, `io_scene_gltf2.io.com.gltf2_io.Animation`, `sampling_cache.get_range`. All three were verified present on 4.5.1 locally;
   - the same probe imports `cod_asset_importer` from its loose path and reports `XMODEL_LOD_API_VERSION == 1` (§5.3);
   - on **macOS only**, `codesign --verify --deep --strict` on the extracted `Blender.app` and `TeamIdentifier=68UA947AUU`, `flags=0x10000`. This is now the *only* thing standing between the player and running a substituted binary, so it is an assertion, not a log line. Note it will begin failing after Blender's first run writes `__pycache__` into itself (measured, §4.2) — hence `PYTHONDONTWRITEBYTECODE=1` in `child_env(kind='blender')`, and hence the check runs **once, at provision time**, and its result is recorded in the stamp rather than re-run per export.
6. **Promote atomically** — `rename(<cache>/4.5.1.incoming, <cache>/4.5.1)` only after step 5 passes, then write `.fod_blender_ok` containing the archive SHA-256, the probe results and a timestamp. A half-extracted tree can therefore never be mistaken for a good one. A previous version's directory is deleted only after the new one is promoted.
7. **Serialize** with a lock file in the cache root. The game can launch the exporter while a Tk exporter is already open, and two concurrent 390 MB downloads into the same directory is a corruption path, not a theoretical one. The second process waits and reports "waiting for another Friends of Duty exporter to finish preparing Blender".

**Found at runtime:** `fod_paths.provisioned_blender()` returns `blender/blender.exe`, `Blender.app/Contents/MacOS/Blender`, or `blender/blender` beneath the resolved root. `find_blender()` (`friends_of_duty_exporter.py:97-109`) is rewritten to the §4.4 resolution order. **The candidate list and the PATH lookup are deleted in the frozen build** — but, reversing a deletion the bundled design called for, **`--blender` and the GUI's Browse row are KEPT**. They are the offline escape hatch and the "I already have exactly 4.5.1" path, and under a runtime-fetch design that is a support necessity rather than a convenience. A manually supplied Blender still runs the step-5 probe and is refused with a named reason if it is not 4.5.x.

**The offline failure message is a deliverable, not an afterthought.** When the fetch fails — no network, a corporate TLS-intercepting proxy, blender.org unreachable — the exporter must print and display the exact pinned URL, the expected SHA-256, and the destination directory to drop the archive into, so a player on a locked-down connection can complete it by hand on another machine. M6's definition of done includes that message existing and being tested with the network disabled.

**Version constants.** `BUNDLED_BLENDER_VERSION` is renamed `PINNED_BLENDER_VERSION = (4, 5, 1)`; the frozen build asserts `bpy.app.version == PINNED_BLENDER_VERSION` and refuses otherwise. The dev-checkout path keeps `MIN_BLENDER = (4, 5, 0)` and `MAX_BLENDER = (5, 0)` **exclusive** (i.e. 4.5.x only), with a warning when the running patch differs from the pin. `find_blender()` currently `return`s on the *first* candidate whose version parses and is at or above MIN (`:97-109`); with a ceiling it must keep iterating `blender_candidates()` (`:58-79`) past out-of-range hits, or a stray 5.2 in `/Applications` masks a valid 4.5 — which, given blender.org now serves 5.2 LTS by default, is the common case on a developer machine, not an edge case.

**Why 4.5.1 specifically:** it produces `Khronos glTF Blender I/O v4.5.47`, the generator string embedded in the shipped reference pak. Two caveats stated honestly rather than asserted: `v4.5.47` pins the **add-on** version, not a Blender patch release, and nothing in the repo records which Blender built the reference — `Content/Content/current/provenance/` contains only `source_archives.json`, and `fodpak.json`'s keys are `categories, contentTier, createdUtc, exporterVersion, format, gameContentVersion, hasUnitedOffensive, notes, orientation, sourcePolicyFormat, sourcePolicyVersion, sourceSummary, version` — no Blender, importer, Python, numpy or Pillow field (all verified). M5 must record the exact Blender build empirically. Blender 4.5 LTS is supported to 2027-07; 5.0 shipped 2025-11-18, 5.1 on 2026-03-17 and 5.2 LTS on 2026-07-14, so the pin is two LTS generations behind current by the time this ships. That is intentional and it is also a scheduled liability — see risk 7 and §12 Q4.

**Invocation is unchanged** apart from one added flag — see §6.1.

**Two things the audits could not settle, stated plainly:**

1. The 4.2-vs-4.5 accessor gap (84,628 vs 53,162) is larger than the missing sampling-range monkeypatch alone should explain. It was not established whether stock 4.2 also differs in `export_optimize_animation_size` behaviour. Pinning 4.5.1 makes it moot; nobody should spend time on it.
2. Both glTF monkeypatches are documented as *structure-neutral* — `tools/fod_export_common.py:242-244` states the dedup replacement "returns the same indices in the same order, so the glTF structure is unchanged", and `:376-379` states the sampling patch produced a byte-identical viewmodel. So a patch **miss** is a 6× speed regression (171.8 s vs 29.0 s per player profile, ~11.5 min of silent 100% CPU across four profiles), not a content change. Under a bundled runtime the soft `emit("  note: …")` fallbacks at `:328-334`, `:344`, `:358`, `:370` and `:373` become a hard `SystemExit` when `FOD_BUNDLED=1`, and the provisioner's step-5 probe asserts all three targets resolve before Blender is ever promoted.


### 5.3 The Rust importer (`cod_asset_importer`)

**Bundled:** one per-target v3.6.0 abi3 extension as a **loose file** at `tools/cod-asset-importer/python/cod_asset_importer/cod_asset_importer.{pyd,abi3.so}`, with its Python wrapper, `LICENSE`, `PROVENANCE.md`, `MODIFICATIONS.md` and the full `rust/` tree beside it.

**It must stay loose for exactly one reason: Blender's interpreter cannot import from inside a frozen archive.** `pipeline.py:1085` (and five siblings) passes that directory as an argv positional and the Blender-side tools `sys.path.insert` it. The earlier draft added a second reason — "`prop_export_fingerprint` globs `wrapper.parent`, so the path must be stable across runs or every run re-exports all 239 props" — which is **false** and is withdrawn (see §3.2: the fingerprint hashes `path.name`, never an absolute path). The real constraint the fingerprint imposes is different and is handled in §6.1: `wrapper.parent.glob("cod_asset_importer.*")` (`:158-163`) must find **exactly one** `.so`/`.pyd`, and `:176`'s silent `if not path.is_file(): continue` must become a hard error.

**abi3 is confirmed, not assumed.** `tools/cod-asset-importer/rust/cod_asset_importer/Cargo.toml:7-14` declares `[lib]` at `:7`, `crate-type = ["cdylib"]` at `:9`, and `pyo3 0.20.0` with `extension-module, abi3, abi3-py38` at `:14`. The same `.so` was loaded successfully in Blender 4.5.1's bundled CPython 3.11.11 and in host CPython 3.13.5, both reporting `XMODEL_LOD_API_VERSION = 1`. All 75 undefined Py symbols are limited-API; `otool -L` shows no libpython, and the ELF `DT_NEEDED` set is libgcc_s/librt/libpthread/libdl/libc only.

**Version identity is currently inconsistent and must be fixed in M1.** "v3.6" lives only in `python/cod_asset_importer/__init__.py:5` (`"version": (3, 6, 0)`); `rust/cod_asset_importer/Cargo.toml:3` still says `version = "1.1.0"`. Bump the crate in the same commit so the fork tag, the built artifact and the Python package agree.

**Produced by CI, never copied from a build host.** See §8.1. `exporter/build_importer.py`'s `_cargo_build` (`:261-340`), its `sys.executable -c` probe (`:144-148`) and its `PYO3_PYTHON = sys.executable` (`:300`) are all deleted — the first because a rustup + C++ toolchain prerequisite is the exact opposite of the goal, the latter two because they are broken under any freezer. **What survives:** `build_importer.py` keeps `_extract_from_release` but points it at `packaging/importers/<target>/` (populated by `make importer-fetch`, §8.1) instead of the deleted v3.5 zips, and its `BuildError` message becomes "run `make importer-fetch`" rather than naming rustup. `build_importer.py` is a **developer/CI module and is excluded from the player payload** — today's `Builds/*/Exporter/build_importer.py` stops shipping.

**Verified at runtime, on the Blender side of the exec boundary.** `check()` becomes a single `blender --background --factory-startup --python-expr` probe that imports the extension from its real path and prints `XMODEL_LOD_API_VERSION`, cached for the session. The frozen host **never imports it** — that keeps the GPL `.so` exclusively inside GPL Blender's process, which is the licensing posture §9 depends on, and it validates the actual production load path rather than a proxy.

**One-line correctness fix, independent of packaging:** `tools/cod-asset-importer/python/cod_asset_importer/importer.py:271` opens a bare `try:` whose `except: return` at `:357-358` swallows the whole `_import_material_v14` body, including the deprecated `material.blend_method` write at `:275`. If a future Blender removes that attribute, every CoD1 material import fails silently and produces untextured models with no error line. Replace with a typed except that re-raises, or set `surface_render_method` when present.

### 5.4 Pillow and numpy

**Bundled** inside the frozen shell (`_internal/PIL`, `_internal/numpy`), pinned by exact version and hash in `packaging/requirements-3.13.lock`. Measured installed sizes: numpy 2.4.3 = 19.3 MB (m), Pillow 12.1.1 = 13.0 MB (m); numpy's `tests/` (~14 MB) is stripped by the spec.

**Blender's interpreter needs neither.** None of the four Blender-side tools nor `fod_export_common.py` imports PIL or numpy; Blender bundles its own numpy 1.26.4 for `io_scene_gltf2` and ships no PIL. No site-packages injection into Blender is required, and there is no version negotiation between the two numpys.

**Note for the docs:** `Docs/CONTENT_PIPELINE.md` claims the exporter "converts DDS/TGA/JPG via Pillow". That is stale for the model path — `tools/fod_export_common.py:90-102` (`convert_image_to_png`) routes every model texture through `bpy.data.images.load()` → `image.save()`. Pillow decodes the *map* textures (`tools/import_cod_multiplayer_maps.py:2116`, `:2208`). This matters for Phase B; see §11 M11.

---

## 6. Runtime contract changes

### 6.1 `exporter/pipeline.py`

**`_python()` (`:222-223`).** The real signature is `_python(cfg: PipelineConfig, tool: str, *args: str)` and all eleven call sites are `build_argv=lambda c: _python(c, "x.py", …)`. Keep the name and the signature so no lambda is touched:

```python
def _python(cfg: PipelineConfig, tool: str, *args: str) -> list[str]:
    return _host(cfg, TOOLS_DIR / tool, *args)

def _host(cfg: PipelineConfig, script: Path, *args: str) -> list[str]:
    if fod_paths.is_bundled():
        return [fod_paths.self_exe(), "--fod-run-tool", str(script), *args]
    return [sys.executable, str(script), *args]
```

**The `package` step (`:1367-1376`)** changes from a literal `[sys.executable, str(EXPORTER_DIR / "package.py"), …]` at `:1371` to `_host(c, EXPORTER_DIR / "package.py", …)`.

The launcher's dispatch, which must be the **first** thing `fod_launcher.py` does:

```python
if len(sys.argv) > 2 and sys.argv[1] == "--fod-run-tool":
    tool = pathlib.Path(sys.argv[2]).resolve()
    sys.argv = [str(tool), *sys.argv[3:]]
    sys.path.insert(0, str(tool.parent))       # tools/ sibling imports, e.g. cod1_archive_policy
    runpy.run_path(str(tool), run_name="__main__")
    raise SystemExit(0)
```

`runpy.run_path` with `run_name="__main__"` is load-bearing, not stylistic: **28 of the 50 tools have no `def main`** and execute at module scope with argv bound at import time (`tools/export_cod1_demo_viewmodels.py:37`, `:864-873`). A fresh process per step plus `run_path` gives every one of them exactly the semantics they have today.

**`_blender()` (`:226-232`)** keeps its exact shape and gains one flag:

```python
return [str(cfg.blender), "--background", "--factory-startup",
        "--python", str(TOOLS_DIR / tool), "--", *args]
```

`--factory-startup` is not optional. Today the pipeline loads the *player's* Blender preferences and third-party add-ons into the export session — a BlenderMCP add-on was observed injecting output into a live run. Verified on 4.5.1 that `io_scene_gltf2` remains resolvable from `addons_core` under factory prefs, `bpy.ops.export_scene.gltf` is exposed, and all three monkeypatch targets import. **But the shipped reference pak was produced *without* the flag**, so it changes the oracle for the 429 Blender-produced GLBs. M4's definition of done therefore includes re-exporting those 429 under `--factory-startup` and freezing the result as the Phase B oracle (§11 M4).

**Blender exits 0 on Python-level failure.** `pipeline.py:1443` treats only a nonzero return code as a step failure, and `blender --background --python` returns 0 down several error paths; today the only guard is the step's `is_done` output probe. Every Blender-side tool must therefore end with an explicit success sentinel line (`@fod v1 end step=<key> status=done …`, §7.2) followed by `sys.exit(0)`, and `_run_subprocess` fails the step if the sentinel is absent even on exit 0. The M8 smoke test asserts this on a synthetic export.

**`_run_subprocess` (`:1399-1444`)** gains `env=fod_paths.child_env(kind)` and an `@fod` line parse (§7). It keeps `bufsize=1, text=True, errors="replace"`, merged stderr, `cwd=str(REPO_ROOT)` (`:1404`), the cancellation check after every line (`:1431-1434`) and the `SILENCE_HEARTBEAT_SECONDS = 15.0` watchdog (`:152`, `:1417-1421`). It also gains the cancel fix in §7.3.

**`_step_signature` (`:1447-1490`)** keeps its per-file `sha256` list unchanged — this is the whole reason the tools stay loose on disk — and gains a **per-step, per-toolchain** key. The earlier draft proposed a single global `"payload": build_stamp.PAYLOAD_SHA256` covering "the exporter build id"; that is rejected, because it would invalidate all 19 markers — including `maps` (391 MB, 1,663 GLBs) and `props` (341 GLBs) — for a one-line UI fix, which is strictly worse than today and directly contradicts §3.2's argument that onedir was chosen so a `.py` fix costs a few hundred KB. Instead:

```python
"toolchain": build_stamp.toolchain_for(step.key),   # dict, sorted keys
```

- Blender steps (`viewmodels`, `worldmodels`, `shellcasing`, `projectiles`, `players`, `props`) contribute `{blenderVersion, blenderSourceSha256}`.
- Importer-dependent steps additionally contribute `{importerVersion, importerNativeSha256}`.
- Host steps contribute `{interpreterVersion, numpyVersion, pillowVersion}`.
- `EXPORTER_BUILD` is **not** hashed anywhere.

This closes a real gap that exists today — `signature_files` covers `IMPORTER_NATIVE` (`:1354`) but nothing about Blender, so a Blender bump currently invalidates **zero** markers — without creating a new one. Bump `PIPELINE_SCHEMA_VERSION` (`:135`) 3 → 4 in the same commit so pre-migration markers retire, and update `tests/test_exporter_pipeline.py` and `tests/test_prop_export_fingerprint.py` in that commit (both assert on the current versions).

**Marker portability, and why the schema bump belongs in the same commit.** `_step_signature` records each signature file as `resolved.relative_to(REPO_ROOT)` (`:1458-1459`), and `REPO_ROOT` flips from the repo root to `Exporter/` when `EXPORTER_DIR/tools` exists (`:63-70`). `exporter/package.py` therefore hashes in as `exporter/package.py` in a dev checkout and `package.py` in the bundle; `_run_subprocess`'s `cwd` changes meaning the same way. Markers are consequently **not portable between a dev checkout and the bundle** — which is correct behaviour, and is another reason the `PIPELINE_SCHEMA_VERSION` bump lands alongside.

**`needs_blender` (`:194`, set at `:1081`, `:1099`, `:1115`, `:1132`, `:1154`, `:1340`)** has zero readers. Delete it, and delete the `--cli` WARN-and-continue path at `friends_of_duty_exporter.py:289-290` — with Blender internal, neither can be true.

**`tools/export_cod_multiplayer_props.py:148-203`** needs three changes: (a) add `FOD_PAYLOAD_STAMP` (set by `child_env()`) to the payload dict and bump `PROP_EXPORT_FINGERPRINT_VERSION`; (b) turn `:176`'s silent `if not path.is_file(): continue` into a hard error — a fingerprint that quietly stops being sensitive to the extension is worse than a crash; (c) assert `wrapper.parent.glob("cod_asset_importer.*")` yields exactly one `.so`/`.pyd`, which is the property the hard error actually depends on.

### 6.2 `exporter/fod_paths.py` and `exporter/fod_launcher.py` (new)

`fod_paths.py` is the single source of truth:

- `is_bundled()` → `getattr(sys, "frozen", False)`
- `self_exe()` → `sys.executable` (which under PyInstaller *is* the bundle — correct here)
- `payload_root()` → directory containing the launcher; on macOS, the directory containing the `.app`
- `blender_cache_root()` → the per-user, version-keyed directory in §4.4, created on demand
- `provisioned_blender()` → the §4.4 resolution order (`--blender` → `FOD_BLENDER` → cache → fetch), delegating the fetch to `blender_provisioner.py`. On POSIX it `chmod +x`es `blender/blender` and `blender/4.5/python/bin/python3.11` — `tarfile` preserves modes and `ditto` preserves the bundle, so this is belt-and-braces rather than load-bearing, but a `zipfile` extraction on a POSIX host (a developer fetching the Windows archive) does drop them. **It cannot repair the exporter's own exec bit** — by the time `fod_paths` runs, the ELF already executed. That case is handled by the depot `FileProperties` (§8.5) plus a `File.SetUnixFileMode` pre-spawn fix in `ExporterLauncher` (§6.4).
- `importer_python_root()` → `payload_root()/tools/cod-asset-importer/python`
- `child_env(kind)` → the env every child gets

**`child_env(kind)` returns `os.environ.copy()` with kind-specific mutations.** Starting from an empty dict would strip `PATH`, `HOME`, `TEMP` and `SystemRoot` (which Windows subprocesses require) and `DISPLAY`. The two child kinds have *opposite* requirements — PyInstaller's bootloader needs its own variables for our re-exec'd frozen children, and Blender must never see them:

- **`kind='blender'`:** **delete** `LD_LIBRARY_PATH` and `DYLD_LIBRARY_PATH` outright; delete `_MEIPASS`, `_PYI_ARCHIVE_FILE`, `_PYI_APPLICATION_HOME_DIR`; clear `PYTHONHOME`, `PYTHONPATH`, `PYTHONSTARTUP`, `OCIO`, `BLENDER_USER_SCRIPTS`, `BLENDER_USER_CONFIG`, `BLENDER_USER_EXTENSIONS`, `BLENDER_SYSTEM_SCRIPTS`, `BLENDER_SYSTEM_RESOURCES`; set `PYTHONDONTWRITEBYTECODE=1` (§4.2 — Blender otherwise writes `.pyc` into its own signed bundle and breaks the seal).
- **`kind='host'`:** leave PyInstaller's own variables alone; clear only `PYTHONHOME`, `PYTHONPATH`, `PYTHONSTARTUP`.
- **Both:** set `FOD_BUNDLED=1`, `FOD_PAYLOAD_STAMP=<PAYLOAD_SHA256>`, `PYTHONHASHSEED=0`, `PYTHONIOENCODING=utf-8`, `PYTHONUTF8=1`, and `FOD_PROGRESS_PROTOCOL=1` iff the parent was invoked with `--game-callback`.

**Why delete `LD_LIBRARY_PATH` rather than restore it from `*_ORIG` (the earlier draft's instruction).** `Builds/SteamOS/FriendsOfDuty.sh:6` is `export LD_LIBRARY_PATH="$SCRIPT_DIR:$STEAM_PLUGIN_DIR${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"` — the game directory and Unity's plugin directory are **prepended** to whatever Steam already set. PyInstaller's `LD_LIBRARY_PATH_ORIG` therefore holds that entire poisoned string, not a clean one. Restoring it hands Blender the game directory (which contains `libsteam_api.so`) and, under Steam Linux Runtime 1.0 (scout — an `LD_LIBRARY_PATH` runtime, not a container), a 2012-vintage `libstdc++.so.6` that Blender 4.5 will refuse with a GLIBCXX error. Restoring `*_ORIG` would deliver the exact failure the mitigation exists to prevent. Blender resolves through its own `$ORIGIN/lib` RPATH and the host loader; `*_ORIG` is consulted only to **log** what was dropped.

`PYTHONHASHSEED=0` is one line and it protects M5's acceptance gate: string hashing is randomized per process, so any tool deriving output from iterating an unsorted `set` of strings varies run to run, independent of interpreter or numpy version. Without the pin, a nonzero `maps` diff is uninterpretable.

`fod_launcher.py` is the PyInstaller entry point, in order: (1) `--fod-run-tool` dispatch; (2) `--fod-selftest` (and `--fod-selftest --report-timing`); (3) `--fod-probe-importer`; (4) `--fod-detect-cod`; (5) `sys.path.insert(0, payload_root())`, `import friends_of_duty_exporter`, `sys.exit(main())`.

### 6.3 `exporter/friends_of_duty_exporter.py`

- `find_blender()` (`:97-109`) → the §4.4 resolution order when frozen; dev-checkout discovery survives with the version bounds from §5.2 and the "keep iterating past out-of-range candidates" fix.
- Delete `pip_install_argv` (`:140-141`) and its uses (`:286`, `:515`), the "Install Python packages (pip)" button (`:437-440`, `:509-520`), the "Prepare importer" button (`:441-444`, `:533-556`), and the whole `RequirementsFrame` (`:399-564`). The wizard becomes Paths → Export → Done.
- **Reversal, relative to the bundled design: the Blender-path row and Browse (`:430-433`, `:503-507`) are KEPT**, moved onto the Paths screen as a collapsed "Advanced — use an existing Blender 4.5" affordance that is empty and ignored in the normal case. Under a runtime fetch this is the offline/blocked-network escape hatch and the support tool for a corrupted cache, so deleting it would remove the only recovery path a player has when the download cannot complete. It writes through to the same `--blender` argument, and a manually chosen Blender is put through §5.2's step-5 probe exactly like a fetched one.
- Add a **Blender provisioning phase** ahead of the 19 steps: a status line, a determinate download bar, and a Cancel that abandons the transfer without touching the cache. On a cache hit this phase renders for well under a second and reports "Blender 4.5.1 ready (cached)".
- **Delete `importer_addon_status()` (`:112-114`) and the `run_cli` importer-preparation block (`:291-316`), and drop `import build_importer as fod_build_importer` (`:27`).** Once §5.3 removes `_cargo_build` and the `sys.executable -c` probe, that block — which prints "preparing the authored-LOD importer…" and calls `fod_build_importer.build(..., require_lod=True)` at `:298-301` — is dead or broken. Under a bundled runtime the importer is a shipped artifact, not something the exporter prepares. `check()` moves to the Blender-side probe in §5.3 and runs once at startup only to fail fast with a named payload-integrity error.
- Add a free-space precheck (against the **output** volume, §4.4) and progress lines around `seed_working_directory` (`:159-195`).
- Keep `--cli`, `--game-dir`, `--output`, `--force`, `--only`, `--zip`, `--game-exe` verbatim. Keep `--include-uo` and `--all-mp` accepted-and-ignored (`:859-865`; rationale comment at `:853-857`) — installed older builds pass them. Keep `--maps` hard-erroring (`:866-870` plus the `parser.error` at `:883`). `--game-callback` (`:876`), parsed and read nowhere today, becomes the live switch for the `@fod` protocol and for `FOD_PROGRESS_PROTOCOL`.
- Keep `stream_stdout_live()` (`:888-901`, `reconfigure(line_buffering=True)`) and the ASCII-only constraint documented at `tools/fod_export_common.py:181-191` — the Windows console codepage cannot take non-ASCII in *emitted* text. Player-supplied **paths** are a separate matter: `child_env()` sets `PYTHONIOENCODING=utf-8` and `PYTHONUTF8=1`, and the M8 smoke test includes a CJK and an accented path round-tripped through `--fod-run-tool` argv and printed.
- Delete the atomic-promote logic? **No.** `:146-230` (hidden `.current.exporting`, APFS clone / reflink / copytree seed, validate, rename, roll back) is one of the best things in the codebase. Untouched.

### 6.4 `Assets/Scripts/Content/ExporterLauncher.cs`

The "zero C# change" position is not tenable and is not taken here.

**Discovery.** `ExporterScriptName` (`:21`) becomes an ordered list of **(name, kind) pairs**, because `IsExporterDirectory` (`:777-789`) is implemented as `Directory.Exists(directory) && File.Exists(Path.Combine(directory, ExporterScriptName))` and a `.app` is a **directory** — `File.Exists` returns false for it, so a naive probe list would fail discovery on the one platform where the artifact is definitely present. The predicate becomes:

```csharp
probes.Any(p => p.IsBundle
    ? Directory.Exists(Path.Combine(dir, p.Name))
    : File.Exists(Path.Combine(dir, p.Name)))
```

with entries `("FriendsOfDutyExporter.exe", file)`, `("Friends of Duty Exporter.app", bundle)`, `("FriendsOfDutyExporter", file)`, `("friends_of_duty_exporter.py", file)`. The same blind spot applies to `RequireExternalPayloadFile` (`FriendsOfDutyMacBuild.cs:355`, `File.Exists(...)` + length check) — §8.4 adds a `RequireExternalPayloadDirectory` sibling.

**Launch.** `BuildStartInfo` (`:616-667`) names a **file** on all three platforms with `UseShellExecute = false`, `RedirectStandardOutput = true`, `RedirectStandardError = true`, `CreateNoWindow = true`:

- Windows: `Exporter/FriendsOfDutyExporter.exe`. This is the fix for the permanently-empty console: `UseShellExecute = true` (`:639`) existed only so `cmd.exe` could resolve `py`/`python`, and a bundled binary removes that reason.
- macOS: `Exporter/Friends of Duty Exporter.app/Contents/MacOS/FriendsOfDutyExporter` — the **inner Mach-O**, not the bundle, because `UseShellExecute=false` cannot launch a bundle and `open --args` drops arguments (which is exactly what the existing comment at `:643-646` documents).
- Linux: `Exporter/FriendsOfDutyExporter`.

**Exec bit.** Before spawning on POSIX, `BuildStartInfo` stats the target and, if the owner-execute bit is clear, applies `File.SetUnixFileMode(path, 0755)`. The exporter cannot fix its own exec bit, and today's shim survives only because the launcher invokes `/bin/sh <script>` (`:660-661`), which ignores it.

**Diagnostics.** The four strings naming Python and Blender (`:148`, `:161-163`, `:180-181`, `:566-569`) are rewritten; `:569` is one of the ten `4.2` literals in §11 M4.

**Back-compat.** The three `run_exporter.*` shims survive one release, plus an 8-line `friends_of_duty_exporter.py` sentinel that `os.execv`s the binary. The Windows shim **must block and propagate the exit code** — `start` returns immediately and `WatchProcess` (`:674-757`) would latch `s_successfulCompletion` on cmd.exe's exit 0 and print "Export complete" seconds after launch:

```bat
@echo off
"%~dp0FriendsOfDutyExporter.exe" %*
exit /b %ERRORLEVEL%
```

`.sh` / `.command`: `exec "$(dirname "$0")/FriendsOfDutyExporter" "$@"`.

**Unchanged:** the argument string (`:130-135`), the exit-code contract (`:674-757`), the CoD auto-detection (`:192-209`, `:471-484`, `:492-542`), the ring buffer (`:43`), `CondenseForNotice` (`:759-775`), `Quote` (`:873-874`), and the folder picker (`:260-320`, `osascript` / PowerShell `FolderBrowserDialog` / `zenity` / `kdialog`). Note for honesty: "requires nothing from the user" is not literally true while that picker depends on host tooling — which is precisely why §6.3 ports auto-detection into the exporter (`exporter/cod_autodetect.py`, M6) and why the picker is only reached when auto-detection fails. On Steam Deck Gaming Mode neither the picker nor a Tk window is reachable at all; see §7.1.

### 6.5 Game UI

Delete `Assets/Scripts/Content/FodToolchainProbe.cs` (29,690 B) entirely. In `Assets/Scripts/Frontend/FodBootFlow.cs` — **verified site list, because the earlier draft's line numbers were wrong in every case and its site list was incomplete enough that the project would not compile**:

**`FodToolchainProbe` references (8):** `:705` (`FodToolchainProbe.Begin()`), `:764-768` (the five-field sample block, of which the draft cited only two), `:1696` (`Refresh()`), `:1916` (`Refresh()`). The `bool probeReady` field is declared at `:151`.

**`Stage.LocalRequirements` references (15):** `:47` (enum member; the `Stage` enum runs `:40-54`), `:300` (`target = Stage.LocalRequirements`), `:546` (stage predicate), `:703` (`if (next == Stage.LocalRequirements)`), `:1168` (`Stage.LocalRequirements => 2`, the Stage→step-number map), `:1200` (stage predicate), `:1219-1220` (`case` → `DrawLocalRequirements()`), `:1250` (cancel switch), `:1255` (`Enter(Stage.LocalRequirements)` from `LocalFailed`), `:1372` (`RequestErase(Stage.LocalRequirements)`), `:1557` (`() => Enter(Stage.LocalRequirements)` — the source card's action), `:1639` (`void DrawLocalRequirements()`), `:1917`, `:2305`.

Two of those are behavioural, not mechanical:

- **`:1168` does NOT renumber anything.** The map reads `Stage.LocalRequirements => 2,` at `:1168` and `Stage.LocalExtract => 2,` at `:1169` — verified. Deleting the `LocalRequirements` arm leaves every remaining stage on the same step number. (One critic asserted this would "silently shift every subsequent step's displayed index"; it would not, and the reasoning is recorded here so the objection cannot recur. Still re-read `:1164-1172` before editing.)
- **`:1250-1255` is the cancel/back state machine.** `case Stage.LocalRequirements:` falls through with `RemoteInput` to `Enter(Stage.SourceChoice)` and is simply removed; `case Stage.LocalFailed:` currently backs out to `Enter(Stage.LocalRequirements)` at `:1255` and must become `Enter(Stage.SourceChoice)`.
- `:1372`'s `RequestErase(Stage.LocalRequirements)` becomes `RequestErase(Stage.LocalExtract)`; `:300`, `:546`, `:703`, `:1200`, `:1557`, `:1917`, `:2305` each become `LocalExtract`.

**Other edits:** remove the `GUI.enabled = probeReady` gate at `:1679` (the guarded button block runs `:1679-1686`); remove the `else if (!probeReady)` branch at `:1666-1675`; fix the source-card requirement literal at `:1555-1556` (`"REQUIRES  Call of Duty + United Offensive  •  Python 3.10+  " + "•  Blender 4.2+"`); fix the pane subtitle at `:1646-1649`; the `DrawToolRow` labels at `:1654` (PYTHON) and `:1656` (BLENDER) disappear with `DrawLocalRequirements()`; fix the "The exporter is running in its own window" copy at `:1831-1832` (under `--cli` there is no window, and there never was); swap the export pane's `DrawProgressBar(-1f, null)` at **`:1839`** for the real fraction. The second indeterminate bar at `:1664` disappears with `DrawLocalRequirements()`; `:1289` belongs to a different stage and is not touched. `DrawExporterConsole` at `:2185` is unchanged.

M7's definition of done includes `grep -c "LocalRequirements\|FodToolchainProbe" Assets/Scripts/Frontend/FodBootFlow.cs` returning 0 and the project compiling.

This is the largest player-visible win in the project: the screen that tells the player to go install two things ceases to exist.

---

## 7. Progress reporting & UX

### 7.1 Standalone (double-click) — desktop only

1. Player double-clicks the one artifact. No console window, no installer, no admin prompt.
2. Window opens in ~1-2 s on **Paths**. The CoD folder is pre-filled by `exporter/cod_autodetect.py` (§6.3, an M6 deliverable). Validation is live and specific: `validate_game_dir` (`:117-137`) calls `official_archives(path, COD1_TIER)` and `uo_installation_status(path)`, so a UO-less install gets the named reason that `tools/cod1_archive_policy.py:157-165` **already produces today**:

   > "Friends of Duty requires both Call of Duty and Call of Duty: United Offensive.\nNo United Offensive archives were found in `<dir>`.\nInstall United Offensive into the same folder as Main, then run the exporter again. No files were written."

   and a disabled Start button, before any directory is created (`pipeline.py:1538-1541`). **PROPOSED copy change** (this string does not exist yet and was mis-quoted as current in the earlier draft): extend that message with the archive counts and the concrete UO contribution, which `cod1_archive_policy.py:141-143` already documents in its docstring — the smoke grenade, 95 additional player animations and two of the seven maps.
3. Output folder pre-filled with `FodContentPaths.PreferredMountDir`'s equivalent. "Force full re-export" checkbox. Free-space line naming the output volume.
4. **Prepare.** On a first run only: "Downloading Blender 4.5.1 — 137 MB of 348 MB", a determinate bar driven by the provisioner's byte callback, and a Cancel that abandons the transfer leaving the cache untouched. Then a brief "Verifying…" / "Extracting…" / "Checking…". On every subsequent run this phase reports "Blender 4.5.1 ready (cached)" and is gone in under a second. If the fetch fails, this screen — not a log line — carries the pinned URL, the expected SHA-256 and the destination directory (§5.2), plus a button that opens the Advanced Blender-path row.
5. **Export.** 19-row table with per-step status, a **determinate** bar, a scrolling log, and Cancel.
6. **Done.** "Save transportable .fodpak (zip)…" and "Launch game".

**Steam Deck Gaming Mode is not this flow.** There is no desktop session for a Tk window spawned by a game process to appear in, and neither `zenity` nor `kdialog` can draw there. On Deck the exporter is reachable **only** through the game's `--cli` + `@fod` path: the game owns auto-detection and the folder picker and renders the determinate bar itself. The Tk GUI is a Desktop Mode affordance. M5's Deck deliverable and M8's Linux acceptance test are written against the in-game `--cli` flow, and Gaming-Mode auto-detection of a CoD path on an external microSD, a second library or a Proton prefix is an explicit M5 measurement, not an assumption.

### 7.2 The progress model

Today: 19 discrete ticks plus free text, with two heartbeats papering over the silence (`pipeline.py:152` at 15 s, `tools/fod_export_common.py:178` at 5 s). The `maps` and `props` steps are single ticks running for minutes.

New: an ASCII, line-delimited, per-line-flushed protocol.

```
@fod v1 prepare phase=download done=143654912 total=365109248 label=blender-4.5.1-windows-x64.zip
@fod v1 prepare phase=verify   label=sha256
@fod v1 prepare phase=extract  label=blender-4.5.1
@fod v1 prepare phase=ready    cached=0 secs=94.2
@fod v1 begin   step=props index=17 total=19 title=Export map props
@fod v1 item    step=props done=112 total=239 label=xmodel/carentan_wall_a
@fod v1 end     step=props status=done secs=12.3
@fod v1 overall frac=0.83 eta=94
@fod v1 done    ok=1 package=/Users/x/Library/.../Content/current
```

**`prepare` lines precede `begin index=0` and are outside the 19-step weighting.** The consumer renders them as a separate phase with its own bar rather than folding them into `overall frac`, because the download is bounded by the player's connection and mixing it into an ETA calibrated from CPU work produces a wildly wrong estimate. `phase=ready cached=1` is emitted immediately on a cache hit, so a consumer can skip drawing the phase at all.

- **`index` is 0-based**, matching `ProgressFn`'s first argument, and `props` is `index=17` of `total=19` in `build_steps` order (`extract_cod1, extract_mp_models, stage_ordnance, viewmodels, worldmodels, shellcasing, projectiles, players, presentation, footsteps, impacts, mp_scripts, map_previews, hud, ordnance, weapon_data, maps, props, package`). `run_pipeline` logs `[index+1/total]` for humans. **`--only` shortens `total`**, so a consumer must never assume 19.
- **Activation crosses the process boundary via the environment, not argv.** `--game-callback` is a flag on the *parent*; none of the 19 children sees it. `child_env()` sets `FOD_PROGRESS_PROTOCOL=1` when the parent was invoked with it (§6.2). `tools/fod_export_common.progress(done, total, label)` — a new helper beside `emit()` (`:181-191`) — is a **no-op** unless `os.environ.get("FOD_PROGRESS_PROTOCOL") == "1"`. The existing human `[{index}/{len(model_ids)}]` lines (`tools/export_cod_multiplayer_props.py:814`, `:821`, `:823`) are unconditional and unchanged, so terminal output for a human is byte-identical to today — asserted by diff in M7's definition of done. Add per-item emission for the 39 viewmodels, the 4 player profiles and the 7 maps.
- `_run_subprocess` matches `@fod v1 item …` from the child, calls a new optional `sub_progress` callback, **and still forwards the raw line to `log`** — so `ExporterLauncher.cs:686-687`'s `s_status = line; AppendConsole(line);` shows something sensible even on a game build that has not been updated.
- `overall frac` is a **weighted** sum from `packaging/step_weights.json` — a checked-in, human-reviewable file read by `exporter/progress.py`, **not** a constant inside the CI-generated `build_stamp.py` (which would destroy M5's calibration on every build). Weights are **per-platform**, keyed by `sys.platform` plus machine class, falling back to the macOS table: a table calibrated on Apple silicon will mispredict on a Steam Deck. **The `maps` step wall clock has never been measured** (it produces 391 MB of the 810 MB package). Measuring it is a deliverable of M5, and **re-measuring on the final signed payload is a deliverable of M8** — `--factory-startup` alone removes third-party add-on load, and the pinned 4.5.1 replaces whatever M5 ran on.
- Unity: new `Assets/Scripts/Content/FodExporterProgress.cs` parses `@fod` out of the captured stream in `WatchProcess` (`:674-757`) and exposes `Fraction` / `StepTitle` / `Eta`. Non-`@fod` lines keep flowing to the existing console ring buffer (`ExporterLauncher.cs:43`, rendered by `FodBootFlow.DrawExporterConsole` at `:2185`).

### 7.3 Cancel, failure and resume

**Cancel** keeps the existing `threading.Event` → `terminate()` → 30 s wait → `PipelineCancelled` shape (`friends_of_duty_exporter.py:750-753`, `pipeline.py:1431-1434`) but **fixes it**: today, if the child ignores SIGTERM, `subprocess.TimeoutExpired` propagates instead of `PipelineCancelled`, the `finally` block terminates again, and the GUI reports a crash rather than a cancel. `_run_subprocess` wraps the wait — `except subprocess.TimeoutExpired: process.kill(); process.wait()` — and raises `PipelineCancelled` on both paths. The cancel poll also moves onto the existing `SILENCE_HEARTBEAT_SECONDS` watchdog thread (`:152`, `:1417-1421`) so it fires on the 15 s tick rather than only when the child emits a line. A `<staging>/cancel` sentinel bounds latency inside the per-item loops (props, viewmodels, maps), because a Blender child cannot observe the parent's `Event`. **Inside a single `export_scene.gltf` call there is no loop to poll**: cancel latency is that call's duration — up to ~170 s for a player profile — and the UI must say "Finishing the current item…" rather than appearing hung.

**Failure** stays what it is today and that is a feature of this design: every step is a subprocess, so a segfault kills one child and is reported as a named step failure with a nonzero exit code and the last 8 lines condensed into `LastLaunchNotice`. Additionally: a `<staging>/running/<key>.json` breadcrumb written before each step and unlinked after `_mark_complete`, plus `faulthandler.enable(file=<staging>/crash-<key>.txt)`.

**Native crash capture is asymmetric and the docs must say so.** `faulthandler` catches Python-level fatal signals; it does **not** catch Win32 access violations in native code (numpy, Pillow, the Rust extension, Blender). The Windows story is: Blender's own crash `.txt` for Blender steps, and for host steps a WER local-dump registry note in `Docs/EXPORTER_BUNDLE.md` — or an explicit statement that we will not have one. Do not imply coverage that does not exist.

**Resume** is preserved exactly. Markers stay at `<content>.parent/.fod_staging/done/<key>.json`; `_step_complete` (`:1497-1510`) still requires marker-match **and** the output probe; `_mark_complete` (`:1513-1530`) still re-asserts `is_done` after a clean exit and refuses to write a marker for incomplete output; the killed step's marker was already unlinked at `:1560` so it re-runs while every earlier step is skipped. The only change is the per-step `toolchain` key (§6.1): a Blender or importer bump now invalidates **exactly the steps that toolchain produces**, and an exporter code change still invalidates only the steps whose `signature_files` changed.

### 7.4 Headless `--cli` contract (what the game drives)

Unchanged: `--cli --game-dir "<d>" --output "<d>" --game-exe "<p>" --game-callback`, exit 0 = success, nonzero = failure. No IPC, no file handshake, no exit-code taxonomy. `--include-uo`, `--all-mp`, `--game-callback` remain tolerated. The stages `SourceChoice → LocalExtract → (Preparing | LocalFailed)` are unchanged; `LocalRequirements` is deleted.

---

## 8. Build & release pipeline

There is no CI today: `.github/` does not exist, the Makefile has no packaging target, and nothing in the repo reproduces a single native artifact. This section is greenfield.

### 8.1 `.github/workflows/importer.yml` — must land first

**Prerequisite (and the M1 trap).** `tools/cod-asset-importer/.git` **exists** — it is a nested clone with 8 modified files plus an untracked `PROVENANCE.md` and `.abi3.so`. Simply deleting `.gitignore:89` and running `git add tools/cod-asset-importer` records a **mode-160000 gitlink**, not files: `git ls-files` then returns exactly 1 and a fresh clone contains zero importer source. Either:

- **(a) Submodule:** push the v3.6 tree to `github.com/anarqz/cod-asset-importer` under GPLv3; `git rm -r --cached tools/cod-asset-importer`; delete `.gitignore:89`; `git submodule add -b main <url> tools/cod-asset-importer`; check out the v3.6 SHA; commit the gitlink plus `.gitmodules`. **Every CI job and `SteamPipe/build_all.sh` must then pass `--recurse-submodules`.**
- **(b) Vendor flat:** `rm -rf tools/cod-asset-importer/.git`; delete `.gitignore:89`; `git add tools/cod-asset-importer`.

Note also that `PROVENANCE.md` is itself untracked in the nested clone, so M1's "fix `PROVENANCE.md:67`" has no versioned baseline until the fork lands — the fix and the vendoring are one commit.

| Target | Runner | Build command and assertions |
|---|---|---|
| linux-x64 | `ubuntu-24.04`, container `ghcr.io/pyo3/maturin:v1.9.6` | `maturin build --release --manifest-path rust/cod_asset_importer/Cargo.toml --interpreter python3.11 --out dist`. Assert the GLIBC floor with `objdump -T … \| grep GLIBC_ \| sort -V \| tail -1` and **record whatever it reports**. The earlier draft claimed the v3.5 artifacts achieved GLIBC_2.16; maturin's images are manylinux2014-based, i.e. a **2.17** floor. Correct the number to the measured one rather than carrying an unverified claim. |
| win-amd64 | `windows-2025` | `actions/setup-python@v5` with `3.11`; `PYO3_PYTHON=$(which python)` set **in the workflow** (this is why deleting `PYO3_PYTHON = sys.executable` from `build_importer.py:300` is safe — the variable moves to CI, it does not disappear); `cargo build --release`; rename `cod_asset_importer.dll` → `cod_asset_importer.pyd`. |
| macos-arm64 | `macos-15` | `MACOSX_DEPLOYMENT_TARGET=11.0`; `RUSTFLAGS='-C link-arg=-undefined -C link-arg=dynamic_lookup'` (carried verbatim from the deleted `build_importer.py:303-312`, where the link requires it); `cargo build --release`; rename `libcod_asset_importer.dylib` → `cod_asset_importer.abi3.so`. |

Each job asserts `XMODEL_LOD_API_VERSION == 1` and `callable(inspect_xmodel_lods)` before upload, and regenerates `PROVENANCE.md` hashes. Artifacts upload as `importer-<target>`. Add `rust-toolchain.toml` pinning the exact rustc that was measured (paste `rustc -vV` into the same commit) — PyO3 0.20.0 is a 2023-era pin and its `non_local_definitions` warnings will become errors on some future edition bump. There is no `rust-toolchain.toml` under `tools/cod-asset-importer/rust/` today.

**`make importer-fetch` (M2 deliverable, and the dev-checkout lifeline).** After the v3.5 zips are deleted and `_cargo_build` is removed, a developer on a fresh clone has neither a prebuilt nor a documented way to build one, and every Blender step that `sys.path.insert`s the importer directory (`pipeline.py:1085` + five siblings) fails. `make importer-fetch` runs `gh run download --name importer-<target>` for all three targets into `packaging/importers/<target>/` and installs the host-matching one into `tools/cod-asset-importer/python/cod_asset_importer/`. `packaging/importers/` is the same input M3 consumes. Building from source with `cargo build` remains possible but is no longer an automatic fallback.

**Then:** delete the five v3.5.0 zips (**6,614,931 B** measured — the earlier draft's 6,614,936 B included the 5-byte `release/.gitignore`, which is not one of the zips) so `_extract_from_release` cannot pick a stale one, and rewrite the tests **in the same commit**:

- `tests/test_build_importer.py:48-75` (`test_bundled_release_extensions_cover_common_desktop_hosts`) → `build(require_lod=True)` against `packaging/importers/`, asserting `artifact_lod_api_version() == 1`. **It will fail today. That is the correct signal.**
- `tests/test_build_importer.py:118-152` (`test_authored_lod_requirement_rebuilds_lod0_only_binary`) does `mock.patch.object(build_importer, "_cargo_build", …)` and raises `AttributeError` the moment `_cargo_build` is removed. Delete it alongside.
- `tests/test_build_importer.py:77-89` (`test_bundled_macos_extension_matches_current_host`) reads the on-disk `tools/` artifact directly; repoint it at the artifact cache.

macos-x86_64 and linux-arm64 are deliberately absent in Phase A (see §12 Q1 and Q11) and return in Phase B.

### 8.2 `.github/workflows/exporter.yml`

| Job | Runner | Produces |
|---|---|---|
| `win-x64` | `windows-2025` | frozen shell + payload, Authenticode-signed |
| `linux-x64` | `ubuntu-24.04`, container `quay.io/pypa/manylinux_2_28_x86_64` | frozen shell + payload (glibc floor matching Blender's own). The image has no Tcl/Tk, so the frozen interpreter must be a python-build-standalone variant that carries Tk (§5.1). |
| `mac-arm64` | `macos-15` | frozen shell, `.app` assembly, sign, notarize, staple |

`packaging/build_exporter.py --platform <p>` is the single entry point:

1. **Validate the Blender pin without downloading it into the payload.** Range-request the first bytes of each pinned URL to confirm it is still live, fetch blender.org's published checksum file, and assert it equals `packaging/blender_pin.json`'s `sha256`. **Fail the build on any mismatch or 404.** This is the standing guard against the single failure mode a runtime fetch introduces that a bundled payload could not have — the pinned archive moving or changing under us (risk 7) — and it is cheap enough to run on every build. A separate scheduled weekly job runs only this check, so link rot is discovered on a Tuesday rather than during a release.
2. Bake the validated pin into `exporter/build_stamp.py` and mirror it into `payload.json`. **Blender itself is never downloaded by this workflow and never enters any artifact.**
3. `pyinstaller --clean --noconfirm packaging/fod_exporter.spec` (onedir, generated `hiddenimports`, **no `--codesign-identity`** — §8.3).
4. **Assemble `tools/` from `packaging/tools_manifest.txt`, an explicit allow-list — not from deny patterns.** The earlier draft's deny-list (`audit_*`, `report_*`, `diagnose_*`, `render_*`, `test_*`, plus four named files) is the wrong instrument and provably leaks: it does not match `extract_cod1_pavlov_entities.py` or `sync_cod1_unity_weapons.py`, and it says nothing about `tools/blender_asset_gen/` (84 KB) or `tools/ui_asset_gen/` (152 KB), all four of which ship to every customer today. The manifest is generated by an AST import-closure walk over `build_steps`' entry tools and is **26 modules** (measured; `tools/` holds 50 `.py` files, so 24 are unreachable):

   `batch_export_cod1_models, build_pavlov_sky_panorama, cod1_archive_policy, cod1_mp_gsc_content, cod1_multiplayer_closure, cod1_playeranim, cod1_script_exploder, cod1_shipping_maps, cod1_weapon_metadata, cod1_xanim, export_cod1_demo_viewmodels, export_cod1_multiplayer_players, export_cod_multiplayer_props, extract_cod1_assets, extract_cod1_footsteps, extract_cod1_hud, extract_cod1_impacts, extract_cod1_map_previews, extract_cod1_mp_gsc_content, extract_cod1_ordnance, extract_cod1_weapon_presentation, extract_cod_multiplayer_model_assets, fod_decal_alpha, fod_export_common, fod_glb_writer, import_cod_multiplayer_maps`

   plus `tools/cod-asset-importer/`. `tests/test_tools_manifest.py` (a) regenerates the closure and fails if it disagrees with the manifest, naming the offending `file:line`, and (b) asserts every `tools/*.py` is either listed or matched by a rule in `packaging/tools_exclude.txt`, so a new import can never silently leave a module out and a new dev tool can never silently ship. `build_exporter.py` reads the manifest; it applies no patterns at build time. Directory exclusions carried verbatim from `FriendsOfDutyMacBuild.cs:430-438` (`__pycache__`, `.venv`, `.git`, `OpenAssetTools`, `BetterBlenderCOD`, `target`) still apply inside `tools/cod-asset-importer/`.
5. **Compute `PAYLOAD_SHA256` = sha256 over a sorted manifest of `(relpath, sha256)` for every payload file EXCEPT `build_stamp.py` and `payload.json`**, plus the literals `BLENDER_VERSION`, `BLENDER_SOURCE_SHA256`, `IMPORTER_VERSION`, `IMPORTER_NATIVE_SHA256`, `PYTHON_VERSION`. Write `exporter/build_stamp.py` containing only build-varying constants (`PAYLOAD_SHA256`, `EXPORTER_BUILD`, `BLENDER_VERSION`, `IMPORTER_VERSION`, plus `toolchain_for(step_key)` from §6.1) and mirror the same values into `payload.json` for human and CI inspection. **No second freeze pass** — `build_stamp.py` is a loose file, so the earlier draft's "re-freeze (two-pass, because the stamp covers the payload)" was both circular and unnecessary. `STEP_WEIGHTS` does **not** live here; it lives in `packaging/step_weights.json` (§7.2).
6. Sign (§8.3).
7. **Smoke test — the gate that matters.** Two artifacts with a clean split:
   - **`--fod-selftest` lives in `fod_launcher.py` and ships**, because it is also the support tool for "the exporter won't start". It runs the in-payload assertions and emits machine-readable output:
     1. the frozen shell imports `PIL`, `numpy`, `tkinter`; `numpy.__version__` matches the lockfile;
     2. `PIL.Image.open` decodes a synthetic DXT1 `.dds`, type-2 `.tga`, `.jpg` and `.png`, and `set(PIL.Image.ID) >= {"DDS","TGA","JPEG","PNG"}`;
     3. `--fod-run-tool` **every module in `tools_manifest.txt`** with `--help` (or a `--fod-import-check` no-op added to the four that take positional argv), asserting exit 0 — this executes every module-scope import in the payload and is the only mechanical guard on the manifest;
     4. `import ssl`, `import lzma` and `certifi.where()` succeed, and a TLS handshake against the pinned host completes — the three provisioner dependencies that no existing code exercises (§5.1);
     5. **`--fod-selftest --provision`** (opt-in, because it downloads ~390 MB) runs the real provisioner end to end into a scratch cache: fetch, SHA-256 verify, extract, then `--background --factory-startup --python <probe>` reporting `bpy.app.version == (4, 5, 1)`, `bpy.ops.export_scene.gltf` present, and **all three private hooks resolving** — `io_scene_gltf2.blender.exp.exporter.GlTF2Exporter.__append_unique_and_get_index`, `io_scene_gltf2.io.com.gltf2_io.Animation`, `io_scene_gltf2.blender.exp.animation.sampled.sampling_cache.get_range` — and that same Blender importing `cod_asset_importer` from the loose path, reporting `XMODEL_LOD_API_VERSION == 1`, with `wrapper.parent.glob("cod_asset_importer.*")` yielding **exactly one** `.so`/`.pyd`. Without `--provision` the selftest asserts only that a *resolvable* Blender passes those checks, so it stays fast and offline-safe for support use. **CI runs `--provision` on every platform**: it is the only mechanical proof that the whole acquisition path works, and it is the step most likely to break silently;
     6. a synthetic armature + cube exports to GLB, parses, and the Blender-side tool emits its success sentinel before `sys.exit(0)`;
     7. a CJK and an accented path round-trip through `--fod-run-tool` argv and print without an encoding error.
   - **`packaging/smoke_test.py` is CI-only**: it invokes `--fod-selftest`, parses its output, and adds the assertions that need the *build tree* rather than the runtime — zero symlinks in the `.app`, file magic per platform, `LICENSES/` completeness, no dev-only tool or `build_importer.py` or release zip present, and `cargo build --release --offline` of the shipped Corresponding Source (§9.2).
8. Emit `Builds/<platform>/Exporter/`.

**A separate regression job, because the smoke gate is CoD-free by necessity.** `exporter-regression` runs on release tags only, on the **self-hosted Windows runner in §8.6** (the only machine that legally holds CoD), executes a full `--cli` export and diffs the produced pak against `tests/data/reference_pak_manifest.json` (the M0 deliverable). Failing it blocks the tag. GitHub-hosted runners can never run this; say so in the workflow file. Without it, a numpy bump in `requirements-3.13.lock` silently changes 391 MB of GLBs with green CI, because §10 risk 8's mitigation is a one-time M5 measurement, not a standing gate.

**Artifact hygiene.** Each payload is ~66-68 MB, so all three fit comfortably inside GitHub Free's 500 MB storage — the constraint that made the bundled design's CI barely runnable is gone. Keep `retention-days: 7`. `macos-15` still bills at a 10x minute multiplier, and `--fod-selftest --provision` adds a ~390 MB download per platform per run; run it on push to `main` and on tags, not on every PR commit (§12 Q3).

### 8.3 Signing and notarization

**macOS — the keychain steps are part of the plan, not an implementation detail.** A fresh `macos-15` runner has no keychain; `security import` into the default login keychain fails headless and `codesign` hangs on a UI prompt without `set-key-partition-list`. `packaging/sign_macos.sh` runs, in order:

```
security create-keychain -p "$MACOS_KEYCHAIN_PASSWORD" fod.keychain-db
security set-keychain-settings -lut 21600 fod.keychain-db
security unlock-keychain -p "$MACOS_KEYCHAIN_PASSWORD" fod.keychain-db
security default-keychain -s fod.keychain-db
echo "$MACOS_CERT_P12_BASE64" | base64 -d > cert.p12
security import cert.p12 -k fod.keychain-db -P "$MACOS_CERT_PASSWORD" -T /usr/bin/codesign
security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k "$MACOS_KEYCHAIN_PASSWORD" fod.keychain-db
trap 'security delete-keychain fod.keychain-db' EXIT
```

Required CI secrets, named explicitly: `MACOS_CERT_P12_BASE64`, `MACOS_CERT_PASSWORD`, `MACOS_KEYCHAIN_PASSWORD`, `MACOS_TEAM_ID`, `ASC_KEY_ID`, `ASC_ISSUER_ID`, `ASC_KEY_P8_BASE64`.

**The submission is now only our own code.** No Blender is nested, so we sign our own binaries inside-out (nested Mach-O in `Contents/Frameworks` first, `.app` last) with our Developer ID and hardened runtime on, and submit a ~66 MB bundle. The fetched Blender is never signed by us, never submitted and never stapled — it runs on Blender Foundation's own Developer ID Application signature (68UA947AUU, hardened runtime `flags=0x10000`, secure timestamp 2025-07-29, all measured on this machine), which the provisioner asserts at §5.2 step 5.

**Entitlements start EMPTY.** The earlier draft justified `com.apple.security.cs.disable-library-validation` as "required to exec BF-signed code". That is wrong: library validation is an **in-process** dylib-loading rule and has nothing to say about `posix_spawn`. Everything our frozen app loads in-process lives in `Contents/Frameworks` and is signed by us. Shipping an unnecessary hardened-runtime exception weakens the posture for no benefit. **M0 determines the minimum entitlement set empirically and records it**; add exceptions only against an observed failure.

Notarization is `xcrun notarytool submit --key <p8> --key-id $ASC_KEY_ID --issuer $ASC_ISSUER_ID --wait` (`--keychain-profile` does not exist on a fresh runner; `altool` is dead), then `stapler staple`, then `spctl -a -vvv -t exec` as the build gate.

**Never `codesign --deep` for signing.** Apple deprecated it as of macOS 13 and calls it "almost never the right option for signing an app with nested code". PyInstaller uses it by default; therefore PyInstaller must not sign. **The repo already uses it**: `AdHocSign` (`FriendsOfDutyMacBuild.cs:277-283`) runs `/usr/bin/codesign --force --deep --sign -` and is called at `:113`, immediately before `CopyBesideBuild` at `:114`. M8 replaces it with an inside-out Developer ID pass, the `--deep` flag goes with it, and **the exporter payload must be installed and signed *before* the outer `.app` is sealed**, not after. (The `--verify --deep --strict` at `:351` is a *verification* use and stays; `--deep` is only deprecated for signing.)

Steam has required all new macOS applications to be 64-bit and notarized since 2019-10-14. The game is currently only ad-hoc signed, so this raises the bar for the whole macOS release: Apple Developer Program enrolment, a Developer ID `.p12` and an ASC API key as CI secrets.

**What M0 still has to prove, now that the notarization scale problem is gone.** The old spike (a ~870 MB submission over ~200 third-party Mach-O files, undocumented by Apple, unfixable if a single BF binary lacked hardened runtime) is deleted with the bundling. Two smaller questions replace it, and both are genuinely open:

1. **Does a downloaded-and-extracted Blender.app run without a Gatekeeper prompt?** It has a valid Developer ID signature but **no stapled ticket** (measured: `xcrun stapler validate` -> "does not have a ticket stapled to it" -- BF staples the DMG, not the app). We `posix_spawn` the inner Mach-O rather than `open`ing the bundle, and a file written by our own process should carry no `com.apple.quarantine` xattr, so it should execute with at most an online notarization check. **Verify on a clean Mac that has never seen Blender**, both online and with the network disabled after provisioning, and record whether `spctl -a -t exec` passes and whether any prompt appears.
2. **The minimum entitlement set**, determined empirically from empty (below).

Both are hours, not days. Prove them before M6 writes the provisioner, not before M1.

**Windows.** Sign `FriendsOfDutyExporter.exe` and `cod_asset_importer.pyd` with `signtool.exe sign /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 /v` (Windows SDK, present on `windows-2025`). **Since the CA/Browser Forum's 2023-06-01 code-signing baseline, newly issued OV *and* EV private keys must reside on FIPS 140-2 L2 / CC EAL4+ hardware — you cannot export a `.p12` into a GitHub secret.** The CI job therefore uses a cloud signing service exposing a KSP/dlib — DigiCert KeyLocker (`smctl sign`), SSL.com eSigner (`CodeSignTool`), or Azure Trusted Signing (`Azure.CodeSigning.Dlib` via `signtool /dlib`) — recorded in `packaging/sign_windows.ps1`. If a physical hardware token is used instead, the Windows sign step must move to the self-hosted Windows box in §8.6. Blender's DLLs keep BF's own signatures — most of the payload's bytes are therefore already reputable, which is the best possible Defender/SmartScreen profile.

**Linux.** Nothing to sign; assert exec bits.

### 8.4 Unity build integration

`FriendsOfDutyExporterPayload` (`Assets/Editor/FriendsOfDutyMacBuild.cs:424-686` — the class opens at `:424`, not `:540`) moves into its own file. `CopyBesideBuild`'s raw `CopyTree(exporterSource)` + `CopyTree(toolsSource)` (`:556-607`) — the direct cause of both live depot defects — is replaced in two stages:

- **M3 (no CI-exporter dependency):** keep copying loose source, but replace the blanket `CopyTree(toolsSource)` with a per-target importer selection reading `packaging/importers/<target>/` (populated by `make importer-fetch`, §8.1), apply the `tools_manifest.txt` allow-list, and add the magic-byte assertions.
- **M8:** `InstallPrebuiltExporter(buildOutputRoot, BuildTarget)` unpacks the full CI payload for **that** target and verifies it.

**`InstallPrebuiltExporter` must delete `<buildRoot>/Exporter` in full before unpacking**, preserving `CopyBesideBuild`'s existing `Directory.Delete(destination, true)` semantics (`:575`). A partial overlay would leave a superseded `blender/<version>/` tree in the depot after a 4.5.1 → 4.5.2 bump, doubling an already ~1 GB payload.

Add `RequireExternalPayloadFile`-style assertions to all three client builds (only macOS has them today, `:332-347`, helper at `:355`), add a `RequireExternalPayloadDirectory` sibling for the `.app`, and **move `VerifyWindowsBuild` / `VerifyLinuxBuild` to after the copy** (`FriendsOfDutyWindowsBuild.cs:70-71`, `FriendsOfDutySteamOsBuild.cs:82-83`). Each must assert:

- the launcher binary exists **and its file magic matches the target** (`MZ` / `\x7fELF` / Mach-O);
- `blender/…/blender*` exists and matches the target's executable format;
- `cod_asset_importer.{pyd,abi3.so}` exists and its magic matches the target;
- **no `Exporter/blender/` directory exists at all** -- Blender must never enter a depot under this design, and a stray one would be both a GPL conveyance we have not prepared for and ~1 GB of unintended payload;
- `payload.json` carries a `blenderPin` block whose `sha256` matches `packaging/blender_pin.json`;
- the payload's total byte count is within ±5% of the value recorded in `packaging/blender_pin.json`;
- `LICENSES/` and `payload.json` exist;
- **none of these paths exists**: `Exporter/build_importer.py`, `Exporter/tools/cod-asset-importer/release/`, `Exporter/tools/blender_asset_gen/`, `Exporter/tools/ui_asset_gen/`, or any of the 24 unreachable dev tools, `BetterBlenderCOD`, `OpenAssetTools`.

The magic check alone would have caught both live defects at build time instead of at depot upload. Extend `VerifyMacBundle` to cover `Exporter/Friends of Duty Exporter.app` as well as `Friends of Duty.app`, and add `spctl -a -vvv -t exec` on the exporter app. The dedicated server build continues to omit the payload deliberately (`FriendsOfDutyDedicatedServerBuild.cs:88-96`).

**Payload migration.** SteamPipe removes only files absent from the new manifest, so the "removed from payload" list above is not cosmetic: an incomplete exclusion leaves today's rustup-prompting `build_importer.py` on disk beside the new bundle. A post-install assertion (`--fod-selftest`) checks that none of those paths exists.

**Makefile targets to add:** `importer-matrix`, `importer-fetch`, `exporter-payload`, `exporter-smoke`, `blender-pin`, with `exporter-payload` a prerequisite of `windows-build` (`Makefile:317-318`), `steamos-build` (`:320-321`) and `macos-build` (`:176`), inherited by `build` (`:336-345`).

**The Windows build host — the exact sequence, because the naive one destroys the payload.** `SteamPipe/build_all.sh` does `rm -rf "$PROJECT_DIR/Builds/Windows"` (`:324`) then `mv "$staging/Windows" "$PROJECT_DIR/Builds/Windows"` (`:325`): the whole directory is replaced by the scp'd zip. Anything fetched into `Builds/Windows/Exporter/` beforehand is deleted. The order is:

1. `scp` + unzip + swap `Builds/Windows` (`:308-326`), with a new PowerShell filter line `if ($rel -like 'Exporter\*') { continue }` added beside the existing `Content\*`, `*_BackUpThisFolder_*` and `*_BurstDebugInformation_DoNotShip*` filters at `:246-248`. This is what keeps the wire at 63,005,163 B (m) instead of ~1.05 GB per release.
2. `fetch_exporter_payload()`: `gh run download --repo <org>/FriendsOfDutyUnity --name exporter-win-x64 --dir "$PROJECT_DIR/Builds/.exporter-win"` (requires `gh auth login` on the Mac, or `GH_TOKEN` in the operator's environment), then `rsync -a --delete` into `Builds/Windows/Exporter/`. The CI artifact is now the complete payload -- there is no local Blender assembly step -- so this is a download and a copy. Verify `payload.json`'s `exporterBuild` equals the version stamped by the Windows player build. If `gh` is missing or the artifact is absent, **abort with exit 3 and a named remedy** -- never upload a Windows depot with a stale or missing `Exporter/`.
3. Guard: abort the upload if `Builds/Windows/Exporter/FriendsOfDutyExporter.exe` is absent.
4. `python3 SteamPipe/verify_depots.py --files`.
5. `SteamPipe/upload_all.sh` → `steamcmd +run_app_build`.

### 8.5 SteamPipe depot changes

All four files live under `SteamPipe/scripts/`.

- **`SteamPipe/scripts/depot_build_4480882.vdf` (Linux, `"LocalPath" "*"` recursive at `:6-11`).** Its `FileProperties` blocks (`:24-28`, `:30-34`) cover only `FriendsOfDuty.x86_64` and `FriendsOfDuty.sh`. Today's shim survives only because `ExporterLauncher.cs:660-661` invokes `/bin/sh <script>`, which ignores the exec bit — a bundled ELF has no such escape. Add executable properties for `Exporter/FriendsOfDutyExporter` and `Exporter/run_exporter.sh`. No Blender paths appear here -- the extracted Blender lives outside the depot in a per-user directory (§4.4), where `tarfile` preserves its modes and `fod_paths` re-applies them defensively. Rely on `ExporterLauncher`'s pre-spawn `File.SetUnixFileMode` for the exporter's own ELF (§6.4).
- **`SteamPipe/scripts/depot_build_4480883.vdf` (macOS).** Maps only three explicit subtrees (`:6-25`): `Friends of Duty.app/*`, `Exporter/*`, `controller_config/*`; anything outside them is dropped at upload **with no error**. The bundle must stay under `Builds/macOS/Exporter/`. The only `FileProperties` block today is for the game binary (`:39-43`); add an executable property for `Exporter/Friends of Duty Exporter.app/Contents/MacOS/FriendsOfDutyExporter`. Nothing Blender-related appears in this depot.
- **`SteamPipe/scripts/depot_build_4480881.vdf` (Windows).** No change.
- **`SteamPipe/scripts/depot_build_4480884.vdf` (server).** Delete the stale `FileProperties` for `Exporter/run_exporter.sh` (`:64-68`) — the same file already excludes `Exporter/*` at `:50`.
- **All four `"FileExclusion" "Content/*"` lines survive verbatim** (`4480883:29`, `4480882:15`, and the siblings). They are the mechanical enforcement of the "no CoD assets in the depot" guarantee.
- Run `python3 SteamPipe/verify_depots.py --files` before every upload, **on a host where `Builds/macOS/Content` and `Builds/SteamOS/Content` are populated** (they are here: 941 MB) — that is the only condition under which the exclusion is actually being tested.

### 8.6 Acceptance hardware (new — nothing in the plan worked without it)

| Machine | Role | Requirements | Owner |
|---|---|---|---|
| Clean Windows 11 x64 box | M8 acceptance; **self-hosted runner for `exporter-regression`**; fallback Authenticode signing host if a hardware token is used | Legally-owned CoD1 + UO install; Steam access to a private beta branch; Defender real-time protection **ON** for the M5 timing run | TBD (§12) |
| Steam Deck or SteamOS 3.x machine | §10 risk 6; M5 Blender probe under all three Steam compatibility settings (none / SLR 1.0 scout / SLR 3.0 sniper); M8 Linux acceptance via the in-game `--cli` flow in **Gaming Mode** | CoD1 + UO on internal or microSD storage; beta branch | TBD |
| Clean Apple-silicon Mac **that has never seen the developer certificate** | M0's `spctl` criterion; M8 acceptance | CoD1 + UO; beta branch | TBD |
| Intel Mac | Only if §12 Q1 is answered "universal" | CoD1 + UO | TBD |

`Docs/EXPORTER_ACCEPTANCE.md` holds the manual checklist these machines run, with wall-clock and peak-disk recorded per platform.

---

## 9. Licensing & compliance

### 9.1 The boundary

| Component | License | Ships as | Enters whose process |
|---|---|---|---|
| `Friends of Duty` (Unity, IL2CPP, Steamworks.NET) | proprietary | game binary | its own |
| Frozen exporter shell (CPython, numpy, Pillow, Tcl/Tk, our orchestrator) | proprietary + permissive deps | `FriendsOfDutyExporter*` | its own |
| Blender 4.5.1 LTS, **stock unmodified** | GPL-3.0-or-later | **not shipped** -- fetched by the player's machine from blender.org (§5.2) | its own, spawned over argv |
| `cod_asset_importer` v3.6, **modified** | GPL-3.0 | loose `.so`/`.pyd` + full source | **Blender's only** |
| The `bpy`-importing `tools/*.py` | GPL (declare explicitly) | loose readable source | Blender's only |

The exec boundary is load-bearing. The FSF's stated criterion is that command-line arguments are a communication mechanism normally used between two *separate* programs, while modules in the same executable file are definitely one program. Blender Foundation publishes a matching carve-out: proprietary software may keep its own license if it "operates outside of Blender", uses "no… API calls (including Python API)", and "executes Blender". `exporter/pipeline.py:226-232` satisfies the exec half; the `bpy`-importing scripts do not, which is why they stay loose and get an explicit GPL declaration rather than being frozen into a proprietary binary.

**Two hard invariants, to be written into `Docs/` and CI-enforced:**

1. **No module reachable from `fod_launcher` or from any host (`--fod-run-tool`) step may import `bpy` or `mathutils`.** The earlier draft's formulation — "the frozen exporter binary's module graph must never contain `bpy`" — is **vacuous and gives false assurance**: because every tool is loose and executed via `runpy.run_path`, PyInstaller analyses only `fod_launcher.py`, so no `tools/*.py` is ever in the frozen graph and the check passes trivially while proving nothing about the boundary the licensing position rests on. Enforce it instead with an AST import-closure scan over the 11 host-step entry tools plus `exporter/package.py` and their local-import closure, run in CI. Verified true today: none of `cod1_archive_policy`, `cod1_playeranim`, `cod1_multiplayer_closure`, `cod1_shipping_maps`, `cod1_mp_gsc_content`, `cod1_script_exploder`, `cod1_xanim`, `extract_cod1_mp_gsc_content`, `fod_glb_writer`, `import_cod_multiplayer_maps`, `fod_decal_alpha` or `build_pavlov_sky_panorama` imports `bpy` — but nothing enforces it. **Phase B corollary:** `tools/fod_glb_writer.py`, `fod_scene.py`, `fod_rig_math.py` and `fod_mesh_ops.py` will run on *both* sides and must never gain a `bpy` import; a violation there moves GPL code into the proprietary process.
2. The Unity player must never load Blender, `bpy`, or the GPL `.so`. The only bridge between game and exporter is `Assets/Scripts/FodNativeProcess.cs`'s posix_spawn / CreateProcess. No P/Invoke. Valve's public rule is scoped to *combining code with the Steamworks SDK*; the game links Steamworks.NET (MIT wrapper, `Packages/manifest.json:3`) and spawns the exporter arms-length. Valve explicitly declines to adjudicate and pushes the warranty back to the partner — this is a fact pattern to confirm with counsel, not a permission granted.

Note: `tools/report_cod1_xmodel_bind.py:14` imports `cod_asset_importer.cod_asset_importer` bare, with no bpy. It is dev-only, unreachable from the closure, and excluded by `tools_manifest.txt`; §8.4's assertion covers it.

### 9.2 What must ship

`Exporter/LICENSES/`, one subdirectory per component, in every client depot:

- `blender/` — **not a distribution obligation any more, and kept anyway as a courtesy notice.** We never convey Blender: the player's own machine fetches the official binary from blender.org, so no GPLv3 §4/§5/§6 obligation attaches to us for it, and there is no §6(d) source-availability duty to discharge. The directory still carries the verbatim GPLv3 text, the exact version (`4.5.1 LTS`, build hash `b0a72b245dcf`), the pinned binary URL and its SHA-256, and a pointer to `download.blender.org/source/` — because the exporter downloads and executes it, players deserve to be told what it is and under what licence, and the pin is the honest record of exactly which build ran. **If we ever host or mirror the archive ourselves — including an S3 fallback added to work around risk 7 — conveyance resumes and every obligation in this bullet becomes real.** That is the single decision that would flip this back, and it must not be made casually as an availability fix. See §12 Q4.
- `cod-asset-importer/` — GPLv3 text, `PROVENANCE.md`, a new `MODIFICATIONS.md` enumerating all 8 modified files with dates, and the **complete corresponding source**: `python/`, **the whole `rust/` directory including the `valid_enum` path-dependency crate** (`rust/cod_asset_importer/Cargo.toml:18` declares `valid_enum = { version = "*", path = "../valid_enum" }`, and `rust/valid_enum/` exists as a sibling — shipping only `rust/cod_asset_importer/src/` produces a tree that cannot build, which is exactly what GPLv3 §1 forbids and would also break §8.1's CI), `Cargo.toml`, `Cargo.lock`, `pyproject.toml`, `setup.py` and `rust-toolchain.toml`. **Verified by a CI step** that copies `LICENSES/cod-asset-importer/rust/` into a scratch directory and runs `cargo build --release --offline` in it. A source drop that does not build is the most common GPL compliance defect and it is machine-checkable.
- `cpython/` (PSF-2.0), `numpy/` (BSD-3), `pillow/` (MIT-CMU/HPND), `tcl-tk/`, `rust-crates/`, `steamworks.net/` (MIT).
- `rust-crates/` is generated by `cargo-about` over the **49 dependency entries** in `tools/cod-asset-importer/rust/cod_asset_importer/Cargo.lock` (50 `[[package]]` blocks including the crate itself). The local path crate `valid_enum` has no registry metadata and needs a hand-written entry.
- A top-level `NOTICE.txt`.

There is currently **no** `THIRD_PARTY`, `NOTICE`, `LICENSES`, `CREDITS` or `ATTRIBUTION` file tracked anywhere in the repository, and no about/legal screen in the game or the exporter. Every permissive license in the stack requires notice reproduction on binary distribution; that obligation is presently unmet across the board, independent of anything in this plan. Add a "Legal / Open Source" entry to the main menu pointing at the directory.

### 9.3 Corrections owed before the first paid build

- ~~**`tools/cod-asset-importer/PROVENANCE.md:67` states "No upstream source files were modified."**~~ **FIXED 2026-08-10.** It was false: 8 files, 194 insertions, 27 deletions, including a new public API symbol `XMODEL_LOD_API_VERSION` added to `src/lib.rs`, and `python/cod_asset_importer/__init__.py:5` bumped 3.5.0 → 3.6.0. GPLv3 §5(a) requires prominent notices stating modification and giving a date. Add them to each touched file and fix the record. (The line is `:67`, not `:47`; `:47` is the macOS x86_64 archive's SHA-256.)
- ~~**Publish the fork** at a stable public URL under GPLv3 as the §6(d) Corresponding Source location.~~ **DONE 2026-08-10:** <https://github.com/anarqz/cod-asset-importer>, tag `v3.6.0`. Reference this URL from `LICENSES/cod-asset-importer/` when M8 generates it.
- **Rewrite `Docs/STEAM_RELEASE.md:82-92` in full.** (a) Correct `:87-89` — all five prebuilts exist but every one is LOD-API-0 and refused by production prop export, and the Windows depot ships none of them; after M2 the sentence describes CI-produced v3.6 artifacts. (b) Replace `:84-86`'s "`exporter/` + `tools/` from the repo root, minus …" copy description with the `InstallPrebuiltExporter` + `tools_manifest.txt` contract. (c) Clarify `:90-91` to: "the depot mappings upload `Exporter/` as-is — never add a `FileExclusion` for it. The Windows **transport** zip is a separate matter: `Exporter/` is excluded there and fetched from CI on the staging Mac (§8.4)." Leaving these means the next person to run a release follows a doc that contradicts the plan.

### 9.4 Blocking, unrelated to this project

**`github.com/anarqz/fodpak` is a PUBLIC 1.25 GB repository containing the CoD1+UO-derived package** (`gh repo view` → `"visibility":"PUBLIC"`, `diskUsage` 1,256,969 KB), wired in at `.gitmodules:3` as `url = git@github.com:anarqz/fodpak.git`, and therefore trivially discoverable by anyone who reads the repo. As written, `Docs/CONTENT_PIPELINE.md:11-13` and `exporter/README.md:5-6` assert that packages "must not be publicly redistributed"; the depot side of that guarantee is mechanically enforced by the four `"FileExclusion" "Content/*"` lines, this side is not.

**OWNER DECISION (2026-08-10): the repository stays PUBLIC for now.** It is a testing artefact and is not part of any shipped build — no depot contains it, and the game never fetches it. This is recorded as an accepted, owner-held risk rather than an open action item, and it blocks nothing in this plan.

Two consequences that do remain in scope, because they are cheap and they are what makes the decision reversible later:

1. **Revisit before the store page goes live.** Public availability of a CoD-derived package is a different exposure once there is a storefront pointing at the repository that references it. Re-put the question at that point; nothing here forecloses making it private then.
2. **`tests/data/reference_pak_manifest.json` is an M0 deliverable regardless of visibility.** `Content/Content/current` is the only reference pak in existence — the declared oracle for M5's numpy diff, M10's harness and M11–M15's entire go/no-go structure — and `.gitmodules:3` means the submodule going away at any point breaks `git submodule update` for every developer and every CI job. Committing sha256 of every file in the pak gives M5/M10 and the `exporter-regression` job a checked-in oracle that survives the repository changing visibility, moving, or being deleted. If it is ever made private, CI and `SteamPipe/build_all.sh` additionally need a deploy key or the submodule made optional.

Separately, if the repository is to stay public, `Docs/CONTENT_PIPELINE.md:11-13` and `exporter/README.md:5-6` should say what is actually true — the *shipped game* contains no CoD assets and the depots mechanically cannot carry them — rather than a blanket claim the development workflow contradicts.

Adjacent, same shipping decision: `Assets/Resources/Fonts/ConduitITCStd.otf` is a commercial ITC/Monotype typeface needing an app-embedding license, and `Assets/Resources/Audio/Music/At Ease Soldier - Unknown.mp3` credits an unidentifiable author and therefore cannot be cleared. Both go out inside the game build.

---

## 10. Risks & mitigations

| # | Sev | Risk | Mitigation | Status |
|---|---|---|---|---|
| 1 | Resolved | No v3.6 / LOD-API-1 importer BINARY exists for any target — the source now does (published 2026-08-10). No v3.6 prebuilt — all five bundled prebuilts are v3.5.0 with the symbol absent — and production prop export refuses API 0 (`export_cod_multiplayer_props.py:723-733`). All four targets therefore require rustup + a C++ linker today. | M1-M2: vendor the fork, build the CI matrix, add `make importer-fetch`, delete the v3.5 zips, delete `_cargo_build`. | Open |
| 2 | Resolved | `tools/cod-asset-importer` was gitignored with the only copy of the v3.6 patch as uncommitted state on one machine. | **Closed 2026-08-10:** published as a GPLv3 fork and added here as a submodule. Follow-up: every clone, CI job and `SteamPipe/build_all.sh` needs `--recurse-submodules`. | Done |
| 3 | Low | Public `anarqz/fodpak` carries the CoD-derived package (§9.4). | **Owner decision 2026-08-10: stays public — testing artefact, ships in nothing.** Accepted risk; revisit before the store page goes live. `tests/data/reference_pak_manifest.json` still lands in M0 so the oracle survives the repo changing. | Accepted |
| 4 | **Blocker** | **The first export requires a working internet connection and a ~350-390 MB transfer we own.** No connection, a captive portal, a metered link, a TLS-intercepting corporate proxy, or a player who simply expects an installed game to work offline all mean no pak. This risk did not exist in the bundled design and is the direct cost of the runtime-fetch decision. | Resume via HTTP `Range` + 3 retries with backoff (§5.2 step 2); certifi with a logged `load_default_certs()` fallback for intercepting proxies; a failure screen carrying the exact URL, SHA-256 and destination directory so the archive can be sideloaded from another machine; `--blender`/`FOD_BLENDER`/the Advanced Browse row as the manual path (§6.3). **M8 acceptance tests the network-disabled path explicitly.** The structural fix is Phase B. The packaging fix, if this proves worse than estimated, is the optional-DLC-depot option in §3.3, which solves rows 4-8 at once. | Open |
| 5 | **Blocker** | **Link rot on the pinned archive.** Blender 4.5 LTS reaches EOL 2027-07 and the pin is already two LTS generations behind (5.2 LTS shipped 2026-07-14). If `download.blender.org` moves, renames or prunes the 4.5.1 artifact, **every existing installation stops being able to export** -- not just new ones. Nothing in the bundled design could fail this way. | §8.2 step 1 validates the pinned URL and published checksum **on every build**, plus a scheduled weekly job so rot is found on a Tuesday. M6 must confirm blender.org's retention policy for EOL releases. Contingency is deliberately NOT "mirror it ourselves" -- that resumes GPL conveyance (§9.2) -- but a pin bump to a still-published build, which costs a full M8 smoke pass and an M10 harness re-run. **§12 Q4 asks the owner to decide the mirror question before it is needed at 2am.** | Open |
| 6 | High | **`ssl`, `certifi` and `lzma` are new to the frozen surface** -- today's codebase uses none of them (§2.1). A frozen `ssl` that cannot verify a certificate, or a missing `_lzma`, fails on the player's first export with an unhelpful message and is invisible to any test that does not actually download. | `--fod-selftest` asserts `import ssl`, `import lzma`, `certifi.where()` and a live TLS handshake; **`--fod-selftest --provision` runs the real download+extract+probe on every platform in CI** (§8.2 step 7). certifi is pinned in `requirements-3.13.lock`. | Designed |
| 7 | High | Blender's official Linux build needs host `libX11`, `libXi`, `libXxf86vm`, `libXfixes`, `libXrender`, `libGL` even in `--background`; Steam's runtime choice changes the loader environment; and on SteamOS the immutable root means a player **cannot install a missing library**. Under a runtime fetch this is worse than before, because the tarball is not something we validated against that host at build time. | M5: run the provisioner and then `blender --background --factory-startup --python` on a real Steam Deck under **all three** Steam compatibility settings (none, SLR 1.0 scout, SLR 3.0 sniper); ship the supported set as the launch-option requirement. `child_env()` **deletes** `LD_LIBRARY_PATH` rather than restoring `*_ORIG`, which under `FriendsOfDuty.sh:6` is already poisoned with the game dir and Steam's runtime paths. | Unknown |
| 8 | Med | **~800 MB-1.05 GB of per-user disk** for the extracted Blender, on a volume that is often not the one the player chose for the game -- `%LOCALAPPDATA%` on Windows, the internal drive on a Deck whose library is on microSD. Combined with the 1.75 GB export peak this can fill a small system volume. | M6's precheck stats **two** volumes (output, and the Blender cache) and names each in its error (§4.5). The cache directory is version-keyed so a pin bump does not transiently double it, and the old version is deleted only after the new one is promoted. | Open |
| 8a | Med | **Antivirus and EDR on Windows.** The exporter downloads a ~365 MB archive, extracts several thousand files including ~200 executables, and then spawns one of them. That is close to a textbook dropper heuristic, and it is a far stronger signal than the bundled design gave. | M5's timing run happens with Defender real-time protection **ON**; extraction goes to `%LOCALAPPDATA%` rather than `%TEMP%`; the exporter is Authenticode-signed (§12 Q6) and the fetched Blender carries Blender Foundation's own signature, so both binaries are reputable. Escalate to the DLC-depot option (§3.3) if a mainstream engine flags it. | Unknown |
| 8b | Med | **Two exporters racing on the cache.** The game can spawn `--cli` while a Tk exporter is open; two concurrent 390 MB downloads into one directory corrupts it. | Lock file in the cache root; the loser waits and reports it (§5.2 step 7). Download to `.tmp/`, extract to `4.5.1.incoming/`, promote by `rename` only after the integrity probe passes, so a partial tree can never be mistaken for a good one. | Designed |
| 8c | Low | **A downloaded `Blender.app` has a valid Developer ID signature but no stapled ticket** (measured -- BF staples the DMG, not the app). Gatekeeper behaviour for a bundle written by our process and exec'd via `posix_spawn` is expected to be fine but is not proven. | M0 spike on a clean Mac that has never seen Blender, online and offline (§8.3). If a prompt appears, the fallback is to fetch and retain the DMG and mount it per run, or revert to bundling. | Unknown |
| 9 | Med | numpy 2.4.3 on CPython 3.13 is assumed to reproduce the reference `maps` output. Not verified. **And nothing re-checks it after M5.** | M5 hash-diffs the maps step against `Content/Content/current` with `PYTHONHASHSEED=0`, after first proving run-to-run determinism. The standing gate is `exporter-regression` (§8.2) on the self-hosted CoD-holding runner, tag-only. Fallback: CPython 3.11 + numpy 1.26.4. | Unknown |
| 10 | Med | A global payload stamp in `_step_signature` would make every exporter patch a full ~810 MB re-export, contradicting §3.2. | Per-step `toolchain` key (§6.1); `EXPORTER_BUILD` never hashed; `PIPELINE_SCHEMA_VERSION` 3 → 4 with `tests/test_exporter_pipeline.py` and `tests/test_prop_export_fingerprint.py` updated in the same commit. | Designed |
| 11 | Med | `hiddenimports` is hand-maintainable only in theory; a frozen Pillow missing `DdsImagePlugin` fails at step 17 of a player's export. | Generated from `modulefinder` over `tools_manifest.txt`, build fails on any gap; smoke test decodes DDS/TGA/JPEG/PNG and asserts `PIL.Image.ID` (§8.2 step 7). | Designed |
| 12 | Med | The Windows exporter payload would multiply the existing 63,005,163 B `Builds/Windows.zip` scp to ~1.05 GB per release, and the naive fetch order is destroyed by `build_all.sh:324-325`'s `rm -rf` + `mv`. | Add `if ($rel -like 'Exporter\*') { continue }` at `build_all.sh:246`; fetch from CI **after** the swap, in the five-step order in §8.4; abort on a missing artifact. | Designed |
| 13 | Low | GitHub Actions cost: `macos-15` bills at a 10x multiplier, and `--fod-selftest --provision` adds a ~390 MB download per platform per run. **Storage is no longer a constraint** — three ~67 MB payloads fit inside GitHub Free's 500 MB, where three ~1 GB payloads did not. | Run `--provision` on push-to-`main` and tags, not every PR commit; `retention-days: 7`. Owner confirms the plan tier (§12 Q3a). | Owner decision |
| 14 | Med | Peak export disk is ~1.75 GB **on top of** ~2 GB of game + payload when the output resolves to the portable location (`FodContentPaths.cs:88-107`), with no free-space check, and on non-CoW filesystems the seed is a silent multi-minute `copytree` of 810 MB. | M6 adds a precheck against the **output volume** (+15% headroom, volume named in the error) and seed progress. | Open |
| 15 | Med | Antivirus/EDR during the export — the exporter reads ~1.5 GB of pk3s and writes ~2,092 GLBs plus 1,816 PNGs from a frozen binary that spawns another binary. Plausible multi-× wall-clock regression on the two dominant steps. | M5's timing run on the Windows acceptance box is performed with **Defender real-time protection ON**, which is every player's default; a second run with it off quantifies the delta for the docs. | Open |
| 16 | Med | Steam "Verify integrity of game files" against a populated `Content/current` (excluded from all four depots) has never been tested. | New M8 acceptance item on all three platforms. | Open |
| 17 | Med | `--factory-startup` changes the Blender environment relative to the one that produced the reference pak, silently moving Phase B's oracle at M4. | M4's done-criterion re-exports the 429 Blender-produced GLBs under the flag, compares them under the M10 harness, and **freezes the result as the recorded oracle** with its manifest committed. | Designed |
| 18 | Resolved | `importer.py`'s bare `except: return` would silently produce untextured models if `material.blend_method` were removed. | **Closed 2026-08-10:** typed except with a traceback, and a `_set_hashed_alpha()` helper that prefers whichever of `blend_method` / `surface_render_method` exists. Both present on 4.5.1, so behaviour there is unchanged. | Done |
| 19 | Low | The two glTF monkeypatches target private `io_scene_gltf2` internals and already miss on 4.2, costing 171.8 s vs 29.0 s per player profile. Verified to resolve on 4.5.1. | Pin 4.5.1; the five soft `note:` fallbacks in `fod_export_common.py` become hard `SystemExit` under `FOD_BUNDLED`; CI asserts all three targets resolve. Upstream the O(n²) fix eventually. | Designed |
| 20 | Low | Intel Macs lose the local exporter in Phase A. | Universal2 binary whose x86_64 slice is a legible-dialog stub + the remote-import path. Phase B restores them. Note Blender dropped official Intel macOS builds at 5.0, so 4.5.1 is also the last pin that *could* have served them. | Owner decision (Q1) |
| 21 | Low | Blender 4.5 LTS reaches EOL 2027-07-14 (blender.org; link it). Under a runtime fetch this compounds with row 5 rather than being purely cosmetic. | Recorded as a scheduled migration in `Docs/EXPORTER_BUNDLE.md`, tracked by §8.2's weekly pin check. Phase B removes the dependency before then, if it lands. | Scheduled |
| 22 | Low | No rollback if the notarized macOS payload fails on delivered installs. | Keep the previous Steam build available on a `previous` beta branch for one release cycle; the `run_exporter.*` shims plus loose-source payload remain re-enablable as a hotfix. Written into `Docs/STEAM_RELEASE.md`. | Designed |

---

## 11. Milestones

Every milestone has a machine-checkable definition of done. **M1-M4 are independently shippable and should start regardless of any decision about Phase A vs Phase B.**

### Schedule and ownership

Estimates are engineering days for one engineer; they exist so §12 Q3's "temporary, one quarter" claim can be checked rather than asserted.

| M | Owner | Est. days | Blocked by | Parallel with |
|---|---|---:|---|---|
| M0 gate + notary spike | owner + eng | 3 | — | — |
| ~~M1 vendor importer~~ **done** | eng | 2 | M0 | M10 |
| ~~M2 importer CI matrix~~ **done** | eng | 4 | M1 | M4, M10 |
| M3 platform-aware payload | eng | 3 | M2 | M4, M10 |
| M4 pin Blender + harden env | eng | 4 | M0 | M2, M3, M10 |
| ~~M5 measure & instrument~~ **done** | eng | 5 | M4 | M10 |
| M6 frozen-path plumbing **+ provisioner** | eng | 11 | M5 | M10 |
| M7 progress + game contract | eng | 6 | M6 | M10 |
| ~~M8 freeze/package/ship~~ **done** | eng | 9 | M7, M2, M0 | — |
| M10 harden harness | eng | 5 | M4 (oracle) | all of Phase A |
| M11 texture decoder | eng | 8 | M10 | — |
| M12 static path | eng | 10 | M11 | — |
| M13 skinned pilot **(go/no-go)** | eng | 10 | M12 | — |
| M14 players | eng | 12 | M13 | M15 |
| M15 viewmodels | eng | 10 | M13 | M14 |
| M16 delete Blender | eng | 4 | M14, M15 | — |

Phase A critical path: M0→M1→M2→M3→(M4)→M5→M6→M7→M8 = **47 engineering days**, ~10 calendar weeks at 70% utilisation — inside the quarter §3.1 claims, with M4 and M10 absorbing slack in parallel. The runtime-fetch decision moved three days from M8 (no Blender acquisition, assembly, stripping, or ~870 MB notarization) into M6 (the provisioner, its failure paths and its tests), and **deleted M9 entirely**; the total is unchanged, which is the honest answer — this decision traded packaging work for runtime work roughly one-for-one, and bought its real value in risk and payload size rather than schedule. Phase B adds 55+ days after M13 passes.

### Phase 0 — decisions and de-risking

**M0. Gate.** *Done when:*
- `tests/data/reference_pak_manifest.json` (sha256 of every file in `Content/Content/current`) is committed. (`anarqz/fodpak` visibility is **settled — it stays public**, §9.4; the manifest is required regardless, so the oracle does not depend on that repository still existing.)
- **§12 Q1-Q11 are answered in writing** (Q1→M8/M16, Q2→M8, Q3→M2/M8, Q4→risk 5 response, Q5→architecture confirm, Q6→M8, Q7→M8/§8.4, Q8→M5, Q9→M8, Q10→game release, Q11→M2);
- a throwaway ~66 MB bundle of our own binaries has been signed inside-out, submitted with `notarytool --wait` using an ASC API key on an ephemeral keychain, stapled, and passes `spctl -a -vvv -t exec` on a machine that has never seen the developer certificate — with wall clock recorded. (The old ~870 MB / ~200-nested-third-party-binary spike is deleted with the bundling decision.);
- the **minimum entitlement set** is determined empirically and recorded (start from empty);
- **risk 8c is settled**: a `Blender.app` downloaded and extracted by a plain HTTPS fetch, then exec'd via `posix_spawn` on the inner Mach-O, runs on a clean Mac that has never seen Blender — tested both online and with the network disabled after provisioning — with `spctl -a -t exec` result and any Gatekeeper prompt recorded;
- **the §5.2 archive URLs are confirmed live**, their published checksum format identified, and blender.org's retention policy for EOL releases established (risk 5).

### Phase A.1 — the shippable bug-fix release (no packaging work)

**M1. Vendor the importer. — DONE 2026-08-10.** Published at <https://github.com/anarqz/cod-asset-importer>, tag `v3.6.0`, as a GitHub fork of `mauserzjeh/cod-asset-importer`; `MODIFICATIONS.md` and per-file §5(a) notices landed; `PROVENANCE.md`'s false "No upstream source files were modified" corrected; `importer.py`'s bare `except:` and the four unguarded `Material.blend_method` writes fixed; `rust/rust-toolchain.toml` pins rustc 1.95.0; the built extension is gitignored rather than committed; added to this repo as a submodule.

**Two corrections to this milestone as originally written.** (a) It required the crate and addon versions to "agree". They should not: upstream numbers them independently (crate `1.0.0`, addon `3.5.0`), so the fork keeps that relationship at crate `1.1.0` / addon `3.6.0`. (b) Its done-criterion — `git ls-files tools/cod-asset-importer | wc -l ≥ 40` — was written for the flat-vendor route; under the submodule route the parent tracks a single gitlink by design. The criterion that actually matters, and which was met, is that a fresh `git clone --recurse-submodules` yields a working tree containing `XMODEL_LOD_API_VERSION`.

**M2. Importer CI matrix. — DONE 2026-08-10.** All four targets build and verify `XMODEL_LOD_API_VERSION == 1` at <https://github.com/anarqz/cod-asset-importer/actions>; `make importer-fetch` installs them; the v3.5 zips are deleted; the automatic cargo fallback is gone (`--from-source` survives as an opt-in); `SteamPipe/build_all.sh` recurses submodules. Original text:  `.github/workflows/importer.yml` for linux-x64, win-amd64, macos-arm64 with the commands in §8.1; `make importer-fetch`; `build_importer.py` loses `_cargo_build`, the `sys.executable -c` probe and `PYO3_PYTHON`, and `_extract_from_release` repoints at `packaging/importers/`; delete the five v3.5 zips; rewrite the three affected tests. *Done when:* each job asserts `XMODEL_LOD_API_VERSION == 1` before upload; `tests/test_build_importer.py` fails on the old artifacts and passes on the new ones; and **`make test` passes inside the `ghcr.io/pyo3/maturin` container**, proving the suite is path-portable (the `tests/` vs `Tests/` casing only resolves by accident on APFS).

**M3. Platform-aware payload + verification.** Per-target importer selection from `packaging/importers/<target>/`; `tools_manifest.txt` allow-list applied; `Verify*Build` moved after the copy; magic-matching and removed-path assertions in all three client builds. *Done when:* `file Builds/SteamOS/Exporter/tools/cod-asset-importer/python/cod_asset_importer/cod_asset_importer.abi3.so` reports `ELF 64-bit LSB shared object, x86-64`; `Builds/Windows/Exporter/tools/cod-asset-importer/python/cod_asset_importer/cod_asset_importer.pyd` exists and starts `MZ`; `Builds/*/Exporter/tools/blender_asset_gen` and `ui_asset_gen` are gone; and planting a Mach-O in the Linux slot fails `make windows-build`/`make steamos-build` with a named error.

**M4. Pin Blender + harden the export environment.** Version constants per §5.2 in **all verified sites**: `exporter/friends_of_duty_exporter.py:39, 108, 109, 417`; `Assets/Scripts/Content/FodToolchainProbe.cs:70, 97, 98, 102, 187, 465`; `Assets/Scripts/Frontend/FodBootFlow.cs:1556, 1656`; `Assets/Scripts/Content/ExporterLauncher.cs:569`; `Docs/CONTENT_PIPELINE.md:515, 741`; `exporter/README.md:13`. Add `--factory-startup` to `_blender()`; add `child_env()` scrubbing to `_run_subprocess`. Accept that the two `FodToolchainProbe.cs` constants and the two `FodBootFlow.cs` strings are edited here and deleted in M7 — M1-M4 must be shippable on their own. *Done when:* a full export on a machine with third-party Blender add-ons installed produces no add-on output; a 4.2 Blender is refused with a named reason; `grep -rn '4\.2' Assets/Scripts Docs exporter | grep -i blender` returns nothing; and **all 429 Blender-produced GLBs, re-exported with `--factory-startup`, compare equal under the M10 harness to `Content/Content/current`, with the re-exported set recorded as the frozen Phase B oracle and its manifest committed.**

> **First shippable slice: M1 + M2 + M3 + M4.** After these, a player who owns Blender 4.5 can complete a full export on Windows, Linux and macOS with no Rust toolchain, no C++ linker and no version-dependent output. That is a release, and it does not depend on any decision in §12 beyond M0.

### Phase A.2 — the bundle

**M5. Measure and instrument.** A cold full export against a clean output directory: per-step wall clock (especially `maps`, never measured), peak disk, peak RSS — **with Defender real-time protection ON** on the Windows box, plus a second run with it off. Run the `maps` step **twice with `PYTHONHASHSEED=0`** and confirm byte-identical output *before* comparing against `Content/Content/current` under pinned CPython 3.13 + numpy 2.4.3 + Pillow 12.1.1; if the two runs differ, fix the nondeterminism first — otherwise the reference diff is uninterpretable. Verify `blender --background --factory-startup --python` on a real Steam Deck under all three Steam compatibility settings, and measure Gaming-Mode CoD-path detection (microSD, second library, Proton prefix). Record the exact Blender build that produced the reference pak. *Done when:* `packaging/step_weights.json` is calibrated per-platform from real data, the maps diff is empty (or the fallback interpreter is chosen), and the Deck run succeeds under at least one named runtime configuration.

**M6. Frozen-path plumbing + the Blender provisioner, dev checkout only.** `exporter/fod_paths.py`, `exporter/fod_launcher.py`, `exporter/build_stamp.py`, `exporter/progress.py`, **`exporter/blender_provisioner.py`** (§5.2 in full: resolution order, resumable download, SHA-256 gate, per-platform extraction, the step-5 integrity probe, atomic promotion, the cache lock, and the offline failure message), **`exporter/cod_autodetect.py`** (port of `ExporterLauncher.CandidateGameDirectories` at `:192-209`, `:471-484`, `:492-542` — the Steam library-folders VDF parse, the default Windows/Program Files paths, `~/Library/Application Support`, `~/.steam/steam/steamapps/common` — shared by the GUI's Paths screen and, via `--fod-detect-cod`, by `ExporterLauncher` so the two cannot drift); `packaging/tools_manifest.txt` + `tests/test_tools_manifest.py`; `_python()`/`_host()`/`_blender()`/`_run_subprocess`/`_step_signature` changes; `prop_export_fingerprint` stamp + hard error + single-extension assertion; free-space precheck + seed progress; delete `needs_blender`, `importer_addon_status()`, the `run_cli` importer block, the `import build_importer`, the pip button, the Prepare-importer button, the Blender row, `RequirementsFrame`. *Done when:*
- the full suite is green under a plain dev checkout **and** three new tests cover the bundled branch, which the existing suite cannot (`tests/test_exporter_pipeline.py:699` and `tests/test_shipping_map_roster.py:185` call `build_steps(config)` and take the `sys.executable` path unconditionally): `tests/test_fod_paths.py` monkeypatches `sys.frozen`/`sys.executable` and asserts `_host()` emits `[self_exe, '--fod-run-tool', <abs path>, …]`; `tests/test_fod_launcher.py` runs the real dispatch in a subprocess against a fixture tool asserting `sys.argv[0]`, `sys.path[0]` and `__name__ == '__main__'`; `tests/test_child_env.py` asserts `LD_LIBRARY_PATH`/`DYLD_LIBRARY_PATH` **deletion**, the `BLENDER_*` scrub, and `PYTHONDONTWRITEBYTECODE`/`PYTHONHASHSEED`/`PYTHONUTF8`;
- a full export reproduces the pak; `PIPELINE_SCHEMA_VERSION` is 4; a mid-step kill followed by a rerun resumes correctly while a tool edit invalidates only its own marker;
- `--fod-detect-cod` returns the same path the C# probe returns on all §8.6 machines.
- **the provisioner is proven on every platform**: a cold fetch into an empty cache produces a Blender that passes the step-5 probe; a fetch interrupted at ~50% and restarted resumes rather than restarting; a deliberately corrupted archive is rejected by the SHA-256 gate without extracting; a half-extracted `4.5.1.incoming/` left behind by a kill is never promoted and is cleaned on the next run; two exporters started together serialize on the lock; and **with the network disabled the failure screen prints the pinned URL, the expected SHA-256 and the destination directory**, and `--blender <path>` completes the export from a manually placed Blender.

**M7. Progress and the game contract.** `@fod v1` + `FOD_PROGRESS_PROTOCOL`; per-item emission in maps/props/players/viewmodels; the Blender success sentinel; `<staging>/cancel`; the `TimeoutExpired` cancel fix and the watchdog-thread poll; `<staging>/running/<key>.json` + `faulthandler`; `FodExporterProgress.cs`; `ExporterLauncher.cs` probe kinds, direct launch, macOS inner-Mach-O target, `File.SetUnixFileMode`, Windows redirect, diagnostics; delete `FodToolchainProbe.cs` and all 15 `Stage.LocalRequirements` sites per §6.5; fix the literals, the "own window" copy and the `:1839` bar. *Done when:* `grep -c "LocalRequirements\|FodToolchainProbe" Assets/Scripts/Frontend/FodBootFlow.cs` returns 0 and the project compiles; a `--cli` export driven by the game shows a moving bar and a live item count on Windows; Cancel mid-`props` stops within one item; and **running without `--game-callback` produces stdout byte-identical to the pre-change build for at least one full step, verified by diff.**

**M8. Freeze, package, ship. — DONE 2026-08-11.** `packaging/fod_exporter.spec` + `packaging/build_exporter.py` produce the Windows payload; `.github/workflows/exporter.yml` builds it on `windows-2025` and is **green end to end** (run 31455338555): pin check, tools-manifest check, build, self-test, payload hygiene, artifact upload. **1,115 files / ~77 MB uncompressed, 33.3 MB as a CI artifact.** The signing steps are deleted rather than deferred, per the scope decision.

Proven on the Windows build machine, not merely built: a real export ran from the frozen `.exe` to exit 0 — `viewmodels` 95.5 s, `worldmodels` 3.7 s, `shellcasing` 0.9 s, `projectiles` 1.3 s, producing 39 viewmodel GLBs and 37 world GLBs — with **no Python on the machine's PATH for the exporter or for any step it spawned**.

**The frozen build exposed three defects nothing else could have**, which is the argument for having built it before writing the CI around it:

1. `Failed to load Python DLL python312.dll` — PyInstaller ≥ 6 already emits `_internal/`, and the assemble step re-nested it.
2. The Blender pin was unreachable: the payload root was not on `sys.path` before dispatch, so `build_stamp` was invisible and the provisioner fell back to a `packaging/` path that exists only in a checkout. Fixing it also guarantees the loose `.py` files win over any frozen copy — so what runs is what `_step_signature` hashed.
3. `unrecognized arguments: -c import importlib.util …` — `build_importer` probed the extension with `[sys.executable, "-c", …]`, and under PyInstaller `sys.executable` **is the exporter**, so the snippet reached our own argparse. The obvious fix (import it in-process) would have traded a startup bug for a licensing one, since it puts the GPL `.so` inside the proprietary process. The probe moved into Blender instead, which already loads it: one launch now reports the Blender version, all three glTF hooks and the importer's LOD API. §12 Q9's assumption is untouched.

Original text follows.

**M8. Freeze, package, sign, ship.** `packaging/{fod_exporter.spec, build_exporter.py, blender_pin.json, python_pin.toml, requirements-3.13.lock, step_weights.json, tools_manifest.txt, tools_exclude.txt, entitlements.plist, sign_macos.sh, sign_windows.ps1, smoke_test.py}`; `.github/workflows/exporter.yml` + `exporter-regression`; `LICENSES/` generation + the `cargo build --offline` source-drop check; Makefile targets; depot `FileProperties`; `build_all.sh` filter + `fetch_exporter_payload()`; docs (`Docs/EXPORTER_BUNDLE.md`, `Docs/EXPORTER_ACCEPTANCE.md`, the `Docs/STEAM_RELEASE.md` rewrite). *Done when:*
- all three payloads build in CI, `--fod-selftest` passes on each, and **`--fod-selftest --provision` completes a real fetch-verify-extract-probe cycle on each**;
- §8.2 step 1's pin validation passes, and the scheduled weekly pin-check job is live;
- the macOS `.app` notarizes, staples and passes `spctl -a -vvv -t exec`;
- `verify_depots.py --files` shows the payload in all three client depots with all four `Content/*` exclusions intact, run on a host with `Builds/*/Content` populated;
- `packaging/step_weights.json` is regenerated from a full export on the **final signed payload** on at least one acceptance machine per platform, and the committed weights are within 20% of that run (`--fod-selftest --report-timing` makes this one command);
- Steam's "Verify integrity of game files" completes without deleting or re-downloading a populated `Content/current`, on all three platforms;
- the manual checklist at `Docs/EXPORTER_ACCEPTANCE.md` is signed off on every machine in §8.6, with wall-clock, first-run download time and peak disk **on both volumes** recorded per platform, for a clean install → double-click (desktop) or in-game `--cli` (Deck) → point at CoD → mounted fodpak;
- **the offline path is signed off on at least one machine per platform**: network disabled, the failure screen is legible and actionable, and a sideloaded archive plus `--blender` completes the export.

**This is the milestone that meets the success criterion in §1.**

**M9 is deleted.** It existed only to shrink a bundled Blender. We do not convey Blender, so there is no strip list, no modified-GPLv3 §5(a) obligation, no Blender Foundation trademark question, and no `codesign --remove-signature` → delete → re-sign → re-notarize sequence to reconcile with `FriendsOfDutyMacBuild.cs:351`. Old §12 Q4 is retired and its number reused for the mirror-policy question.

### Phase B — remove Blender

> **Phase B now has its own document: [`EXPORTER_PHASE_B.md`](EXPORTER_PHASE_B.md).**
> It supersedes the summaries below with measured scope (429 GLBs, of which 44
> are skinned and carry 91% of the bytes), the harness contract, the go/no-go
> gate at M13, and a 49-day critical path. The outline here is kept because
> the risk table and milestone numbering reference it.


Each step lands independently, gated on the golden harness, with **M4's frozen `--factory-startup` pinned-Blender output as the oracle**. Nothing here blocks shipping.

**M10. Harden the harness.** **The oracle must be a Windows-produced pak** (measured: a macOS-produced one differs from a Windows one in every viewmodel GLB, at float32-epsilon scale — see §1). `tests/test_glb_golden.py` must compare, per GLB: **structure exactly** — accessor count, bufferView count, node and joint order and names (shipped props are one node per surface with Blender's `.001`/`.002` suffixes; `FodAuthoredPropCatalog.cs:89` imports with `NameImportMethod.OriginalUnique`), per-primitive material binding, animation names and sampler counts, the LOD file set (57 props have `lod1.glb`, 45 have `lod2.glb`); **byte-exact** — static `POSITION`/`NORMAL`/`TEXCOORD_0` and all texture PNG bytes; **within tolerance** — animation sampler outputs AND node TRS, with the measured cross-platform floor of **1.8e-07 absolute** (not a relative bound: the affected values are ~1e-08, where relative error is meaningless). Accessor and bufferView COUNTS must be compared structurally but not required equal across platforms, since dedup outcomes follow the float values. Plus `props.json`/`players.json`/`weapons.json` field-by-field. A harness that compares only sorted positions at 1e-3 cannot detect the two regressions most likely to occur.

**M11. Texture decoder.** Write BC1/BC2/BC3 + TGA type-2 + JPEG decode that matches Blender's output **exactly**. Do not use Pillow's DDS path: measured, 0 of 60 prop textures decode pixel-identically, all differing by 1-2 levels per channel — almost certainly the BC1 ⅓/⅔ interpolant rounding and the BC3 alpha ramp, both of which have implementation latitude. Blender's decoder is in `source/blender/imbuf/intern/dds/`; port it. *Done when:* all 811 model PNGs hash-match.

**M12. Static path.** `tools/fod_scene.py`, `tools/fod_rig_math.py`, `tools/fod_mesh_ops.py` (duplicate-face dedupe, first-wins — measured to explain all 5 initial prototype mismatches exactly: 8/8, 48/48, 1/1), `tools/export_static_direct.py`. Cut over steps `shellcasing`, `projectiles`, `worldmodels` and props-static, **including authored LODs and the full `props.json` table**. *Done when:* 386 of 429 model GLBs pass M10 and `exporter/package.py:607`'s `validate_prop_lods` is green.

**M13. Skinned pilot — the go/no-go gate.** Extend `tools/fod_glb_writer.py` with skins, animations, `JOINTS_0`/`WEIGHTS_0`, multi-node scenes and shared bufferViews. Reproduce `props/mg42_bipod` (13 joints, 7 clips, 128 KB, single rig, no cross-rig rebind, no armature join — the only animated prop). *Done when:* it passes M10 including per-clip per-frame skinned vertex positions. **Do not commit a Phase B schedule until this passes.** If it fails, §12 Q3's fallback applies.

**M14. Players — a rewrite, not a port.** Today the cross-rig rebind is a **per-vertex `mathutils.Matrix` product against `bpy` armature `matrix_local` values inside a Python loop** (`tools/export_cod1_multiplayer_players.py:198-210`; `from mathutils import Matrix, Quaternion, Vector` at `:16`; **numpy is imported nowhere in that file**). The earlier draft described it as "one batched numpy 4×4 multiply per vertex" and called M14 a port; both are wrong, and the correction matters because it moves M14 from porting to writing. Phase B must derive rest matrices from `tools/fod_rig_math.py` and batch the rebind as a single numpy 4×4 broadcast — new code, gated on M10. The verified result that direct quaternion accumulation is *more* accurate than Blender's head/tail/roll reconstruction (whose rest matrices are not even orthonormal, |det−1| up to 3.3e-5) is the reason it is worth doing, not evidence it is already done. Emit 351 clips per profile directly as samplers at `frame/30`. `exporter/package.py:1151` rejects an unnamed animation and `:1154` rejects duplicate names per GLB; `:3518` requires unique names per weapon. *Done when:* all 4 profiles pass M10.

**M15. Viewmodels.** Armature join becomes list concatenation plus a parent-index fixup; port the `tag_view` → `tag_torso` → `tag_weapon` attachment chain and the ADS overlay. *Done when:* all 39 files / 354 clips pass M10.

**M16. Delete Blender.** Remove `exporter/blender_provisioner.py`, the pin, `_blender()`, the six Blender steps' scripts, and `--factory-startup`. This is where the network dependency, the ~390 MB first-run download, the ~1 GB per-user cache and risks 4, 5, 8a and 8c all disappear at once. Restore macOS universal (two 60 MB trees) and the `macos-x86_64` importer target. *Done when:* the payload is ~60 MB/arch, a full export produces a pak that passes M10 end to end, and the game loads it.

---

## 12. Open questions for the project owner

All eleven must be answered before M1 starts (§11 M0), because M2 and M8 are blocked by questions an earlier draft deferred past them. **Q4 was retired with the M9 strip milestone and its number reused for the mirror-policy question**, which the runtime-fetch decision made load-bearing.

**Q1 — macOS architecture in Phase A.** *Blocks M8, M16.* Recommendation: **arm64-only**, with a universal2 binary whose x86_64 slice is a ~20 KB stub showing a dialog pointing Intel players at the remote-import path. This removes the deprecating `macos-15-intel` runner class from the matrix, halves the notarization payload, and halves 10×-billed macOS minutes. Under the runtime-fetch decision universal is far cheaper than it was — two frozen shells (~+62 MB), not two Blender trees (+802 MB) — so the case for arm64-only rests now on the deprecating `macos-15-intel` runner class and 10x-billed macOS minutes rather than on payload size. Note also that Blender dropped official Intel macOS builds at 5.0, so any future pin bump past 4.5 strands Intel regardless. Accept arm64-only, or is universal2 now worth the CI cost?

**Q2 — Apple Developer Program.** *Blocks M8.* Steam has required notarized macOS applications since 2019-10-14 and the game is currently only ad-hoc signed (`FriendsOfDutyMacBuild.cs:277-283`). This needs enrolment ($99/yr), a Developer ID Application certificate exported as `.p12`, an App Store Connect API key, and the **Team ID**, all as CI secrets under the names in §8.3. Is enrolment in place, or will it be? What is the Team ID?

**Q3 — The network dependency, CI budget, and the worst case.** *Blocks M2, M8.* The runtime-fetch decision cut the depot payload to **~66-68 MB per platform (~20 MB first download)** and the per-release upload to ~200 MB across three depots, from ~1.00 GB and ~2.82 GB. What it bought that with is a **~350-390 MB one-time download per player, over a link we do not control, before their first export can start** — risks 4, 5, 8a and 8c, three of which are Open or Unknown. Three parts:

(a) Which GitHub plan is this on, and how often should CI run? `macos-15` bills at a 10x minute multiplier and `--fod-selftest --provision` adds a ~390 MB download per platform per run; the proposal is push-to-`main` and tags only, not every PR commit.

(b) **Is the first-export network requirement acceptable as a product decision?** It is the one thing a player cannot work around by waiting — an offline or heavily-filtered machine simply cannot produce a pak without sideloading an archive by hand. Say explicitly whether that is an acceptable support burden.

(c) **If M13's skinned pilot fails, Phase B does not land and the download is permanent.** The escape hatch is not a smaller download; it is §3.3's optional free-DLC depot, which restores Steam's mirroring, resume and delta patching and eliminates risks 4, 5, 8a and 8c in one move, at the cost of a free DLC AppID and ~40 lines of Steamworks.NET. **Pre-authorise that fallback now**, so it can be taken during M8 acceptance on evidence rather than re-litigated under release pressure.

**Q4 — May we ever mirror the Blender archive ourselves?** *Blocks nothing today; blocks the response to risk 5 when it fires.* This replaces the retired strip question. If `download.blender.org` prunes or moves the 4.5.1 artifact, the fastest fix is to host the archive on our own storage and repoint the pin — and **that would make us a conveyor of GPLv3 Blender**, reviving every obligation §9.2 just discharged: the source-availability duty, the §6(d) "as long as needed" commitment, and the licence-notice requirements. The alternative is a pin bump to a still-published build, which is correct but costs a full M8 smoke pass and an M10 harness re-run and cannot be done in an afternoon. Decide the policy now, while it is cheap: **is self-hosting permitted as an emergency measure (with the compliance work accepted), or is a pin bump the only sanctioned response?** Getting this wrong at 2am during an outage is how projects acquire accidental GPL obligations.

**Q5 — May the exporter be GPL?** *Confirmation only; the architecture already assumes "keep the option".* This design deliberately keeps the frozen binary out of the GPL by never linking Blender code into it. If you do not care — and there is a reasonable argument that the exporter has no proprietary value, since it already ships as readable source in every depot — then `bpy`-in-process becomes reconsiderable and the payload drops ~200-400 MB per platform. It would cost the freezer-hook risk, per-step process isolation, and the `runpy` mechanism that handles the 28 tools with no `main()`. Confirm: keep the option open, or explicitly declare the exporter GPL-3.0-or-later?

**Q6 — Windows code signing service.** *Blocks M8.* Not "do we have a certificate" — since 2023-06-01 the CA/Browser Forum requires OV **and** EV code-signing private keys to live on FIPS 140-2 L2 hardware, so a `.p12` in a GitHub secret is impossible for any certificate obtained today. Which cloud signing service will we use — Azure Trusted Signing (~$9.99/mo), DigiCert KeyLocker, or SSL.com eSigner? A local hardware token is incompatible with GitHub-hosted runners and would move the Windows sign step onto the self-hosted box in §8.6. Without any of them, Defender/SmartScreen quarantining the exporter after install is a support scenario with no user-side remedy.

**Q7 — Windows build host and `gh` auth.** *Blocks M8, §8.4.* Is `gh auth login` (or `GH_TOKEN`) on the staging Mac acceptable, so `fetch_exporter_payload()` can pull the Windows exporter payload from CI after the `Builds/Windows` swap? If not, everything continues through the scp'd `Builds/Windows.zip` and that wire carries ~1.05 GB per release instead of 63,005,163 B.

**Q8 — Reference-build provenance.** *Blocks M5.* **Partly answered 2026-08-10: Blender 4.5.1 is confirmed** — re-exporting `viewmodels` on it reproduced all 39 GLBs byte-identically. The CPython/numpy/Pillow question stands for the host steps (`maps` especially). Original question: what exact **Blender**, CPython, numpy and Pillow versions produced the current `Content/Content/current`? Nothing records any of them: `provenance/` holds only `source_archives.json`, and `fodpak.json`'s keys are `categories, contentTier, createdUtc, exporterVersion, format, gameContentVersion, hasUnitedOffensive, notes, orientation, sourcePolicyFormat, sourcePolicyVersion, sourceSummary, version`. The best available evidence is this machine's `python3` 3.13.5, numpy 2.4.3, Pillow 12.1.1 and Blender 4.5.1 (build `b0a72b245dcf`, 2025-07-29) — but `v4.5.47` in the generator string pins the **add-on**, not the Blender patch. M5 settles it empirically; confirming up front saves a cycle.

**Q9 — Does the game's `fodpak.json` reader tolerate unknown top-level keys?** *Blocks M8. Confirmation, not decision — 10 minutes of reading.* It is loaded via `JsonUtility`, and `JsonUtility.FromJson` ignores unknown keys by design; that is this plan's stated position. **Confirm** by reading the `FodPakManifest` class. If confirmed, toolchain provenance (Blender version + source SHA-256, importer version, exporter build, interpreter/numpy/Pillow versions) goes into `fodpak.json`. If the reader turns out to be stricter, it goes into the existing `provenance/` directory instead — breaking the manifest for provenance would be a self-inflicted wound.

**Q10 — Fonts and music.** *Blocks the game release, not this plan.* Is `ConduitITCStd.otf` covered by a purchased app-embedding licence, and is there any paper trail for `At Ease Soldier - Unknown.mp3`? A track with no identifiable rights holder cannot be cleared. Both ship inside the game build today.

**Q11 — Is Linux arm64 a ship target?** *Blocks M2 (a fourth matrix row).* `Docs/STEAM_RELEASE.md:87-89` implies an arm64 importer ships, and `cod_asset_importer_linux_arm64_v3.5.0.zip` really does contain one — but Blender publishes no official Linux ARM build and `bpy` has never published a Linux aarch64 wheel, so the six Blender-dependent steps cannot run there under any Phase A strategy. Drop the claim, or scope the target to Phase B only?