"""The compose file has to keep up with the config, and has to work without a source checkout."""

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
# Set by the image itself, so the compose file rightly doesn't pass it.
IMAGE_MANAGED = {"READCUE_DATA_DIR"}


def load(name: str) -> dict:
    return yaml.safe_load((ROOT / name).read_text())


def settings_read_by_the_app() -> set[str]:
    source = (ROOT / "src/readcue/config.py").read_text()
    names = set(re.findall(r'"((?:READCUE|OLLAMA|ANTHROPIC|PUSHOVER)_[A-Z_]+)"', source))
    return names - IMAGE_MANAGED


def test_every_setting_the_app_reads_is_passed_through_by_compose():
    passed = set(load("docker-compose.yml")["services"]["readcue"]["environment"])
    assert not settings_read_by_the_app() - passed, "add these to docker-compose.yml's environment block"


def test_every_setting_is_documented():
    docs = (ROOT / "docs/deployment.md").read_text()
    missing = {name for name in settings_read_by_the_app() if name not in docs}
    assert not missing, f"document in docs/deployment.md: {sorted(missing)}"


def test_default_compose_file_needs_no_source_checkout():
    service = load("docker-compose.yml")["services"]["readcue"]
    assert "build" not in service, "a NAS has no Dockerfile next to the compose file"
    assert service["image"].startswith("ghcr.io/owenwright8/readcue:")
    assert "env_file" not in service, "env_file needs a newer Compose and a .env on disk"


def test_build_override_adds_the_build_and_keeps_the_pulled_image_name_free():
    override = load("docker-compose.build.yml")["services"]["readcue"]
    assert override["build"]["context"] == "."
    assert override["image"] == "readcue:local"
    assert not (ROOT / "docker-compose.prebuilt.yml").exists()


def test_hardening_and_safe_defaults_are_in_the_compose_file():
    service = load("docker-compose.yml")["services"]["readcue"]
    assert service["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in service["security_opt"]
    assert "127.0.0.1" in service["ports"][0], "published on localhost unless the user opts in"
    assert service["environment"]["READCUE_BIND"].endswith(":-127.0.0.1}")
    assert "READCUE_UID" in service["user"] and "READCUE_DATA" in service["volumes"][0]
