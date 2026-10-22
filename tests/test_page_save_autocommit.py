"""Markdown Save can leave Git history and the user's staging untouched."""

from pathlib import Path
from unittest.mock import Mock

import bs4
import pytest

from otterwiki.plugins import hookimpl


def git_state(storage):
    """Snapshot index, HEAD and refs without refreshing the Git index."""
    git_dir = Path(storage.repo.git_dir)
    files = [git_dir / name for name in ("HEAD", "index", "packed-refs")]
    files.extend((git_dir / "refs").rglob("*"))
    return {
        str(filename.relative_to(git_dir)): filename.read_bytes()
        for filename in files
        if filename.is_file()
    }


def save_page(client, content, commit=None):
    data = {"content": content}
    if commit is not None:
        data["commit"] = commit
    response = client.post("/SavePage/save", data=data, follow_redirects=True)
    assert response.status_code == 200
    return response.data.decode()


@pytest.fixture
def repo_manager(monkeypatch):
    import otterwiki.gitstorage

    manager = Mock()
    monkeypatch.setattr(
        otterwiki.gitstorage, "get_repo_manager", lambda: manager
    )
    return manager


@pytest.fixture
def hook_calls():
    from otterwiki.plugins import plugin_manager

    calls = []

    class Recorder:
        @hookimpl
        def repository_changed(self, changed_files):
            calls.append(("repository_changed", changed_files))

        @hookimpl
        def page_saved(self, pagepath, content, author, message):
            calls.append(("page_saved", (pagepath, content, author, message)))

    recorder = Recorder()
    plugin_manager.register(recorder)
    try:
        yield calls
    finally:
        plugin_manager.unregister(recorder)


@pytest.mark.parametrize("existing", [False, True])
@pytest.mark.parametrize("staging", ["clean", "unrelated", "partial"])
def test_save_without_autocommit_preserves_git(
    create_app, test_client, repo_manager, existing, staging
):
    storage = create_app.storage
    if existing:
        storage.store("savepage.md", "# Original\n", message="initial page")
    if staging == "unrelated":
        Path(storage.path, "other.md").write_text("staged unrelated content\n")
        storage.repo.index.add(["other.md"])
    elif staging == "partial":
        target = Path(storage.path, "savepage.md")
        target.write_text("# Staged title\n\noriginal body\n")
        storage.repo.index.add(["savepage.md"])
        target.write_text("# Staged title\n\nunstaged body\n")

    create_app.config["PAGE_SAVE_AUTOCOMMIT"] = False
    create_app.config["GIT_REMOTE_PUSH_ENABLED"] = True
    repo_manager.reset_mock()
    before = git_state(storage)
    content = "# Saved page\n\n**Сохранено** без Git.\n"

    html = save_page(test_client, content)

    assert "saved." in html
    assert "<strong>Сохранено</strong>" in html
    assert storage.load("savepage.md") == content
    response = test_client.get("/SavePage/source?raw")
    assert response.status_code == 200
    assert response.data.decode().startswith(content + "\n[]: # (")
    assert git_state(storage) == before
    repo_manager.auto_push_if_enabled.assert_not_called()


@pytest.mark.parametrize("existing", [False, True])
def test_no_autocommit_uses_filesystem_for_noop(
    create_app, test_client, repo_manager, hook_calls, existing
):
    storage = create_app.storage
    original = "# Original\n"
    if existing:
        storage.store("savepage.md", original, message="initial page")
    create_app.config["PAGE_SAVE_AUTOCOMMIT"] = False
    content = "# Saved page\n\nnew content\n"
    hook_calls.clear()
    repo_manager.reset_mock()

    save_page(test_client, content)
    assert [event for event, _ in hook_calls] == [
        "repository_changed",
        "page_saved",
    ]
    assert hook_calls[0][1] == ["savepage.md"]
    pagepath, saved_content, author, message = hook_calls[1][1]
    assert (pagepath, saved_content, message) == ("SavePage", content, "")
    assert isinstance(author, tuple)
    hook_calls.clear()
    before = git_state(storage)

    html = save_page(test_client, content)

    assert "Nothing changed." in html
    assert hook_calls == []
    assert git_state(storage) == before
    # Returning to the indexed content still changes the working file.
    if existing:
        html = save_page(test_client, original)
        assert "Nothing changed." not in html
        assert storage.load("savepage.md") == original
        assert [event for event, _ in hook_calls] == [
            "repository_changed",
            "page_saved",
        ]
        assert git_state(storage) == before
    repo_manager.auto_push_if_enabled.assert_not_called()


