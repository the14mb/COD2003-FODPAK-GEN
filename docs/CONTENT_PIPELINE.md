# Friends of Duty — Runtime Content Pipeline (fodpak) Specification

**Status:** v1 — implementation contract.
**Goal:** the shipped game contains ZERO Call of Duty–derived assets. All CoD content is
generated on the player's machine by the bundled **Exporter** app (Python + Blender) from the
player's own legally owned Call of Duty 1 + United Offensive install, into a runtime-loaded
**content package**. Without a package, the game boots into the **content deployment scene**
(see §5) offering local extraction or a remote import of a package the player generated
earlier. This is the Ship of Harkinian / Spaghetti Kart model.

Packages generated from the original game are for personal use with a lawfully owned copy and
must not be publicly redistributed. The download feature exists so a player can re-use a
package **they generated** (e.g. hosted on their own private storage) across installs.

---

## 1. Content package ("fodpak")

A package is a **directory** (the "mounted" form). For transport/download it may be zipped
into a single `*.fodpak` file (plain zip, package directory contents at zip root). The game
mounts a directory; downloading/unzipping produces that directory.

### 1.1 Locations (search order at boot)

1. CLI `-contentDir <absolute path>` (also used by dedicated servers) or env `FOD_CONTENT_DIR`
2. `<game directory>/Content/current/` — beside the executable, or beside the `.app` bundle on
   macOS. This is the **portable** location and the default install target.
3. `<persistentDataPath>/Content/current/`
4. Legacy `DefaultCompany/FriendsOfDutyUnity` persistent paths (pre-identity-fix builds)
5. `<workspace>/fod_content/current/` found by walking up from `dataPath` (dev checkouts)

`persistentDataPath` = Unity's per-user data dir for the app.

**Install target.** `FodContentPaths.PreferredInstallDir` resolves once per process: the
portable `Content/` beside the game when a write probe there succeeds, otherwise
`persistentDataPath/Content`. The exporter's `--output` argument and the package installer
both derive from it, so a machine never ends up with two competing packages. The editor
always uses the per-user directory so a multi-gigabyte package never lands next to `Assets/`.
SteamPipe excludes `Content/*`, so a portable package can never enter a depot.

### 1.2 Top-level layout

