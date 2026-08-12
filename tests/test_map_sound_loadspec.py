"""`loadspec` mixes map names with load-set names, and conflating them cost
five maps their ambient audio.

The alias filter existed for a real reason: a row whose loadspec is `pavlov`
applies only to Pavlov, and treating it as general is how the CoD1 maps once
ended up on Pavlov's tuning. But `all_mp` is not a map. It is the set every
multiplayer map belongs to, and it is what the shipping CoD1 map scripts'
`ambientPlay()` aliases carry -- so the filter silently discarded the ambient
bed for mp_carentan, mp_chateau, mp_pavlov, mp_railyard and mp_rocket.

mp_arnhem and mp_cassino were unaffected only because United Offensive
re-authors those rows with an empty loadspec, which is why the bug looked
like "two maps have ambience and five do not" rather than an obvious break.
"""

from __future__ import annotations

import sys
import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "tools"))

import import_cod_multiplayer_maps as maps  # noqa: E402

HEADER = (
    "name,sequence,file,vol_min,vol_max,pitch_min,pitch_max,dist_min,"
    "dist_max,channel,type,probability,loop,masterslave,loadspec,subtitle\n"
)


def _archive(directory: Path, rows: str, clips: tuple[str, ...]) -> Path:
    path = directory / "pak_test.pk3"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("soundaliases/iw_sound.csv", HEADER + rows)
        for clip in clips:
            archive.writestr(f"sound/{clip}", b"RIFF____WAVEfmt ")
    return path


class LoadspecTests(unittest.TestCase):
    def _resolve(self, rows: str, alias: str, clips=("ambient/amb.wav",)):
        with TemporaryDirectory() as scratch:
            directory = Path(scratch)
            archive = _archive(directory, rows, clips)
            maps._INDEX_CACHE.clear()
            index = maps.layered_index([archive])
            return maps.resolve_sound_alias(alias, index)

    def test_all_mp_rows_are_general(self):
        """The regression. Without this, five maps ship silent."""
        rows = ("ambient_mp_pavlov,,ambient/amb.wav,0.5,,,,120,,local,"
                "streamed,,looping,,all_mp,\n")
        resolved = self._resolve(rows, "ambient_mp_pavlov")
        self.assertIsNotNone(resolved, "all_mp must not be treated as a map")
        self.assertEqual(resolved["soundName"], "ambient/amb.wav")
        self.assertTrue(resolved["loop"])
        self.assertAlmostEqual(resolved["volume"], 0.5)

    def test_a_map_specific_row_is_still_skipped(self):
        """The original defect must not come back.

        `pavlov` means "only on Pavlov"; accepting it generally is how the
        CoD1 maps once inherited Pavlov's own tuning.
        """
        rows = ("some_alias,,ambient/amb.wav,0.5,,,,120,,local,"
                "streamed,,looping,,pavlov,\n")
        self.assertIsNone(self._resolve(rows, "some_alias"))

    def test_campaign_load_sets_are_still_skipped(self):
        """game_main/game_uo are campaign-wide, not multiplayer.

        Admitting them would let single-player tuning reach multiplayer
        output -- the same mistake as the bug this fixes, reversed.
        """
        for loadspec in ("game_main", "game_uo", "credits", "menu"):
            with self.subTest(loadspec=loadspec):
                rows = (f"some_alias,,ambient/amb.wav,0.5,,,,120,,local,"
                        f"streamed,,looping,,{loadspec},\n")
                self.assertIsNone(self._resolve(rows, "some_alias"))

    def test_an_exclusion_row_is_still_general(self):
        rows = ("some_alias,,ambient/amb.wav,0.5,,,,120,,local,"
                "streamed,,looping,,! Truckride,\n")
        self.assertIsNotNone(self._resolve(rows, "some_alias"))

    def test_an_empty_loadspec_is_general(self):
        """How UO re-authors the rows, and why two maps escaped the bug."""
        rows = ("some_alias,,ambient/amb.wav,0.5,,,,120,,local,"
                "streamed,,looping,,,\n")
        self.assertIsNotNone(self._resolve(rows, "some_alias"))

    def test_every_authored_distance_bank_is_retained(self):
        """The runtime plays only the near bank; the pak carries both.

        Distance banks are not implemented yet but are planned, and the far
        clip is genuinely different audio rather than a quieter copy. It can
        only be extracted while the player's Call of Duty install is in hand,
        so it is captured now.
        """
        rows = (
            "weap_x,1,weapons/near.wav,0.5,,,,10,3500,local,,,looping,,,\n"
            "weap_x,2,weapons/far.wav,0.5,,,,10,8500,local,,,looping,,,\n"
        )
        resolved = self._resolve(rows, "weap_x",
                                 clips=("weapons/near.wav", "weapons/far.wav"))
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved["soundName"], "weapons/near.wav",
                         "the nearest bank must remain the selected cue")
        self.assertEqual([b["soundName"] for b in resolved["banks"]],
                         ["weapons/near.wav", "weapons/far.wav"])

    def test_banks_are_deduplicated_by_clip(self):
        """Several CSVs re-author the same alias; the list must not repeat."""
        row = ("weap_x,1,weapons/near.wav,0.5,,,,10,3500,local,,,"
               "looping,,,\n")
        resolved = self._resolve(row * 3, "weap_x", clips=("weapons/near.wav",))
        self.assertEqual(len(resolved["banks"]), 1)

    def test_the_general_set_is_deliberately_minimal(self):
        self.assertEqual(set(maps.GENERAL_LOADSPECS), {"all_mp"})


if __name__ == "__main__":
    unittest.main()
