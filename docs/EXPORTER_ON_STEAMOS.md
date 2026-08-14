# Running the CoD data importer on SteamOS

**Status:** proposal, with the build half already demonstrated.

The exporter ships Windows-only today. That was an owner decision
(`EXPORTER_SINGLE_EXECUTABLE.md` §1, 2026-08-10) and it rests on a sound
argument: a `.fodpak` carries no platform-specific data, so a package built on
any Windows machine mounts unchanged on macOS, Linux and Steam Deck. A Deck
player is not blocked — they use a package someone built elsewhere.

But "not blocked" is not "zero friction". A Deck player who owns Call of Duty
on that same device currently cannot turn it into a package without finding a
Windows PC. This document is what closing that costs.

---

## 1. What already exists

This is the surprising part: the Linux target was designed in and then switched
off, not left unbuilt. Verified 2026-08-13:

| Piece | State |
|---|---|
| Blender pin | `blender_pin.json` carries `linux-x64` → `blender-4.5.1-linux-x64.tar.xz`, sha256 `085a7ed4ed80…` |
| Importer | CI publishes `linux-x64` → `cod_asset_importer.abi3.so`, **ELF 64-bit x86-64, 3,486,168 bytes**, fetched and confirmed |
| Build target | `build_exporter.py` maps `linux-x64`; `blender_target()` resolves it |
| `.tar.xz` support | `lzma` is already in `ALWAYS_HIDDEN` *specifically* for the Linux Blender archive |
| Install detection | `cod_autodetect` already knows `~/.steam`, `~/.local/share/Steam` and the Flatpak Steam path |
| Launcher | `exporter/run_exporter.sh` exists |
| Windows-only code | `winreg` is imported inside a function behind `try/except ImportError`, so it degrades on Linux rather than failing |

**A Linux payload has now been built and it passes its own acceptance gate in
full.** Built in an `x86_64` Debian 12 container on an Apple Silicon MacBook,
reproducible with `packaging/build_linux_in_docker.sh`:

```
OK   frozen imports — numpy 2.5.2, Pillow 12.3.0
OK   GUI toolkit — Tk 8.6.13
OK   importer probe — cod_asset_importer.abi3.so ready with
     authored XModel LOD API 1 (3,486,168 bytes, Linux x86_64)
OK   Pillow codecs — DDS, JPEG, PNG, TGA
OK   CA bundle — certifi 240216 bytes
OK   Blender pin — 4.5.1 linux-x64
All checks passed.
payload: 559 files, 176,532,928 bytes

FriendsOfDutyExporter: ELF 64-bit LSB executable, x86-64,
                       dynamically linked, for GNU/Linux 3.2.0
```

The Tk line matters most: the payload opened a real window, so Tcl/Tk is being
collected correctly. The importer line matters nearly as much: the Linux native
extension loads and reports the authored-LOD API the prop export requires.

So the build is not the hard part. The hard part is everything after it.

---

## 2. Where to build it

**Not on the Mac directly.** PyInstaller freezes the interpreter that runs it,
so a Linux payload needs Linux CPython, manylinux numpy/Pillow wheels, Linux
Tcl/Tk shared objects and the Linux bootloader. `build_exporter.py` now refuses
a mismatched `--target` outright, because the failure was silent: it used to
produce a Mach-O binary stamped `linux-x64`, and that exact class of mistake has
shipped before — a Mach-O `.abi3.so` once went out inside the SteamOS depot.

Two options that do work:

**A container on the Mac.** `--platform linux/amd64` on `python:3.12-bookworm`,
which is what produced the result above. Convenient, no second machine, and it
is qemu-emulated so it is slow. Its real limitation is not speed: **you never
run what you built.** A container gives you a binary and no evidence.

**A Linux box over ssh.** The same shape as `SteamPipe/build_all.sh`'s
`--remote-windows` leg. Slower to set up, better in every other way, because
the machine that builds it can also run it before it reaches a depot.

Either way, one constraint is not negotiable:

> **Build on an old glibc.** A PyInstaller binary links the build machine's
> glibc and will not start on anything older. Debian 12 is glibc 2.36; SteamOS
> 3.x is Arch-based and newer. Building on Debian and running on SteamOS is the
> safe direction. Building on Arch and shipping to an older distro is not.

---

## 3. What is actually unknown

Everything below needs a Steam Deck. No container answers any of it.

### 3.1 Does a window appear in Gaming Mode?

The largest risk by far. Gaming Mode runs gamescope, which composites a single
game surface. The exporter is a separate process opening its own Tk window, and
whether that surfaces, appears behind the game, or never appears at all is
untested. Desktop Mode is expected to be fine.