```
current/
  fodpak.json                      # package manifest (see 1.3)
  weapons/
    weapons.json                   # weapon roster + gameplay definitions + material tables
    viewmodels/<id>/viewmodel.glb  # skinned hands+weapon rig, all legacy anims (glTFast)
    viewmodels/<id>/textures/*.png
    worldmodels/<world>/world.glb  # static, custom loader; <world> LOWERCASED (weapon_fg42)
                                   # + tag_* attachment nodes (see 1.2.2)
    worldmodels/<world>/textures/*.png
    projectiles/<model>/projectile.glb  # static; every definition projectile_model
                                   # (grenades, panzerfaust rocket), <model> LOWERCASED
                                   # (projectile_usgrenade, weapon_panzerfaust_rocket)
    projectiles/<model>/textures/*.png
    shell/shellcasing.glb          # static
    shell/textures/*.png
    data/<id>.txt                  # raw CoD WEAPONFILE dumps (existing format)
  players/
    players.json
    <id>/player.glb                # skinned, all pb_*/pt_* legacy anims (glTFast)
    <id>/textures/*.png
  maps/
    catalog.json                   # replaces OriginalMultiplayerMapCatalog.json (no Assets/ paths)
                                   # authoritative product roster: five base maps, plus Arnhem
                                   # and Cassino when UO is enabled. Development subset runs can
                                   # merge only within that allowlist; excluded map entries and
                                   # directories are always pruned. props/required_models.json is
                                   # rebuilt from the retained maps/*/*/entities.json files.
                                   # Each generated map also names optimizationJson; old catalogs
                                   # may omit it and use the compatibility renderer below.
    shared/<game>/textures/*.png   # dedup'd world textures  <group>__<hash10>.png; black-key decals
                                   # (textures/decals/* sources) also get a reconstructed-alpha
                                   # companion <group>__<hash10>_rgba.png (tools/fod_decal_alpha.py)
    <game>/<mapId>/world.glb       # complete static world mesh; immutable collision source and
                                   # render fallback (custom loader, see §3 conventions)
    <game>/<mapId>/clip.glb        # INVISIBLE collision only: the 'textures/common/clip*' tool
                                   # brushes, rebuilt from the BSP brush/brushside/plane lumps as
                                   # real geometry (Q3-style base winding chopped by every other
                                   # brush plane, so slanted brushes stay slanted — an AABB would
                                   # seal off space the player must reach). One primitive per clip
                                   # material. Selected by CONTENTS_PLAYERCLIP (0x10000) on the
                                   # material lump, which excludes caulk/trigger/hint/clipfoliage;
                                   # 'common/ladder*' is excluded too, since materials.json already
                                   # ships those as climbable ladderVolumes. Carries POSITION and
                                   # indices only — no NORMAL/TEXCOORD, it never reaches a renderer.
                                   # Optional: "" for a map with no clip brushes at all.
    <game>/<mapId>/optimization.json
                                   # map optimization v2: CoD v59 BSP/PVS data and render-sector index
    <game>/<mapId>/sectors/*.glb   # render-only static geometry, partitioned offline on a 32 m
                                   # Unity-XZ grid; triangle closure equals world.glb exactly
    <game>/<mapId>/entities.json   # same schema as today's <map>_entities.json
    <game>/<mapId>/materials.json  # material table: [{group, texture(rel), alphaCutout, decal,
                                   # polygonOffset, sky, fallbackColor}]
                                   # polygonOffset:true = the pak shader stanza declared
                                   # 'polygonOffset', i.e. CoD1 drew this surface with a depth bias
                                   # because it is authored exactly coplanar with the one beneath
                                   # (terrain blend layers, roads, uniques/* overlays). Read from the
                                   # .shader files, not from the path: 50 of the 54 offset stanzas
                                   # live outside any 'decals/' directory, so the decal rule below
                                   # cannot see them. Routes opaque groups to
                                   # "Friends of Duty/CoD1 World Offset" and sets _OffsetFactor /
                                   # _OffsetUnits (also honoured by the CLASSIC lightmapped shader,
                                   # so both lighting modes bias alike). Never set together with
                                   # decal, whose shader already carries its own Offset.
                                   # decal:true = black-key decal ('decals_*' group, the old
                                   # BuildPavlovScene convention); texture points at the *_rgba.png
                                   # companion and the runtime routes it to the
                                   # "Friends of Duty/Pavlov Alpha Decal" shader (_Brightness .55 for
                                   # decals_decal_cratered_ground, else .68; _Opacity .95; queue
                                   # Transparent-50). Absent field = false (JsonUtility default), so
                                   # older materials.json files stay loadable.
    <game>/<mapId>/lightmaps_lamps/page_<n>.png
                                   # lamps-only world lightmap pages (Lamp Lighting Rework stage 2,
                                   # Docs/LAMP_LIGHTING_REWORK_SPEC.md): an analytic,
                                   # raycast-occluded bake of the entity lamps over the world's
                                   # lightmap charts, using the exact runtime lamp formulas plus
                                   # an N.L term. Mirrors the CLASSIC lightmaps/ set exactly —
                                   # same page count, indexing, UVs and 512x512 dLDR encoding
                                   # (decode x2) — so a surface's classic pageIndex addresses the
                                   # matching lamp page. The manifest lightmaps_lamps.json
                                   # ("FriendsOfDuty.MapLampLightmaps" v1) is named by the
                                   # catalog's lampLightmapsJson, "" when a map has no classic
                                   # pages or no lamps. Additive: old builds ignore the field and
                                   # the directory, so no gameContentVersion bump.
    <game>/<mapId>/sky.png         # equirect panorama
    <game>/<mapId>/ambience/*.mp3
  props/
    props.json                     # v2; materials plus authored XModel lods[] metadata
    <model>/prop.glb               # static LOD0 (custom loader); <model> casefolded
    <model>/lod<N>.glb             # lower authored render LODs when present
    <model>/textures/*.png
  audio/
    presentation.json              # sound alias table (normalized, §1.4)
    weapons/<...>/*.wav            # alias-referenced clips, original relative layout
                                   # (incl. ordnance: weapons/grenade/, weapons/panzerfaust/,
                                   # weapons/smokegrenade/ [UO])
    explosions/*.wav               # grenade_explo01-03 (grenade/rocket explode aliases)
    footsteps/clips/<family>/*.wav # same family layout as today
    impacts/<Surface>/*.wav        # Masonry|Wood|Metal|Snow|Dirt|Flesh
    whizby/*.wav
    fatigue/*.wav
  fx/
    textures/*.png                 # muzflash2, medium_smoke, cloudflash1a, explosion1b
                                   # + the grenade1-3.efx explosion sprite set (explosion1,
                                   # smokeplume1, pjsmoke, whitesmoke, cratered, flash1,
                                   # groundflash1, firecore, mist, snowpuff1, waterspay)
                                   # + UO smoke plume sprites smk_p_{top,none}_wht_{a,b,c}
                                   # + the COMPLETE gfx/impact set (40 names, names preserved;
                                   #   cratered and sparkflash ship as two formats each and
                                   #   the .tga wins). Two roles, and they are NOT
                                   #   interchangeable — see §1.2.1.
    efx/*.efx                      # raw CoD effect definitions (reference): muzzle flashes,
                                   # shellejects + grenade1-3/_snow/_water, fireball2_gren,
                                   # smoke/emitter_panzerfaust, UO grenade/rocket/sg_* sets,
                                   # + the small-arms impacts (default_hit, small_plaster,
                                   # small_brick, small_concrete, small_rock, small_gravel,
                                   # small_gravel2, small_glass, small_grass, small_foliage,
                                   # woodhit_small, snowhit_small, metalhit_small, flesh_hit)
  ui/
    reticle_q.png                  # scope quarter-mask
    hud/                           # in-game HUD art; sources matched by basename
                                   # (menus say .tga, several files ship as .dds)
      compassback.png              # Main/pak5 gfx/hud/hud@compassback.tga
      compassface.png              # Main/pak5 gfx/hud/hud@compassface.tga
      compass_arrow.png            # Main/pak5 gfx/hud/hud@compass_arrow.tga
      compasshighlight.png         # Main/pak5 gfx/hud/hud@compasshighlight.tga (optional)
      health_back.png              # Main/pak5 gfx/hud/hud@health_back.dds
      health_bar.png               # Main/pak5 gfx/hud/hud@health_bar.dds
      health_cross.png             # Main/pak5 gfx/hud/hud@health_cross.dds
      textback.png                 # Main/pak5 gfx/hud/hud@weaponnameback.dds
                                   # (fallback Main/pak0 ui/assets/BLACKGRAD.tga)
      death/<suffix>.png           # every gfx/hud/hud@death_<suffix> kill icon,
                                   # suffix lowercased (pak5 x25 + pak8 antitank)
      stance_stand.png             # Main/pak5 gfx/hud/stance_stand.dds (64px triangle
      stance_crouch.png            # Main/pak5 gfx/hud/stance_crouch.dds  + soldier
      stance_prone.png             # Main/pak5 gfx/hud/stance_prone.dds   silhouette;
                                   # Main preferred over UO's recompressed re-ships)
      ammo_bullet.png              # Main/pak5 gfx/icons/hud@ammo2.dds (64px strip of
                                   # five fanned rounds — NOT a single bullet; ammo5/
                                   # ammo9 are pixel-identical copies)
      ammo_back.png                # Main/pak5 gfx/hud/hud@ammocounterback.dds
                                   # (128x64 rounded ammo-box backdrop; OG menu rect
                                   # 557.5,421.625 80x40 in 640x480 virtual space,
                                   # stance menu rect 100,434.375 40x40)
      grenade_frag.png             # Main/pak5 gfx/icons/hud@us_grenade.dds (Mk2)
      grenade_smoke.png            # uo/pakuo00 gfx/icons/hud@us_smokegrenade.dds
                                   # (UO-only, warn-only when absent)
```

### 1.2.1 Bullet impacts: which `gfx/impact` art is a mark, and which surface gets it

Two independent sources in the original settle this — do not guess from file names.

**What is a mark (decal) vs. a sprite (particle).** `Main/pak5.pk3
fxshaders/pj_impact.shader` carries a literal `// IMPACT DECALS` banner, and every
shader below it declares `surfaceparm nonsolid`, `surfaceparm trans` and
`polygonOffset2` — the Q3-lineage signature of a polygon laid onto existing
geometry. Everything *above* the banner blends as an ordinary billboard.
`fxshaders/pj_fx.shader` adds three more `polygonOffset2` marks.

