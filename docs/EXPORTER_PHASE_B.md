# Phase B — Removing Blender from the Exporter

**Status:** plan of record for M10–M16. Phase A shipped; nothing here blocks it.
**Prerequisite:** Phase A complete (M1–M8) — see [`EXPORTER_SINGLE_EXECUTABLE.md`](EXPORTER_SINGLE_EXECUTABLE.md).

> Every number in this document was measured on 2026-08-11 against the committed
> package at `Content/Content/current` and a full export on the Windows build
> machine. Nothing here is estimated unless it says so.

---

## 1. What this is for

The shipped exporter downloads a pinned Blender 4.5.1 at first use — 381 MiB,
~936 MB extracted into a per-user cache — and drives it as a child process for
six of the nineteen export steps. That works, and it ships. It also means:

- the first export needs a working internet connection and a transfer we own;
- a pinned artifact being pruned upstream would break **every existing
  installation**, not just new ones;
- ~936 MB of per-user disk;
- an unsigned binary that downloads an archive, extracts ~200 executables and
  spawns one of them, which is a textbook dropper shape to an AV heuristic.

Phase B removes the dependency. When it lands, the exporter is a ~77 MB payload
that reads pk3s and writes GLBs, with no download, no cache, and no external
program. Risks 4, 5, 8a and 8c in the Phase A document disappear rather than
being mitigated.

**The size argument is secondary and should not be the motivation.** The payload
barely moves (~77 MB → ~60 MB). What Phase B buys is the removal of the only
step in the export that can fail for reasons outside the player's machine.

---

## 2. The actual scope, measured

Blender produces **429 GLBs / 125,262,352 bytes**. Everything else in the
package is already Blender-free.

| area | GLBs | kind | clips | bytes | step wall clock |
|---|---:|---|---:|---:|---:|
| `props` | 340 | static | 0 | 9,613,356 | 37.3 s |
| `props` (`mg42_bipod`) | 1 | **skinned** | 7 | 128,160 | ″ |
| `worldmodels` | 37 | static | 0 | 1,540,196 | 3.5 s |
| `projectiles` | 7 | static | 0 | 168,020 | 1.2 s |
| `shell` | 1 | static | 0 | 2,776 | 0.9 s |
| `viewmodels` | 39 | **skinned** | 354 | 29,477,440 | 89.2 s |
| `players` | 4 | **skinned** | 1,404 | 84,332,404 | 419.1 s |
| **total** | **429** | 44 skinned | 1,765 | **125,262,352** | **551.2 s** |

Prop LOD files: 239 `prop.glb`, 57 `lod1.glb`, 45 `lod2.glb`.

### 2.1 Two facts that should shape the whole plan

**The hardest part is 5 files.** 385 of the 429 are static meshes with no
animation at all. The skinned half is 4 player profiles, 39 viewmodels and one
animated prop — but it is **91% of the bytes** (113.9 MB of 125.3 MB) and **93%
of the wall clock** (508 s of 551 s).

**A Blender-free GLB writer already exists and already runs at scale.**
`tools/fod_glb_writer.py` produces the entire map half of the package —
**1,663 GLBs / 82,391,996 bytes** — with no Blender anywhere. The `maps` step is
the single most expensive step in the export (507.8 s, 41% of the run) and it
has never touched Blender.

That is the strongest evidence Phase B is achievable: the project has already
done the hard version of this problem once. What `fod_glb_writer` does not yet
do is skins, animations, and multi-node scenes — which is exactly the 44-file
list above.

**`maps` is out of scope.** It is Blender-free today, it is 41% of the run, and
Phase B must not touch it. Nothing in M10–M16 changes the map path.

---

## 3. What "done" means

Every milestone is gated on the golden harness (M10), never on inspection.
The end state is:

1. A full export produces a package that passes M10 against the frozen oracle.
2. `exporter/blender_provisioner.py`, the pin, `_blender()` and the six Blender
   tool scripts are deleted.
3. The payload has no network dependency and no per-user cache.
4. `Docs/EXPORTER_SINGLE_EXECUTABLE.md` risks 4, 5, 7, 8, 8a, 8b, 8c are struck.

---

## 4. M10 — the harness, and why it comes first

**Nothing in Phase B may be written before the harness exists.** A rewrite
validated by eye is a rewrite that ships wrong content.