@pytest.mark.parametrize("existing", [False, True])
def test_enabling_autocommit_commits_accumulated_content(
    create_app, test_client, repo_manager, existing
):
    storage = create_app.storage
    if existing:
        storage.store("savepage.md", "# Original\n", message="initial page")
    original_head = storage.repo.head.commit.hexsha
    create_app.config["PAGE_SAVE_AUTOCOMMIT"] = False
    content = "# Saved page\n\naccumulated content\n"
    repo_manager.reset_mock()
    save_page(test_client, content)
    assert storage.repo.head.commit.hexsha == original_head
    repo_manager.auto_push_if_enabled.assert_not_called()

    create_app.config["PAGE_SAVE_AUTOCOMMIT"] = True
    save_page(test_client, content, commit="record accumulated content")

    committed = storage.repo.head.commit
    assert committed.hexsha != original_head
    assert committed.parents[0].hexsha == original_head
    assert committed.message == "record accumulated content"
    assert committed.tree["savepage.md"].data_stream.read().decode() == content
    repo_manager.auto_push_if_enabled.assert_called_once_with()


def test_autocommit_enabled_by_default(
    create_app, test_client, repo_manager, hook_calls
):
    storage = create_app.storage
    assert create_app.config["PAGE_SAVE_AUTOCOMMIT"] is True
    original_head = storage.repo.head.commit.hexsha
    content = "# Saved page\n\ndefault commit behavior\n"

    save_page(test_client, content, commit="normal Save")

    assert storage.repo.head.commit.hexsha != original_head
    assert storage.repo.head.commit.message == "normal Save"
    assert [event for event, _ in hook_calls] == [
        "repository_changed",
        "page_saved",
    ]
    repo_manager.auto_push_if_enabled.assert_called_once_with()
    committed_head = storage.repo.head.commit.hexsha
    hook_calls.clear()
    assert "Nothing changed." in save_page(test_client, content)
    assert storage.repo.head.commit.hexsha == committed_head
    assert hook_calls == []
    repo_manager.auto_push_if_enabled.assert_called_once_with()


@pytest.mark.parametrize("autocommit", [False, True])
@pytest.mark.parametrize(
    "commit_message", ["REQUIRED", "OPTIONAL", "DISABLED"]
)
def test_editor_commit_message_modal(
    create_app, test_client, autocommit, commit_message
):
    create_app.config["PAGE_SAVE_AUTOCOMMIT"] = autocommit
    create_app.config["COMMIT_MESSAGE"] = commit_message

    response = test_client.get("/SavePage/edit")

    assert response.status_code == 200
    soup = bs4.BeautifulSoup(response.data, "html.parser")
    modal_expected = autocommit and commit_message != "DISABLED"
    assert (soup.select_one("#modal-commit") is not None) == modal_expected
    form = soup.select_one("#saveform")
    assert form is not None
    if not modal_expected:
        assert form.select_one("#save-page-btn") is not None
        assert form.select_one('input[name="commit"][required]') is None


def test_draft_stays_in_sqlite_and_save_discards_it(create_app, test_client):
    from otterwiki.models import Drafts
    from otterwiki.server import db

    storage = create_app.storage
    create_app.config["PAGE_SAVE_AUTOCOMMIT"] = False
    before = git_state(storage)
    content = "# Draft page\n\nautosaved content\n"

    response = test_client.post("/SavePage/draft", data={"content": content})

    assert response.status_code == 200
    assert response.json["status"] == "draft saved"
    assert not Path(storage.path, "savepage.md").exists()
    assert git_state(storage) == before
    with create_app.app_context():
        assert db.engine.dialect.name == "sqlite"
        draft = Drafts.query.filter_by(pagepath="SavePage").one()
        assert draft.content == content

    save_page(test_client, content)

    assert storage.load("savepage.md") == content
    assert git_state(storage) == before
    with create_app.app_context():
        assert Drafts.query.filter_by(pagepath="SavePage").count() == 0


def test_no_autocommit_preserves_unsafe_path_checks(
    create_app, test_client, tmp_path
):
    storage = create_app.storage
    outside = tmp_path / "outside"
    outside.mkdir()
    target = outside / "page.md"
    target.write_text("# Same content\n")
    Path(storage.path, "link").symlink_to(outside, target_is_directory=True)
    create_app.config["PAGE_SAVE_AUTOCOMMIT"] = False
    before = git_state(storage)

    response = test_client.post(
        "/Link/Page/save", data={"content": "# Same content\n"}
    )

    assert response.status_code == 403
    assert target.read_text() == "# Same content\n"
    assert git_state(storage) == before