| role | names |
| --- | --- |
| marks (`polygonOffset2`) | `bullethole1`, `bullethole2`, `bullethit_plaster`, `bullethit_plaster2`, `bullethit_wood1`, `bullethit_wood2`, `bullethit_sand`, `bullethit_snow`, `bullethit_glass`, `bullethit_glass2`, `stone_singleshot1`, `cratered`, `cratered_ground`, `cratered_grounddetail` |
| sprites | `bark_gib*`, `dustlayer1`, `dusty`, `dusty_puff`, `flesh_hit1/2`, `flesh_hitgib`, `foliage_gib1/2`, `foliage_stick`, `grass_piece1/2`, `gravelpuff`, `metal_spark1`, `snow1`, `snowpuff`, `sparkflash`, `sparktrail`, `stone_gib1/2`, `stone_piece1/2`, `wood_splinter1/2`, `woodpuff` |

`bullethit_*` are **marks, not puffs** — the `bullethit_` prefix names the *surface*
the hole is in, not a burst. `metal_spark1`, `sparkflash` and `sparktrail` ship with
no alpha channel and are declared `blendFunc GL_ONE GL_ONE`, so they must be drawn
additively or they render as opaque black squares.

**Which mark goes on which surface.** Each `fx/impacts/<surface>.efx` ends in a
`Decal { life … size { start … } shaders [ … ] }` block; the engine picks one shader
from the list at random per hit. `size` is CoD units (×0.0254 for metres) and reads
as the quad's full width; `life` is milliseconds.

| efx | mark shaders | size (units) | life (ms) |
| --- | --- | --- | --- |
| `default_hit` (unclassified) | `bullethole1`, `bullethole2` | 2–4 | 8000 |
| `small_plaster` | `bullethit_plaster`, `bullethit_plaster2` | 3–5 | 10000 |
| `small_brick` | `bullethit_plaster2` | 3–6 | 10000 |
| `small_concrete` | `bullethit_plaster`, `bullethit_sand` | 3–6 | 10000 |
| `small_rock` | `stone_singleshot1` | 3–4 | 10000 |
| `small_gravel` / `small_gravel2` | `bullethit_sand` | 4–5 / 5–7 | 7000 / 8000 |
| `woodhit_small` | `bullethit_wood1`, `bullethit_wood2` | 5–7 | 10000 |
| `snowhit_small` | `bullethit_snow` | 5 | 10000 |
| `metalhit_small` | `bullethole2` | 2 | 10000 |
| `small_glass` | `bullethit_glass` | 3–4 | 10000 |
| `small_grass`, `small_foliage`, `flesh_hit`, `waterhit_small` | *(no Decal block)* | — | — |

`FodImpactContentBuilder` mirrors this table: one array of marks per surface, one
picked at random per impact, plus `bullethole1`/`bullethole2` mixed into every hard
surface because that is exactly what `default_hit` does for geometry the original
did not classify — and our runtime buckets nine surface types where CoD had dozens.
Flesh, Foliage and Water place no mark, matching their empty efx.

### 1.2.2 `tag_*` attachment nodes in `weapons/worldmodels/<world>/world.glb`

A CoD XModel stores its attachment points as **skeleton bones** named `tag_*`, not as
separate objects: `tag_flash` (muzzle), `tag_brass` (ejection port), `tag_barrel`,
`tag_weapon` (the model root the renderer parents to a hand bone). The viewmodel GLBs are
skinned, so those bones survive as glTF joint nodes; a world model is a **static** GLB, and
baking the rig away used to delete them with it.

`tools/fod_export_common.py` now reads the tag bones' rest transforms
(`armature.matrix_world @ bone.matrix_local`) *before* the rig is dropped and re-emits each
one as a parentless Blender **empty** with the same name. glTF writes an object with no mesh
data as a node with no `mesh` property, and `FodGlbStaticLoader` turns exactly that into a
plain named `GameObject` — so the runtime finds the muzzle with a by-name search on the
instantiated template. Enabled only for the world-model export path
(`export_static_pak_model(..., keep_tags=True)` in `batch_export_cod1_models.py`);
projectiles, the shellcasing and the prop exporter keep the tag-less behaviour and their GLBs
are byte-identical across the change.

Contract for the runtime:

- Tag nodes are **root** nodes, in the same baked space as the mesh nodes (inches ×0.0254 →
  metres, `export_yup`), so their node translation is already the metre-space offset from the
  template root.
- The bone's uniform 0.0254 scale is divided out: tag nodes carry `scale = [1,1,1]` and a
  clean rotation quaternion, while mesh nodes keep `scale = 0.0254` with raw-inch vertices.
- Orientation is the source tag's, unmodified. CoD tags point **+X down the barrel**; after
  the glTF→Unity mirror (`position (-x,y,z)`, `rotation (x,-y,-z,w)`) that direction becomes
  **`-tagTransform.right`** in Unity, *not* `forward`. `tag_weapon` is always the identity.
- Not every world model has tags. The five thrown/ammo props
  (`weapon_mk2fraggrenade`, `weapon_british_handgrenade`, `weapon_russian_handgrenade`,
  `weapon_nebelhandgrenate`, `weapon_panzerfaust_ammo`) have no rig in the source at all;
  `w_us_grn_m18_game` has only `tag_origin`; `weapon_fg42` has `tag_flash` but no `tag_brass`.
  Runtime code must degrade gracefully when a name is absent.

### 1.3 `fodpak.json` (JsonUtility-compatible: flat fields, arrays of objects, no dict maps)

```json
{
  "format": "FriendsOfDuty.ContentPackage",
  "version": 1,
  "gameContentVersion": 1,
  "exporterVersion": "1.0.0",
  "createdUtc": "2026-07-25T00:00:00Z",
  "sourceSummary": "CoD1 (Main, 11 pk3) + UO (uo, 7 pk3)",
  "hasUnitedOffensive": true,
  "categories": [
    {"name": "weapons",  "count": 19},
    {"name": "players",  "count": 4},
    {"name": "maps",     "count": 7},
    {"name": "props",    "count": 130},
    {"name": "audio",    "count": 0},
    {"name": "fx",       "count": 0}
  ],
  "orientation": {
    "viewmodelYawOffsetDegrees": 90.0,
    "playerVisualYawOffsetDegrees": 90.0,
    "playerAuthoredForward": "-X"
  }
}
```

The game requires `format` match and `gameContentVersion == 1`. `orientation` values feed the
existing C# yaw constants so orientation mismatches from the FBX→glTF switch can be corrected
by regenerating/patching the manifest without a code change.