### 4.1 The oracle is Windows-built, and this is not negotiable

`Content/Content/current` was regenerated on the Windows build machine on
2026-08-11 precisely so Phase B has a reference on the shipping platform.

The reason is measured. The same `viewmodels` export on macOS and on Windows
produces **all 39 GLBs different**, with identical node names, node order,
animation names and per-animation sampler counts, and an identical
`asset.generator`. What differs is node TRS on 25–28 of 73 nodes, worst case:

| field | worst absolute delta |
|---|---:|
| rotation | 1.788e-07 |
| translation | 2.328e-10 |
| scale | 3.576e-07 |

float32 epsilon is 1.19e-07. These are last-bit differences on leaf bones whose
values are themselves ~1e-08.

**Do not use a relative tolerance.** The affected values are near zero, where
relative error is meaningless — one pair was `-2.14e-08` vs `+1.49e-08`, a
"relative delta" of 2.0 for a difference of 3.6e-08. The floor is
**1.8e-07 absolute**.

### 4.2 Accessor counts are a consequence, not an independent signal

The Windows build also had **+6 accessors and +6 bufferViews**.
`tools/fod_export_common.py:324-341` monkeypatches
`GlTF2Exporter.__append_unique_and_get_index` — the exporter's accessor **dedup**
path. Values that were bit-identical on one platform and collapsed into one
accessor are no longer identical on another, so fewer dedup hits occur.

The harness must therefore compare accessor and bufferView counts
**structurally but not require equality across platforms**, while requiring
equality for a same-platform rerun.

### 4.3 What `tests/test_glb_golden.py` must assert

**Exactly, per GLB:**

- accessor / bufferView / mesh / node / material / animation counts
- node names **and order** — shipped props are one node per surface carrying
  Blender's `.001`/`.002` collision suffixes, and
  `Assets/Scripts/Content/FodAuthoredPropCatalog.cs:89` imports with
  `NameImportMethod.OriginalUnique`, so a renamed node is a broken prop
- joint names and order; per-primitive material binding
- animation names and per-animation sampler/channel counts
- the LOD file set: 239 `prop.glb`, 57 `lod1.glb`, 45 `lod2.glb`

**Byte-exact:**

- every texture PNG
- static `POSITION` / `NORMAL` / `TEXCOORD_0`

**Within tolerance (1.8e-07 absolute):**

- animation sampler outputs
- node TRS

**Field-by-field:** `props.json`, `players.json`, `weapons.json`,
`maps/catalog.json`.

**Also gate on the package validator.** `exporter/package.py`'s
`validate_prop_lods` enforces LOD0-glb identity, non-empty `sourceSurface` and
strictly increasing distances; `package.py:1151` rejects an unnamed animation
and `:1154` duplicate names per GLB. A harness pass with a validator failure is
not a pass.

**A caution learned the hard way this session:** a *missing* file can look
exactly like an inert one. Five map ambient beds were absent from an export and
looked like stale surplus, because the same bug that stopped producing them also
stopped referencing them. The harness must compare the **file set** and assert
**zero dangling references**, not only compare files present in both.

*Done when:* the harness reproduces a green result against the committed oracle
for all 429 GLBs, and rejects each of four synthetic mutations — a renamed node,
a reordered joint, a 1e-05 sampler shift, and a deleted `lod2.glb`.

**Estimate:** 5 days.

---

## 5. M11 — the texture decoder

Blender currently decodes every model texture:
`tools/fod_export_common.py:90-102` (`convert_image_to_png`) routes them through
`bpy.data.images.load()` → `image.save()`.

**Pillow cannot substitute.** Measured: decoding 60 prop source DDS files with
Pillow and comparing against the shipped Blender-produced PNGs gives **0 of 60
pixel-identical**, all differing by 1–2 levels per channel (e.g.
`wood@panzerfaust_box`, max delta 2, mean 0.227). The cause is almost certainly
BC1's ⅓/⅔ interpolant rounding and the BC3 alpha ramp, both of which have
implementation latitude.

Port Blender's decoder from `source/blender/imbuf/intern/dds/`. Formats needed:
BC1/BC2/BC3, TGA type 2, JPEG.