If it does not work, the options in increasing order of effort are: document
"switch to Desktop Mode" (poor, but honest), have the game launch it through
gamescope explicitly, or drive the export from inside the game's own UI and
leave this tool headless on Deck. **The last one may be the right answer
anyway** — the pipeline already runs perfectly well under `--cli`, and the game
already has a progress protocol for it.

### 3.2 Is the Call of Duty install found?

CoD1 on a Deck runs under Proton, so its files sit in
`steamapps/common/Call of Duty` with a Windows game around them. Detection
should cope: it parses `libraryfolders.vdf` and knows the Linux Steam roots.

One bug in the way of this was found and fixed while writing this document:
`looks_like_install` checked `path / "Main"` with exact case, while
`cod1_archive_policy._tier_directory` resolves the same directory
case-insensitively *and documents why* ("every Linux box, so every Steam Deck").
An install spelled `main/` was therefore accepted by the policy but never
detected — sending the player to a folder picker, which in Gaming Mode is barely
usable with a controller. Now resolved case-insensitively in both places.

### 3.3 Does the provisioned Blender run?

The pin fetches an official `linux-x64` build. It needs its own shared library
dependencies present on SteamOS, and SteamOS has an immutable root, so anything
missing cannot simply be `pacman -S`'d. Blender's official builds are largely
self-contained, so this is expected to work — expected, not demonstrated.

### 3.4 The Steam Linux Runtime

If the game is launched inside the Steam Linux Runtime (pressure-vessel), a
child process it spawns inherits that container. The exporter would then run
against the runtime's libraries rather than the host's. This must be tested in
the configuration the game actually ships in, not just from a Konsole prompt.

### 3.5 Output equivalence

The whole content pipeline assumes reproducibility. §1 of
`EXPORTER_SINGLE_EXECUTABLE.md` records that the oracle is **platform-specific**:
Windows and macOS produce structurally identical but not byte-identical GLBs,
with float drift at 1.8e-07. A Linux build will differ again. That is acceptable
— it is encoder-level noise, and the game does not care — but the golden harness
must not be a `sha256` compare against a Windows-built reference, or Linux will
"fail" a test it actually passes.

---

## 4. Steam plumbing

Depot **4480885** is Windows-only, 64-bit, and carries the Windows payload. A
Linux importer needs its **own depot**, exactly as the game has one per OS —
the payloads are different binaries, so one depot cannot serve both.

1. Create a depot, e.g. `4480886` "Friends of Duty - CoD Data Importer (Linux)",
   OS `Linux + SteamOS`, 64-bit, all languages.
2. Add it to packages 1563735, 1563734, 1563733.
3. Add `depot_build_4480886.vdf` with `ContentRoot "ExporterLinux"` mapped to
   `Exporter`, and mark `Exporter/run_exporter.sh` and the binary as
   `executable` under `FileProperties` — Linux depots lose the executable bit
   otherwise, which is why depot 4480884 already does this for the server.
4. Add it to `app_build_4480880_all*.vdf`.
5. Stage the payload into `Builds/ExporterLinux`.

**Until that exists, the launcher must not offer the importer on Linux or
macOS.** Those depots contain no `Exporter/` directory at all, so a
RUN COD DATA IMPORTER entry there points at nothing. Hide it, or replace it with
a short screen explaining that a package built on any Windows machine works
here — which is true, and is the current supported route.

---

## 5. Suggested order

1. **Hide the launcher entry on non-Windows.** Small, and it removes a dead
   button that exists today.
2. **Stand up a Linux build host** and produce a payload that passes its own
   selftest. **Already done** via `packaging/build_linux_in_docker.sh` — all
   checks pass. A real Linux host is still worth having, because a container
   cannot run what it builds.
3. **Run it on a Deck in Desktop Mode**, against a Proton-installed Call of
   Duty, end to end. This answers §3.2, §3.3 and §3.5 at once.
4. **Then try Gaming Mode.** This is the go/no-go for "zero friction". If the
   window will not surface, fall back to driving `--cli` from the game's own UI
   rather than shipping a tool a Deck player cannot see.
5. **Only then** create the depot and wire the build. Steam depots are
   permanent; creating one before there is a payload worth shipping is
   backwards.

## 6. Honest assessment

The build is free: the machinery already existed, and a container run produces
a payload that passes every check including opening a Tk window and loading the
Linux importer. The remaining risk is entirely in §3.1 and §3.4, and neither can
be retired without a device.

If the answer to §3.1 turns out to be "gamescope will not show it", the
Windows-only decision remains correct for the GUI, and the Deck story becomes
"the game runs the exporter headless and shows its own progress" — which is a
better experience than a separate window anyway, and reuses a progress protocol
that already exists.
