"""Verify the renamed installed distribution and bundled runtime resources."""
from importlib import metadata, resources
from pathlib import Path
import tomllib

import wayline
from wayline.server import db


def test_installed_wayline_identity_and_console_entrypoint():
    distribution = metadata.distribution("wayline")
    declared = tomllib.loads((Path(__file__).parents[1] / "pyproject.toml").read_text())
    assert distribution.version == wayline.__version__ == declared["project"]["version"] == "0.3.0"
    assert distribution.metadata["Requires-Python"].replace(" ", "") == declared["project"]["requires-python"] == ">=3.12,<3.15"
    assert (Path(__file__).parents[1] / ".python-version").read_text().strip() == "3.13"
    scripts = {entry.name: entry.value for entry in distribution.entry_points if entry.group == "console_scripts"}
    assert scripts == {"wayline": "wayline.cli:main"}
    assert db.DEFAULT_DB == Path.home() / ".local/share/wayline/wayline.db"


def test_installed_resources_cover_ui_and_worker_prompt_execution():
    static = resources.files("wayline.server").joinpath("static")
    html = static.joinpath("index.html").read_text()
    assert "<title>Wayline</title>" in html
    assert 'x-data="waylineApp()"' in html
    assert static.joinpath("vendor/alpine.min.js").is_file()
    prompts = resources.files("wayline.dispatcher.prompts")
    assert "{origin}" in prompts.joinpath("default/bootstrap.md").read_text()
    assert "{intent_id}" in prompts.joinpath("default/explore.md").read_text()


def test_distributed_license_and_upstream_attribution_retain_original_terms():
    package = resources.files("wayline")
    original = (Path(__file__).parents[2] / "LICENSE").read_bytes()
    assert package.joinpath("LICENSE").read_bytes() == original
    attribution = package.joinpath("UPSTREAM.md").read_text()
    assert "https://github.com/oritera/Cairn" in attribution
    assert "leixiao" in attribution
    distribution = metadata.distribution("wayline")
    assert any("https://github.com/Prisma-Labs-Dev/Wayline" in value for value in distribution.metadata.get_all("Project-URL"))