**Note for the PNG encoder.** Cross-platform PNG output already differs in the
**deflate tail only** — measured on `smk_p_none_wht_c.png`: inflated pixel
streams byte-identical, last 7 bytes of the compressed stream different, chunk
layout identical. Phase B controls the encoder, so it can and should be made
deterministic (fixed zlib level and strategy), which removes a whole class of
diff noise the Blender path could not avoid.

*Done when:* all 811 model texture PNGs hash-match the oracle.

**Estimate:** 8 days. **Risk:** medium-high — this is a bit-exactness problem
against a C implementation, and it gates everything after it.

---

## 6. M12 — the static path

Cut over `shellcasing`, `projectiles`, `worldmodels` and props-static: **385 of
429 GLBs**, 11.3 MB, 43 s of wall clock.

New modules: `tools/fod_scene.py`, `tools/fod_rig_math.py`,
`tools/fod_mesh_ops.py`, `tools/export_static_direct.py`.

`fod_mesh_ops` needs duplicate-face dedupe, first-wins — measured to explain all
five initial prototype mismatches exactly (8/8, 48/48, 1/1).

This is the milestone where a prior prototype already showed
`props=239 bit-exact=226 differ=0 skinned-skipped=13` against Blender's 16.7 s,
in 0.6 s. Treat that as encouraging, **not** as done: it measured LOD0 geometry
only, and ignored the authored LODs, per-prop textures and the `props.json`
material/cutout table with its `exportFingerprint`.

*Done when:* 385 GLBs pass M10 **and** `validate_prop_lods` is green **and** the
authored LOD file set matches (239/57/45).

**Estimate:** 10 days.

---

## 7. M13 — the skinned pilot (**go/no-go**)

Extend `tools/fod_glb_writer.py` with skins, animations, `JOINTS_0`/`WEIGHTS_0`,
multi-node scenes and shared bufferViews.

**Target: `props/mg42_bipod`.** It is the only animated prop — 13 joints,
7 clips, 128,160 bytes, a single rig, no cross-rig rebind, no armature join. It
is the smallest possible complete test of the skinned path.

*Done when:* it passes M10 **including per-clip, per-frame skinned vertex
positions**.

> **Do not commit a Phase B schedule until this passes.** If it fails, the
> Blender download is permanent, and the fallback in §12 Q3 of the Phase A
> document applies: reconsider shipping the exporter as a free optional DLC
> depot, which removes the download's failure modes without removing Blender.

**Estimate:** 10 days. **Risk:** this is the milestone that decides the project.

---

## 8. M14 — players (a rewrite, not a port)

4 profiles, **1,404 clips**, 84.3 MB, 419.1 s — 34% of the entire export.

**Correct a claim that has appeared in planning before.** The cross-rig rebind is
**not** batched numpy today. It is a per-vertex `mathutils.Matrix` product inside
a Python loop against `bpy` armature `matrix_local` values
(`tools/export_cod1_multiplayer_players.py:198-210`; `from mathutils import
Matrix, Quaternion, Vector` at `:16`). **numpy is imported nowhere in that
file.** M14 is therefore writing new code, not porting existing code.

Derive rest matrices from `fod_rig_math`, and batch the rebind as a single numpy
4×4 broadcast. The verified result that direct quaternion accumulation is *more*
accurate than Blender's head/tail/roll reconstruction — whose rest matrices are
not even orthonormal, |det−1| up to 3.3e-05 — is the reason this is worth doing,
not evidence it is already done.

Emit 351 clips per profile directly as samplers at `frame/30`.

*Done when:* all 4 profiles pass M10.

**Estimate:** 12 days.

---

## 9. M15 — viewmodels

39 files, **354 clips**, 29.5 MB, 89.2 s.

Armature join becomes list concatenation plus a parent-index fixup. Port the
`tag_view` → `tag_torso` → `tag_weapon` attachment chain and the ADS overlay.
`tools/fod_export_common.py:tag_bone_transforms` and `add_tag_empties` define the
contract: `tag_*` bones are re-emitted as parentless empties in baked space, with
the 0.0254 inch scale dropped, and a name collision must fail loudly.

*Done when:* all 39 files / 354 clips pass M10.

**Estimate:** 10 days. Can run in parallel with M14.

---

## 10. M16 — delete Blender

