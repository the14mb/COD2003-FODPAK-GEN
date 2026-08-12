.PHONY: help test run gui selftest detect-cod blender-pin blender-pin-check \
        importer-fetch importer-check payload closure

help:
	@echo "COD2003 fodpak generator"
	@echo ""
	@echo "  make gui                 Launch the exporter GUI"
	@echo "  make run GAME=<dir>      Headless export from a CoD install"
	@echo "  make test                Python test suite"
	@echo "  make selftest            In-payload assertions"
	@echo "                           (SELFTEST_ARGS=--provision also downloads Blender)"
	@echo "  make detect-cod          Report where Call of Duty was found"
	@echo "  make importer-fetch      Download CI-built importer extensions"
	@echo "  make importer-check      Is this host's importer authored-LOD capable?"
	@echo "  make blender-pin-check   Assert the pinned Blender is still published"
	@echo "  make blender-pin VERSION=4.5.1   Re-pin Blender"
	@echo "  make payload             Freeze the standalone app (run this on Windows"
	@echo "                           for a .exe; EXPORTER_TARGET overrides)"
	@echo "  make closure             Regenerate packaging/tools_manifest.txt"

test:
	@python3 -m unittest discover -s tests

gui:
	@python3 exporter/friends_of_duty_exporter.py

# GAME is the Call of Duty install directory (the one containing Main/).
# OUT defaults to ./out; add ZIP=1 to also emit a transportable .fodpak.
run:
	@python3 exporter/friends_of_duty_exporter.py --cli \
		--game-dir "$(GAME)" \
		--output "$(or $(OUT),out)" \
		$(if $(ZIP),--zip "$(or $(OUT),out)/cod2003.fodpak",) \
		$(RUN_ARGS)

selftest:
	@python3 exporter/fod_launcher.py --fod-selftest $(SELFTEST_ARGS)

detect-cod:
	@python3 exporter/cod_autodetect.py

blender-pin:
	@python3 packaging/make_blender_pin.py --version $(VERSION)

# Assert the pinned archives are still published, unchanged, at the pinned
# URLs. If a pinned artifact is pruned upstream, EVERY existing install loses
# the ability to export, not just new ones.
blender-pin-check:
	@python3 packaging/make_blender_pin.py --check

importer-fetch:
	@python3 packaging/fetch_importers.py $(IMPORTER_FETCH_ARGS)

importer-check:
	@python3 exporter/build_importer.py --check --require-lod

# PyInstaller cannot cross-compile: run this on the OS you are targeting.
payload:
	@python3 packaging/build_exporter.py \
		--output "dist/$(or $(EXPORTER_TARGET),windows-x64)" \
		--target $(or $(EXPORTER_TARGET),windows-x64)

closure:
	@python3 packaging/tools_closure.py --write