#### 1.3.1 The version contract, and what happens when it breaks

Three numbers have to agree between the exporter and the game:

| manifest field        | exporter (`exporter/package.py`) | game (`FodPakManifest`)        |
|-----------------------|----------------------------------|--------------------------------|
| `version`             | `PACKAGE_VERSION`                | `ExpectedPackageVersion`       |
| `gameContentVersion`  | `GAME_CONTENT_VERSION`           | `ExpectedGameContentVersion`   |
| `sourcePolicyVersion` | `POLICY_VERSION`                 | `ExpectedSourcePolicyVersion`  |

`gameContentVersion` is the one that moves in normal work: **bump it in both places whenever
the exporter starts emitting a shape older builds cannot read.** `exporterVersion` is a
human-facing stamp and is deliberately *not* part of the check — the game cannot know which
exporter revisions produced compatible output, only which schema it can parse.

A package whose numbers do not match is refused at mount, and the refusal has a screen of its
own rather than being reported as "no content found". `FodPackageSurvey` re-reads the manifest
without validating it (`FodPakManifest.TryReadStamp`) and classifies the directory:

| verdict        | meaning                                                        |
|----------------|----------------------------------------------------------------|
| `Absent`       | no directory, or no `fodpak.json` in it                         |
| `Foreign`      | parsed, but not our `format` — another product's data, never touched |
| `Outdated`     | our format, built for an older schema                           |
| `TooNew`       | our format, built for a newer schema                            |
| `Incompatible` | schema matches, but the package fails the mount checks          |
| `Unreadable`   | a `fodpak.json` that could not be read or parsed                |
| `Valid`        | mountable                                                       |

Everything except `Absent`, `Foreign` and `Valid` publishes `ContentGatePhase.Outdated` with
the report attached to `ContentGateState.Rejected`, which is what the boot flow's
outdated-package screen draws (§5, stage 2a).

### 1.4 Manifest schemas (all JsonUtility-safe)

**weapons.json** — one entry per weapon (19 guns + ordnance: fraggrenade, mk1britishfrag,
rgd33, stielhandgranate, and smokegrenade when UO is present):
```json
{"format":"FriendsOfDuty.Weapons","version":1,"weapons":[{
  "id":"mp40", "displayName":"MP40", "world":"weapon_mp40",
  "viewmodelGlb":"weapons/viewmodels/mp40/viewmodel.glb",
  "worldGlb":"weapons/worldmodels/weapon_mp40/world.glb",
  "viewmodelMaterials":[{"material":"viewmodel@mp40_1","texture":"weapons/viewmodels/mp40/textures/viewmodel@mp40_1.png","cutout":false}],
  "worldMaterials":[...same shape...],
  "projectileGlb":"",              // weapons/projectiles/<model>/projectile.glb when the
                                   // definition has projectile_model (wired by the
                                   // projectiles step); "" or absent otherwise
  "projectileMaterials":[...same shape as worldMaterials...],
  "animations":["mp40_combined_idle","mp40_combined_fire", "..."],
  "definition":{ ...exact same fields as today's weapon_manifest.json definition block:
     name, weapon_file, display_name, world, magazine, reserve, damage, rpm, automatic,
     bolt_action, segmented_reload, reload_ammo_add, reload_start_add, ads, ads_fov,
     ads_in, ads_out, pickup_sound, fire_sound, last_shot_sound, rechamber_sound,
     reload_sound, reload_empty_sound, reload_start_sound, reload_end_sound,
     muzzle_effect, shell_effect,
     ...plus OPTIONAL ordnance fields, emitted only when the WEAPONFILE carries a
     meaningful value (C# mirrors default them when absent):
     weapon_class ("rifle"|"pistol"|"mg"|"rocket"|"grenade"|"smoke"; default rifle,
       omitted for rifle), fuse_time (s), cook (bool), explosion_radius /
     explosion_inner_damage / explosion_outer_damage (CoD units & HP),
     projectile_speed (CoD units/s), throw_speed_up (CoD units/s, frag=120),
     projectile_model (xmodel name, no "xmodel/" prefix), shared_ammo_cap (int),
     explosion_type ("grenade"|"rocket"|"smoke"), bounce_sound / explode_sound /
     throw_sound / pin_sound (presentation.json alias names) }
}]}
```
CoD units convert to metres via 0.0254 in C#. The ordnance definitions are parsed from the
OFFICIAL WEAPONFILEs (extract_cod1_ordnance.py restages them from Main/pak0, with the
Main/pak8 patch override for panzerfaust_mp, replacing copies a mod pak may have overridden;
rerun the viewmodels step after the ordnance step if a mod pak had polluted the staging).

**players.json** (v3, `gameContentVersion` 4): the four MP player rigs carry the exact active
`mp/playeranim.script` inventory. Each animation records `name`, `looped`,
`layers`, `actionRoles`, `scriptOccurrences`, and the mounted-alias provenance
fields `sourceAlias`, `turretWeapon`, `turretStance`, `turretPhase`,
`turretHorizontal`, `turretVertical`. Retail `turretanim` aliases expand to all
108 directional stand/prone MG42 gunner clips. The script's four jeep
driver/gunner aliases are deliberately NOT expanded — this product ships no
vehicle — and are recorded in `source.excludedTurretAnimations` instead, which
is what keeps "cut from the product" distinct from "missing from the install".
The four direct `c_mp_jeep_passenger_*` bindings leave for the same reason.
Campaign animations, duplicate `pb_*MG42gunner_*` families, and the weapon-rig
`*MG42gun_*` clips are excluded from the player GLB. The latter are not discarded: `mg42_bipod` is the one
animated prop in `props.json` v2 and carries the exact seven compatible MP
weapon-rig clips (stand/prone aim, fire, recover, plus prone level fire) with
their `mg42_bipod_stand_mp`/`mg42_bipod_prone_mp` WEAPONFILE provenance.

**props/props.json** (v2): every production static prop carries the complete
authored XModel LOD inventory. `glb` remains the backward-compatible LOD0 path,
and therefore must equal `lods[0].glb`; the animated `mg42_bipod` is the one
exception and keeps its animation metadata instead.

