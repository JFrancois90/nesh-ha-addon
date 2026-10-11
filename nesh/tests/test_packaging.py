"""Checks on the add-on packaging files and house style."""

from pathlib import Path

import yaml

from nesh_agent import __version__
from nesh_agent.config import Options, options_from_dict

ADDON = Path(__file__).resolve().parents[1]
REPO = ADDON.parent


def test_config_yaml_is_consistent():
    cfg = yaml.safe_load((ADDON / "config.yaml").read_text())
    for key in ("name", "version", "slug", "description", "arch"):
        assert key in cfg
    assert cfg["name"] == "Nesh" and cfg["slug"] == "nesh"
    assert str(cfg["version"]) == __version__ == "0.1.0"
    assert sorted(cfg["arch"]) == ["aarch64", "amd64"]
    assert cfg["homeassistant_api"] is True and cfg["init"] is False
    assert "ports" not in cfg and "ingress" not in cfg  # nothing listens
    schema, options = cfg["schema"], cfg["options"]
    assert set(options) <= set(schema)
    for key, typ in schema.items():
        if isinstance(typ, str) and typ.endswith("?"):
            assert key not in options, f"optional {key} must have no default"
    known = set(Options.__dataclass_fields__)
    assert set(schema) <= known
    o = options_from_dict({**options, "home_id": "myhome"})
    assert o.control_enabled is False and o.dry_run is True and o.safety_floor == 14
    assert o.poll_seconds == 60 and o.backfill_days == 30 and o.upload_url == ""


def test_translations_cover_schema():
    tr = yaml.safe_load((ADDON / "translations" / "en.yaml").read_text())["configuration"]
    cfg = yaml.safe_load((ADDON / "config.yaml").read_text())
    assert set(tr) == set(cfg["schema"])


def test_repository_and_docker_files():
    repo = yaml.safe_load((REPO / "repository.yaml").read_text())
    assert repo["name"]
    docker = (ADDON / "Dockerfile").read_text()
    assert "FROM ${BUILD_FROM}" in docker and "base-python:3.12" in docker
    assert (ADDON / "run.sh").read_text().startswith("#!/usr/bin/with-contenv bashio")
    for f in ("DOCS.md", "CHANGELOG.md", "README.md", "requirements.txt"):
        assert (ADDON / f).exists()


def test_no_em_or_en_dashes_anywhere():
    bad = []
    files = [REPO / "repository.yaml"] + [p for p in ADDON.rglob("*") if p.is_file()
                                          and "__pycache__" not in p.parts]
    for p in files:
        try:
            text = p.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if chr(0x2014) in text or chr(0x2013) in text:
            bad.append(str(p))
    assert not bad, bad