Remove `exporter/blender_provisioner.py`, `packaging/blender_pin.json`,
`packaging/make_blender_pin.py`, `pipeline._blender()`, `--factory-startup`, the
six Blender tool scripts, and the `blender` entries in
`toolchain.BLENDER_STEPS`. Drop the `prepare` phase from the `@fod` protocol and
the provisioning UI from both front ends.

Then: strike risks 4, 5, 7, 8, 8a, 8b, 8c; delete §12 Q4 (the mirror question);
and remove the weekly pin-check job from CI.

*Done when:* a full export produces a package that passes M10 end to end, the
game mounts it, and `grep -rn "blender" exporter/ tools/` returns only history.

**Estimate:** 4 days.

---

## 11. Schedule

| M | what | days | gate |
|---|---|---:|---|
| M10 | harness | 5 | rejects 4 synthetic mutations |
| M11 | texture decoder | 8 | 811 PNGs hash-match |
| M12 | static path | 10 | 385 GLBs + `validate_prop_lods` |
| **M13** | **skinned pilot** | **10** | **go/no-go** |
| M14 | players | 12 | 4 profiles |
| M15 | viewmodels | 10 | 39 files (parallel with M14) |
| M16 | delete Blender | 4 | full export + game mounts |

**Critical path:** M10 → M11 → M12 → M13 → M14 → M16 = **49 days**, with M15
absorbed alongside M14. M13 falls at day 33; nothing after it should be
committed to before then.

---

## 12. Risks

| # | Sev | Risk | Mitigation |
|---|---|---|---|
| 1 | **High** | M13 fails and the skinned path is not reproducible. 91% of the bytes are skinned, so this is most of the value. | Pilot the smallest complete case first (`mg42_bipod`, 13 joints, 7 clips). Fallback is the optional-DLC-depot option, which fixes the download's failure modes without removing Blender. |
| 2 | **High** | M11 cannot reach bit-exactness against Blender's C decoder. Everything downstream is gated on it. | Port from `imbuf/intern/dds/` rather than reimplementing. If a format resists, keep Blender for textures only while moving geometry — the steps are separable. |
| 3 | Med | The harness passes on structure while content is subtly wrong. | Byte-exact where bytes are stable; tolerance only where drift is measured. Reject 4 synthetic mutations before trusting a green run. |
| 4 | Med | A file stops being produced and looks inert because nothing references it. **This happened.** | Compare the file SET and assert zero dangling references, not only files present in both. |
| 5 | Med | `players` is 34% of the run and is new code, not a port. | Budget it as a rewrite. Gate per profile rather than all four at once. |
| 6 | Low | Prop node naming drifts and breaks `FodAuthoredPropCatalog`. | Node names and order are an exact assertion in M10. |
| 7 | Low | Phase B lands and the payload is barely smaller, disappointing whoever expected a size win. | State the goal as removing the network dependency, not shrinking the payload. ~77 MB → ~60 MB. |

---

## 13. What Phase B does not change

- **`maps`** — 1,663 GLBs, 507.8 s, already Blender-free. Untouched.
- **The fodpak contract** — `Docs/CONTENT_PIPELINE.md` is unchanged throughout.
- **The resume model** — per-step signatures keep working; the `blenderVersion`
  key simply stops appearing once `BLENDER_STEPS` is empty.
- **The Windows-only scope** — unrelated to this work, and unaffected by it.
- **The importer** — `cod_asset_importer` v3.6 is the *foundation* of Phase B,
  not a casualty. It stays, and it is the thing that makes the rewrite possible.

---

## 14. Open questions

1. **Does the runtime want authored distance banks?** The exporter now ships
   every authored bank (near and far), but nothing consumes the far one. That is
   a runtime feature, independent of Phase B, and the data is already in the
   package.
2. **Should the PNG encoder be pinned for determinism?** Phase B controls the
   encoder for the first time. Fixing zlib level and strategy would make texture
   output byte-reproducible across platforms, which the Blender path could never
   offer. Cheap, and it would simplify M10's tolerance story.
3. **Does anything still need Blender after M16?** `tools/blender_asset_gen/` and
   the `audit_*`/`render_*` dev tools do, but none ship and none run in an
   export. Confirm before deleting the provisioner, so a developer workflow is
   not silently broken.