```json
{"format":"FriendsOfDuty.Props","version":2,"props":[{
  "name":"barrel_black1",
  "glb":"props/barrel_black1/prop.glb",
  "materials":[{"material":"barrel_black","texture":"props/barrel_black1/textures/barrel_black.png","cutout":false}],
  "lods":[
    {"glb":"props/barrel_black1/prop.glb","sourceSurface":"barrel_black1","distance":256.0},
    {"glb":"props/barrel_black1/lod1.glb","sourceSurface":"barrel_black2","distance":512.0},
    {"glb":"props/barrel_black1/lod2.glb","sourceSurface":"barrel_black3","distance":0.0}
  ]
}]}
```

`sourceSurface` preserves the source XModel surface identifier. `distance` is
the authored far boundary in CoD units (inches), is strictly increasing when
positive, and a zero final distance means the source did not author a cull
boundary for its lowest level. The runtime converts those boundaries into
Unity `LODGroup` screen-height thresholds using CoD1's reference 80° horizontal
4:3 view. Only LOD0 participates in prop collision; lower levels are
render-only and release their CPU mesh copies after the `LODGroup` is built.
Unity still accepts an older entry without `lods` as an LOD0-only fallback,
but the production exporter and package validator require `lods` on every
static prop.

**maps/catalog.json**: same logical content as today's catalog minus Unity asset paths:
`{"format":"FriendsOfDuty.MapCatalog","version":2,"codUnitToMetre":0.0254,"maps":[{"game","mapId","title","source","sourceFingerprint","worldGlb","optimizationJson","entitiesJson","materialsJson","skyPng","environment{...unchanged}","audio{...unchanged, assetPath→relative pak path}","recommendedSpawn","spawns":[...unchanged]}]}`

`optimizationJson` points to a **FriendsOfDuty.MapOptimization v2** document:

```json
{
  "format":"FriendsOfDuty.MapOptimization",
  "version":2,
  "sourceBspVersion":59,
  "codUnitToMetre":0.0254,
  "sectorStrategy":"unity-xz-grid-v1",
  "sectorSizeMetres":32.0,
  "fallbackWorldGlb":"maps/cod1/mp_pavlov/world.glb",
  "sectors":[{
    "name":"grid_m001_p000",
    "glb":"maps/cod1/mp_pavlov/sectors/grid_m001_p000.glb",
    "gridX":-1,
    "gridZ":0,
    "clusterIndices":[12,13],
    "alwaysVisible":false,
    "triangles":742,
    "boundsCodUnits":{"minimum":[-1,-1,-1],"maximum":[1,1,1]}
  }],
  "visibility":{
    "clusterCount":666,
    "rowBytes":84,
    "pvsBase64":"...",
    "planeCount":1234,
    "planesBase64":"...",
    "nodeCount":1234,
    "nodesBase64":"...",
    "leafCount":1235,
    "leavesBase64":"...",
    "cells":[{"minimum":[-1,-1,-1],"maximum":[1,1,1]}]
  },
  "counts":{"sectors":150,"triangles":61280,"alwaysVisibleTriangles":3413,"clusters":666,"cells":20}
}
```

The numeric values above illustrate the schema; validators use the actual
per-map counts. The exporter decodes the original uncompressed, LSB-first CoD1
and CoDUO v59 PVS. It clips each complete static render triangle through the
BSP tree to find the exact union of intersected visibility clusters, then
emits that triangle exactly once into the 32 m sector containing its centroid.
A sector renders when any of its owner clusters is visible from the camera
cluster. Sky, all-solid faces, numerical ambiguity, or the clipping complexity
guard go to one explicit always-visible sector. Package and runtime validation
require the optimized sectors' triangle total to equal `world.glb`; this makes
the result conservative rather than permitting an uncertain surface to
disappear.

**audio/presentation.json**: today's alias schema with `asset` field removed; `file` stays
relative to `audio/` (e.g. `weapons/mp40/mp40_1.wav`).

**footsteps**: the game rebuilds `SurfaceSoundSet[]` in C# from the family-dir layout
(`audio/footsteps/clips/<family>/<prefix>NN.wav`), mirroring the old editor logic — no JSON
map needed (the old footstep_manifest "clips" object-map is not JsonUtility-parseable).

---

### 1.6 Path case is part of the format

**Every path a package refers to must match its spelling on disk exactly, including
case.** A package is produced on one OS and played on another: Windows and macOS
resolve a path whatever its case, Linux and SteamOS do not. An export built on a
case-insensitive filesystem therefore looks perfect on the machine that built it and
fails on a Steam Deck — this shipped once, where `audio/presentation.json` asked for
`Explosions/Explo_metal01.wav` while the exporter had written
`audio/explosions/Explo_metal01.wav`, and the catalog build refused the whole
package over one capital letter.

Two mechanisms enforce it:

- **Export side** — `package.validate_path_case_consistency` fails the build when any
  manifest reference (or `.glb` image/buffer URI) exists only under different casing,
  and when two packaged files differ only by case. It compares against the spellings
  `rglob` returned rather than asking the filesystem, so it detects the fault on the
  case-insensitive hosts that cause it. The pipeline's `package` step runs it on every
  export; `--skip-validate` bypasses it and must not be used for a package anyone else
  will mount.
- **Import side** — `FodContent.ResolvePath` repairs a case-mismatched path against a
  cached per-directory index, so packages published before the gate existed still
  mount. Exact hits return first, so case-insensitive hosts pay nothing.

Extractors should keep writing lowercase paths (`canonical_sound_file` +
`canonical_output_path` are the established helpers — the latter also case-renames an
existing directory, which a plain write cannot do on APFS). Lowercasing is the easy
way to satisfy the rule, but the rule the validator enforces is only "references match
disk".

## 2. Exporter app (bundled with the game)

`exporter/` at repo root (copied into builds post-build as `<build>/Exporter/`):

- `friends_of_duty_exporter.py` — tkinter GUI (falls back to `--cli` mode). Screens:
  1. Requirements check: Python ≥3.10, Pillow, numpy. Blender is not a
     requirement: the exporter downloads the pinned 4.5.1 build itself
     (`exporter/blender_provisioner.py`). Legacy auto-detect text (auto-detect
     `/Applications/Blender.app`, PATH, common Windows/Linux paths; manual browse).
     Offers `pip install pillow numpy` on demand.
  2. Paths: CoD install dir (validates `Main/pak0.pk3`… present; `uo/` optional but
     recommended), output dir (default from `--output`, which the game passes as its
     `persistentDataPath/Content/current`).
  3. Export: runs the pipeline with per-step progress + live log + cancel; resumable
     (steps are incremental/skippable when outputs exist and fingerprints match).
  4. Done: optional "Save transportable .fodpak (zip)" + "Launch game".
