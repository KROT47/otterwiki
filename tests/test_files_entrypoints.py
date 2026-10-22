"""Exercise Docker bootstrap without users, plugins, ownership or services."""

import ast
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.fixture(params=["entrypoint.sh", "entrypoint-slim.sh"])
def bootstrap(request, tmp_path):
    script = Path(__file__).resolve().parents[1] / "docker" / request.param
    shell = "/bin/sh" if "slim" in request.param else "/bin/bash"
    subprocess.run([shell, "-n", str(script)], check=True)
    # Run the real bootstrap section, stopping before chown, plugins or services.
    marker = "\n# install plugins" if "slim" in request.param else "\nchown -R"
    source, separator, _ = script.read_text().partition(marker)
    assert separator

    parent = tmp_path / "parent"
    (parent / ".git").mkdir(parents=True)
    repository = parent / "wiki 'quoted' \\ folder"
    settings = tmp_path / "settings 'quoted'.cfg"
    git_log = tmp_path / "git.jsonl"
    commands = tmp_path / "bin"
    commands.mkdir()
    (commands / "python3").symlink_to(sys.executable)
    git = commands / "git"
    git.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys\n"
        "with open(os.environ['ENTRYPOINT_GIT_LOG'], 'a') as log:\n"
        "    log.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "if sys.argv[1:2] == ['init']:\n"
        "    pathlib.Path(sys.argv[-1], '.git').mkdir(parents=True)\n"
    )
    git.chmod(0o755)

    def run(mode=None):
        env = {
            "PATH": str(commands) + os.pathsep + os.defpath,
            "PUID": "33",
            "PGID": "33",
            "OTTERWIKI_REPOSITORY": str(repository),
            "OTTERWIKI_SETTINGS": str(settings),
            "ENTRYPOINT_GIT_LOG": str(git_log),
        }
        if mode is not None:
            env["STORAGE_BACKEND"] = mode
        result = subprocess.run(
            [shell, "-c", source],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
        )
        calls = (
            [json.loads(line) for line in git_log.read_text().splitlines()]
            if git_log.exists()
            else []
        )
        return result, calls

    return run, repository, settings, request.param


def test_files_bootstrap_skips_git_and_quotes_generated_path(bootstrap):
    run, repository, settings, _ = bootstrap

    result, calls = run("files")

    assert result.returncode == 0, result.stderr
    assert repository.is_dir()
    assert not (repository / ".git").exists()
    assert calls == []
    assignments = ast.parse(settings.read_text()).body
    config = {
        statement.targets[0].id: ast.literal_eval(statement.value)
        for statement in assignments
    }
    assert config["REPOSITORY"] == str(repository)
    assert config["SQLALCHEMY_DATABASE_URI"] == "sqlite:////app-data/db.sqlite"
    assert len(config["SECRET_KEY"]) == 64


@pytest.mark.parametrize("mode", [None, "git"])
def test_git_bootstrap_keeps_default_initialization(bootstrap, mode):
    run, repository, _, script = bootstrap

    result, calls = run(mode)

    assert result.returncode == 0, result.stderr
    assert calls[0] == ["init", "-b", "main", str(repository)]
    assert (repository / ".git").is_dir()
    assert calls[1:] == (
        [
            ["config", "user.name", "Otterwiki Robot"],
            ["config", "user.email", "noreply@otterwiki"],
        ]
        if "slim" in script
        else []
    )


@pytest.mark.parametrize("mode", ["", "invalid", "FILES"])
def test_invalid_backend_stops_before_bootstrap(bootstrap, mode):
    run, repository, settings, _ = bootstrap

    result, calls = run(mode)

    assert result.returncode != 0
    assert "STORAGE_BACKEND must be git or files" in result.stderr
    assert calls == []
    assert not repository.exists()
    assert not settings.exists()


def test_files_bootstrap_preserves_existing_settings(bootstrap):
    run, _, settings, _ = bootstrap
    existing = "REPOSITORY = '/existing/location'\n"
    settings.write_text(existing)

    result, calls = run("files")

    assert result.returncode == 0, result.stderr
    assert calls == []
    assert settings.read_text() == existing
