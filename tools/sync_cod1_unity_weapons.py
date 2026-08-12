"""Copy exported CoD 1 viewmodels and world models into the Unity project."""

from __future__ import annotations

import json
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
EXPORT_ROOT = ROOT / "extracted_assets/unity_ready"
UNITY_ROOT = ROOT / "FriendsOfDutyUnity/Assets/Models"
VIEWMODEL_SOURCE = EXPORT_ROOT / "demo_viewmodels"
WORLD_SOURCE = EXPORT_ROOT / "weapons"
PRESENTATION_SOURCE = EXPORT_ROOT / "presentation"
EFFECT_SOURCE = EXPORT_ROOT / "effects/shellcasing"
VIEWMODEL_DESTINATION = UNITY_ROOT / "FirstPerson"
WORLD_DESTINATION = UNITY_ROOT / "Weapons"
PRESENTATION_DESTINATION = ROOT / "FriendsOfDutyUnity/Assets/Presentation/CoD1"


def copy_model(source: Path, destination: Path, model_name: str) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source / f"{model_name}.fbx", destination / f"{model_name}.fbx")
    textures = source / "Textures"
    if textures.is_dir():
        destination_textures = destination / "Textures"
        destination_textures.mkdir(parents=True, exist_ok=True)
        for texture in textures.iterdir():
            if texture.is_file() and texture.suffix.lower() != ".meta":
                shutil.copy2(texture, destination_textures / texture.name)


raw_manifest = json.loads((VIEWMODEL_SOURCE / "manifest.json").read_text())
weapons = raw_manifest["weapons"] if isinstance(raw_manifest, dict) else raw_manifest

for weapon in weapons:
    combined_name = weapon["name"]
    copy_model(
        VIEWMODEL_SOURCE / combined_name,
        VIEWMODEL_DESTINATION / combined_name,
        combined_name,
    )

    world_name = weapon["definition"]["world"]
    copy_model(
        WORLD_SOURCE / world_name,
        WORLD_DESTINATION / world_name,
        world_name,
    )

unity_manifest = {"weapons": weapons}
(VIEWMODEL_DESTINATION / "weapon_manifest.json").write_text(
    json.dumps(unity_manifest, indent=2) + "\n"
)

shutil.copytree(PRESENTATION_SOURCE, PRESENTATION_DESTINATION, dirs_exist_ok=True)
copy_model(
    EFFECT_SOURCE,
    PRESENTATION_DESTINATION / "Models/shellcasing",
    "shellcasing",
)

print(
    f"Synced {len(weapons)} combined viewmodels, world models, audio, "
    "FX textures, and shell geometry into Unity."
)