- `pipeline.py` — step orchestrator (subprocess per step; Blender steps via
  `blender --background --python <tool> -- <args>`).
- Steps (wrapping/extending existing `tools/`):
  1. Multiplayer-only source closure:
     `extract_cod1_assets.py` stages only the selected MP WEAPONFILE graph,
     weapon/player XModels and exportable `mp/playeranim.script` XAnims into
     `cod1_source`; `extract_cod_multiplayer_model_assets.py` separately
     stages only XModels placed by the selected official MP BSP/GSC roster
     into `cod_multiplayer_source`. Both expand compiled XModel dependencies
     to their exact parts/surfaces/skins. The global retail `xmodel/`,
     `xanim/`, `skins/` and SinglePlayer map roots are never bulk-copied.
     Player-script `turretanim` aliases are expanded only to the retail
     directional MP MG42 gunner family; the jeep driver/gunner families are
     refused with the vehicles. The map-model closure separately stages
     the two map-referenced MP MG42 WEAPONFILEs and their seven nine-bone
     weapon-rig XAnims so `mg42_bipod` can export as an animated prop.
  2. viewmodels: `export_cod1_demo_viewmodels.py --format glb` → pak `weapons/viewmodels/` +
     weapons.json data (roster includes the grenades; the UO smokegrenade joins once
     `extract_cod1_ordnance.py` has staged its members into cod1_source). The
     v3 definition also carries exact MP `maxAmmo` and
     `dropAmmoMin`/`dropAmmoMax` values used when a fallen/same-ammo weapon
     supplies ammunition. UO's layered
     WEAPONFILE overrides are retained (they deliberately reduce these bounds)
     rather than reusing the base CoD table.
  3. world models + shellcasing: `batch_export_cod1_models.py ... --format glb --roster <from weapons.json>`
  3b. projectiles: `batch_export_cod1_models.py ... projectile --format glb` → pak
     `weapons/projectiles/` + weapons.json projectileGlb/projectileMaterials wiring
  4. players: `export_cod1_multiplayer_players.py --format glb` → pak `players/`
  5. presentation audio/fx: `extract_cod1_weapon_presentation.py` (normalized manifest,
     PNG-converted weapon FX, and MP-script global aliases; notably the stock
     minefield warning click and detonation)
  6. footsteps: `extract_cod1_footsteps.py` → pak `audio/footsteps/`
  7. impacts/minefield/whizby/fatigue/reticle: `extract_cod1_impacts.py`.
     The minefield selection is the exact MP-script graph
     `newimps/minefield.efx -> fluff1 + dirthit_mortar`, plus all eight shader
     images it names; no SinglePlayer effect directory is swept.
  7b. ordnance: `extract_cod1_ordnance.py <game> --pak-root <out> --staging <cod1_source>` —
     restages the official grenade/panzerfaust WEAPONFILEs + the UO M18 smoke grenade
     members, converts the explosion/smoke FX sprite sets, copies the ordnance .efx sets,
     and extracts + MERGES the ordnance sound aliases into `audio/presentation.json`
     (single owner of grenade_bounce/explode, rocket_explode, pin/throw and UO smoke
     aliases; existing aliases from step 5 are preserved untouched)
  8. weapon data: WEAPONFILE dumps → `weapons/data/`
  9. maps: `import_cod_multiplayer_maps.py --pak-root <out>` (GLB writer, PNG textures,
     normalized catalog v2). The exporter has one hard product allowlist:
     Carentan, Chateau, Pavlov, Railyard and Rocket from CoD1, plus Arnhem and
     Cassino when UO is enabled. `--maps` can select an allowlisted development
     subset; the legacy `--all-mp` flag is a compatibility no-op and cannot
     widen the roster. Excluded catalog entries, map directories, and unreferenced
     shared map textures are pruned. **mp_pavlov is generated by the same unified
     path** — the hand-built `existingPavlov` special case is retired. The same
     pass writes the complete `world.glb` fallback plus optimization v2's
     offline 32 m render sectors and exact v59 cluster PVS data.
  10. props: `export_cod_multiplayer_props.py --format glb` (includes former
      PavlovProps). Production export requires cod-asset-importer v3.6+
      authored-XModel-LOD API 1 and writes every static prop's authored levels,
      source-surface provenance, and CoD distance boundaries.
  11. package: write `fodpak.json`, verify the exact selected-tier rosters
     (23/24 weapons, four player rigs, 5/7 maps), the complete detached
     source closure and its per-member provenance, optional zip
- Blender availability of `tools/cod-asset-importer` is validated up front,
  including authored-XModel-LOD API 1. API0/v3.5 remains usable by legacy
  developer calls that explicitly export LOD0 only, but the production GUI,
  CLI, prop step, and final package validation reject it rather than silently
  discarding authored levels. **Prepare importer** first tries a matching
  capable bundled stable-ABI extension; when none is available (including
  when an older bundled artifact is API0), it performs a one-time local build
  from the vendored Rust source. That fallback needs the Rust toolchain but
  downloads no game content. The equivalent check/build command is
  `python3 exporter/build_importer.py --require-lod`.

Existing tools keep their FBX/Unity-targeting code paths behind flags for dev use, but the
default invocation from the exporter targets the pak layout with **relative paths only** in
all manifests (no absolute paths, no `Assets/...` strings).

## 3. Model container conventions

- **Skinned/animated models** (viewmodel combined rigs, players): GLB exported by Blender
  (`export_image_format='NONE'`, actions→one glTF animation per action, forced sampling,
  30fps). Loaded at runtime with **glTFast 6.19** `ImportSettings.AnimationMethod = Legacy`,
  materials via a custom `IMaterialGenerator` that returns Standard-shader materials; textures
  rebound afterward from the manifest material tables (glTF material names are preserved).
  Animation/clip names must survive exactly (e.g. `mp40_combined_fire`, `pb_stand_alert`) —
  the runtime matches the same lowercase `_token` suffix rules as today.
- **Static models** (map worlds, props, weapon world models, shellcasing): GLB parsed by the
  custom `FodGlbStaticLoader` (JSON+BIN chunks; POSITION/NORMAL/TEXCOORD_0 + indices;
  node hierarchy with TRS; material name per primitive; no skins/animations).
  A node without a `mesh` becomes a plain named `GameObject` — that is how weapon world
  models carry their `tag_flash`/`tag_brass` attachment points (§1.2.2).
  glTF→Unity conversion (must match glTFast's convention so both loaders agree):
  position `(-x, y, z)`, rotation `(x, -y, -z, w)`, triangle winding reversed, UV `v' = 1 - v`.
  Map GLBs are written by a pure-Python writer from BSP data: CoD Z-up `(x,y,z)` →
  glTF `(x, z, -y) * 0.0254` (which the loader's `-x` mirror turns into today's proven
  Unity-space result `(-x, z, -y) * 0.0254`).
- Static-loader meshes start readable so runtime MeshColliders can cook.
  Collision-bearing `world.glb` and prop LOD0 meshes retain their CPU data.
  Duplicate render-only meshes do not: optimized map sectors and lower prop
  LODs call `UploadMeshData(true)` after validation, material binding, and LOD
  bounds setup.
- Textures: everything is **PNG** in the pak (exporter converts DDS/TGA/JPG via Pillow,
  preserving base names like `viewmodel@mp40_1.png`; `@` and `__<hash>` conventions kept).
  PNG remains the portable source/fallback, not the format repeatedly supplied
  to the GPU on supported desktops. On the first Windows/macOS/Linux load of
  an eligible texture, Unity decodes it, creates mipmaps as required, compresses
  it to DXT5, stores the raw GPU-ready mip payload under
  `<persistentDataPath>/Cache/RuntimeTextures/v2`, and releases the CPU copy.
  Later launches validate and load that payload directly, avoiding repeated PNG
  decode and runtime compression. The cache is bounded to 768 MiB and 12,000
  entries; stale/corrupt/missing records, an unwritable cache, unsupported
  DXT5, or ineligible dimensions transparently fall back to the source PNG.
  Set `FOD_DISABLE_TEXTURE_CACHE=1` (also accepts `true` or `yes`) to bypass
  persistent reads/writes for diagnosis.

## 4. Unity runtime architecture (new code in `Assets/Scripts/Content/`)

`FodContent` static facade:
- `TryMount()`: discover → validate `fodpak.json` → set `IsMounted`, raise `ContentMounted`.
- File access: `ResolvePath/ReadBytes/ReadText/FileExists` (all relative to mount root).
- Loaders: `LoadTexture(rel, profile)` (profiles: SkinMipmapped, SpriteClampNoMip,
  PanoramaClamp…), `LoadWav(rel)` (custom PCM 8/16-bit mono/stereo parser),
  `LoadMp3Async(rel)` (UnityWebRequestMultimedia file:// streaming).
- `FodGlbStaticLoader.Load(rel)` → `GameObject` template (inactive, under hidden
  DontDestroyOnLoad root) with meshes/materials via `FodMaterialFactory`.
- `FodSkinnedModelLoader.LoadAsync(rel)` (glTFast) → inactive template with legacy
  `Animation` + clips.
- **`RuntimeWeaponCatalog.BuildAsync()`** mirrors old `BuildFpsDemo.Rebuild`: hydrates 19
  `WeaponDefinition` ScriptableObject instances (stats from weapons.json, audio cues via
  presentation.json aliases → WAV clips with per-variant volume/pitch, muzzle textures via
  the existing efx-substring heuristics, shell template, viewmodel/world templates,
  entity template GameObjects carrying `WeaponAudioPlayer` + `Cod1HitscanWeapon` bound via a
  new `WeaponEntity.BindDefinition(def)` public API). Exposes synchronous `Get(id)` /
  `All` once built (server hot paths stay cheap).
- **`RuntimePlayerCatalog.BuildAsync()`**: player GLBs → templates;
  `NetworkPlayerCharacterFactory` / `LocalPlayerShadowPresentation` switch from
  `Resources.Load` to catalog lookups (instantiated clones get `SetActive(true)` —
  templates are inactive).
- **`RuntimeMapBuilder`**: replaces the per-map baked scenes. One shipped empty scene
  `Assets/Scenes/Multiplayer/OriginalMapRuntime.unity` for ALL maps; catalog `sceneAssetPath`
  semantics replaced by mapId-driven runtime assembly. Builder (subscribed to sceneLoaded at
  `SubsystemRegistration` so it runs before other handlers) synchronously parses the complete
  `world.glb` → meshes + `FodMaterialFactory` materials (Standard/cutout/sky/decal conventions,
  same constants as the editor builders) → MeshColliders (same cooking flags) +
  `Cod1FootstepSurface` via `FodSurfaceClassifier` (classification logic moved from
  BuildPavlovScene to runtime). On a client it then validates and loads the render-only
  `optimization.json` sectors, rebinds their materials, releases their CPU mesh data, and only
  then disables the fallback world's renderers; the fallback meshes remain resident for
  collision. The gameplay camera is located in the exact v59 BSP leaf each frame and a sector
  stays visible when any of its `clusterIndices` is in that camera cluster's PVS.
  Missing/malformed optimization data, a partial sector load, triangle-closure mismatch, no
  active gameplay camera, or a camera outside/inside a solid leaf all fail open: every sector
  remains visible, or the builder keeps `world.glb` rendering with its conservative runtime
  32 m split. PVS culling applies to validated **static world geometry only**; props, movers,
  destructibles, and gameplay presentation remain always visible. Developers can compare the
  result with `RuntimeMapVisibilityCuller.DebugDisableCulling = true` or the
  `-disableMapVisibilityCulling` command-line switch.
  Builder then adds props from prop templates (wrapper GameObject carries the
  entity transform) → point lights (same intensity/range formulas) → spawn points →
  ambient/emitter audio → skybox material + fog + sun/ambient from catalog environment →
  root object with `OriginalMultiplayerMap` component (existing detection contract).
  Dedicated/batchmode builds geometry+colliders+spawns only.
- **`FodPropLibrary`**: loads the authored GLBs in each static prop's `lods`
  array and constructs a Unity `LODGroup` from the original CoD distance
  boundaries. LOD0 remains the sole collision source. Lower levels are
  render-only and non-readable after setup; an unexpected secondary-LOD load
  failure cleans up the partial levels and conservatively keeps LOD0.
- **Footsteps**: `FodFootstepContentBuilder` recreates `SurfaceSoundSet[]` (volumes/tuning
  stay in C#) and injects via existing `Cod1FootstepController.Configure`.
- **Impacts**: build `ProjectileSurfaceImpactProfile[]` from pak and call
  `ProjectileImpactSystem.Configure`; whizby/fatigue loaders redirected to pak.
- `Cod1WeaponTuning` reads `weapons/data/<id>.txt` from pak; scope reticle from `ui/`.
- `OriginalMultiplayerMapSelection` reads catalog from pak; cache invalidated on mount.

## 5. Startup: the Boot scene

`Assets/Scenes/Frontend/Boot.unity` is build scene 0; `MainMenu` is scene 1.
`FodBootFlow` (`Assets/Scripts/Frontend/FodBootFlow.cs`) owns it, and its whole job is:
show the studio mark, let the active packages mount, go to the menu.

This used to be a four-stage first-run wizard — locate a package, choose a source, run an
exporter against a Call of Duty installation, prepare the data — because the game could not
start without one specific package and had to talk the player through producing one. None of
that is true any more. The game ships its own content (`Assets/Resources/basepak.bytes`,
unpacked on first launch and mounted last), reads any number of `.fodpak` packages, and
treats "no package installed" as an ordinary state rather than a failure. Generators live
outside the build entirely and are never launched from here.

Stages:

1. **Studio mark** — black plate, ~3.4 s, skippable with any input. Authored art at
   `Resources/UI/fod_studio_logo` replaces the lockup when present.
2. **Wait for the gate** — only `Mounting` and `Building` hold the screen, and only for
   `ContentWaitCeilingSeconds` (90 s). `Ready`, `Failed`, `Missing` and `Outdated` all
   proceed. **A package that fails to mount must never cost the player their menu**: the
   gate reports it and the MODS page is where it gets fixed.
3. **Handoff** — `SceneManager.LoadScene("MainMenu")`.

`FriendsOfDutyMainMenu` therefore may NOT assume a mounted package. `FodContentBootstrap`
still re-checks on app focus, which is what picks up a package produced or dropped in while
the game was in the background. `StartMatch`/dedicated match start remain hard-gated on
content readiness.

### 5.1 MODS

There is no GAME DATA page and no whole-content wipe. `ModsPage`
(`Assets/Scripts/Multiplayer/ModsPage.cs`) is the whole of content management: it lists every
discovered package, installs new ones, enables and orders them, and removes them one at a
time. A per-package uninstall is strictly better than a purge now that the game does not
depend on any single package.

Packages are listed **without unpacking them**. `FodPakSummary.TryReadArchive` pulls the
manifest and preview straight out of the zip in about ten milliseconds, so a player can
browse and reorder packages that have never been extracted. Only ACTIVE packages are ever
unpacked, and that happens at boot.

**Install from a link.** `FodRemoteContentSource` accepts an http(s) link to a
`.zip`/`.fodpak`, and additionally understands a GitHub repository link
(`github.com/owner/repo`, `.git`, `/tree/branch`), mapping it to
`codeload.github.com/owner/repo/zip/refs/heads/<branch>` and trying `main` then `master`
when no branch is named. A github.com URL that already points at an archive is passed through
untouched. `FodContentDownloader` streams to `download.tmp` with `DownloadHandlerFile`
(progress, Content-Length and free-space guards, 40 GB ceiling, `removeFileOnAbort`) and
renames the finished file to `download.fodpak`.

`FodModInstaller.TryAdopt` then does the smallest possible thing: it confirms the file reads
as a package at all — the same manifest read the list itself is built from — and **moves it
into the drop folder** (§1.1) under a name derived from the package's own id. Nothing is
unpacked. Boot discovers, extracts and mounts it exactly as it would a file the player
dropped in by hand: one discovery mechanism, one set of rules, nothing that only works for
downloads.

It is also the **upsert**. An archive already installed under the same id is deleted *after*
the new one is in place, so a failed move leaves the previous package working rather than
removing it for a replacement that never arrived. A Workshop-managed package is refused
instead, because Steam re-syncs its own copy. Extractions are deliberately untouched: the
package being replaced may be the one this session is mounted from, with its textures and
audio live, and deleting that directory would break the running session on POSIX and fail
outright on Windows. The extraction stamp records the source archive's size and write time,
so a replaced archive re-extracts on the next launch — which is when the change applies
anyway.

The package id is authored by whoever built the package and ends up as a path component, so
`FodModInstaller.ArchiveFileNameFor` rewrites rather than trusts it: anything outside
`[A-Za-z0-9._-]` collapses to a single `-`, the length is capped, and an empty or all-dot
result falls back. `TryResolveTarget` then confirms the combined path really did land
directly in the drop folder, so the check and the sanitiser cannot drift apart silently.
`FodModInstallerTests` pins both.

**Enable / order / remove.** Toggling and reordering write to `FodModSettings` (`mods.json`,
an `active` list plus a `known` list) and nothing else; `FodArchiveStore.EnsureActiveExtracted`
reads it at boot to decide what to unpack. A change applies on the next launch, because the
mounted package is live and `FodContent.ContentMounted` fans out to more than eight static
caches that rebuild wholesale. The page says so and offers **RESTART NOW** rather than
appearing to toggle instantly. `FodApplicationRestart` closes the game and arranges for it to
come back: under Steam's own shell (Big Picture or a Deck in game mode) via
`steam://rungameid/<appid>` with a short hand-off delay, because Steam owns the window and
will not launch an app it still believes is running; anywhere else by starting the executable
directly. If neither route is available the game still closes, and the copy says so instead
of promising a relaunch it cannot deliver.

**Layout contract.** Every install widget is present on every frame — buttons disable rather
than disappear, and the status line and progress bar always hold their space — and every
button records a request that `Sample()` carries out on the **Layout** pass. Starting a
download, adopting one and restarting all change what the page draws, and doing that during
Repaint is exactly how the two IMGUI passes fall out of step. RESTART NOW and the uninstall
confirm are the only conditional widgets, and both are gated on values latched for that
reason.

**Not yet built.** Steam Workshop install/subscribe. `FodArchiveStore.IsSteamManaged`
recognises a Workshop-owned path and the page refuses to delete or replace one, but there is
no ISteamUGC integration at all.

## 6. Removal of CoD content from the repo/build

After the runtime path is verified: all CoD-derived directories under `Assets/` are MOVED
(not deleted) to `<repo>/removed_from_unity/` (preserving structure, including .meta files);
the retired editor generators/importers/validators are deleted (git history preserves them);
`EditorBuildSettings` scene list becomes `MainMenu` + `OriginalMapRuntime`. Build scripts
gain a post-build step copying `exporter/` + `tools/` into the build output.
