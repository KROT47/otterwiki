"""Files mode reads and writes Markdown without using a containing Git repo."""

import os
from datetime import datetime
from io import BytesIO
from pathlib import Path
import shutil
import subprocess
import sys
import textwrap
from types import SimpleNamespace
from unittest.mock import Mock

from bs4 import BeautifulSoup
from PIL import Image
import pytest
from werkzeug.http import http_date

from otterwiki.gitstorage import (
    GitStorage,
    StorageError,
    StorageNotFound,
    StorageUnsafePath,
)


def git_snapshot(repo):
    git_dir = Path(repo.git_dir)
    paths = [git_dir / name for name in ("HEAD", "index", "packed-refs")]
    paths.extend((git_dir / "refs").rglob("*"))
    return {
        str(path.relative_to(git_dir)): path.read_bytes()
        for path in paths
        if path.is_file()
    }


def file_snapshot(root):
    return {
        str(path.relative_to(root)): (
            path.read_bytes() if path.is_file() else None
        )
        for path in root.rglob("*")
    }


@pytest.fixture
def files_app(create_app):
    """Reuse the app singleton, restoring storage before the next fixture."""
    storage = create_app.storage
    parent_path, parent_repo = storage.path, storage.repo
    staged = Path(parent_path, "staged.md")
    staged.write_text("staged version\n")
    parent_repo.index.add(["staged.md"])
    staged.write_text("unstaged version\n")
    before = git_snapshot(parent_repo)
    root = Path(parent_path, "wiki")
    root.mkdir()
    (root / "home.md").write_text("# Files home\n")
    storage.path = str(root)
    storage.repo = None
    create_app.config.update(
        STORAGE_BACKEND="files",
        REPOSITORY=str(root),
        PAGE_SAVE_AUTOCOMMIT=False,
    )
    try:
        yield SimpleNamespace(
            app=create_app,
            client=create_app.test_client(),
            storage=storage,
            root=root,
        )
    finally:
        storage.path, storage.repo = parent_path, parent_repo
        assert git_snapshot(parent_repo) == before
        assert not (root / ".git").exists()


@pytest.mark.parametrize("preexisting", [False, True])
def test_files_startup_without_git(tmp_path, preexisting):
    parent = GitStorage(tmp_path, initialize=True)
    parent.store("parent.md", "original\n", message="parent commit")
    Path(parent.path, "parent.md").write_text("staged\n")
    parent.repo.index.add(["parent.md"])
    Path(parent.path, "parent.md").write_text("unstaged\n")
    before = git_snapshot(parent.repo)
    root = tmp_path / "wiki"
    root.mkdir()
    if preexisting:
        (root / "home.md").write_text("# Existing files home\n")
        (root / "empty.md").touch()
    settings = tmp_path / "settings.cfg"
    settings.write_text(
        f"REPOSITORY = {str(root)!r}\n"
        "STORAGE_BACKEND = 'files'\n"
        "SECRET_KEY = 'Only a synthetic testing key'\n"
        "TESTING = True\n"
        "MAIL_SUPPRESS_SEND = True\n"
        "GIT_WEB_SERVER = True\n"
        "GIT_REMOTE_PUSH_ENABLED = True\n"
        "GIT_REMOTE_PULL_ENABLED = True\n"
        "PAGE_SAVE_AUTOCOMMIT = True\n"
    )
    env = dict(os.environ)
    env.update(
        OTTERWIKI_SETTINGS=str(settings),
        STORAGE_BACKEND="files",
        GIT_WEB_SERVER="true",
        GIT_REMOTE_PUSH_ENABLED="true",
        GIT_REMOTE_PULL_ENABLED="true",
        PAGE_SAVE_AUTOCOMMIT="true",
        GIT_REMOTE_PUSH_PRIVATE_KEY_FILE="",
        GIT_REMOTE_PULL_PRIVATE_KEY_FILE="",
    )
    script = textwrap.dedent(
        """
        from pathlib import Path
        from unittest.mock import Mock
        import sys

        class NoGitImport:
            def find_spec(self, fullname, path=None, target=None):
                if fullname == 'git' or fullname.startswith('git.'):
                    raise AssertionError('Files startup must not import GitPython')

        sys.meta_path.insert(0, NoGitImport())
        from otterwiki.gitstorage import GitStorage
        forbidden = Mock(side_effect=AssertionError('Git must not be opened'))
        GitStorage._read_repo = forbidden
        from otterwiki.server import app, db, storage, githttpserver, update_app_config
        from otterwiki.models import Preferences
        from otterwiki.repomgmt import get_repo_manager

        assert not storage.supports_git
        assert storage.repo is None
        assert githttpserver is None
        assert get_repo_manager() is None
        with app.app_context():
            for name in ('PAGE_SAVE_AUTOCOMMIT', 'GIT_WEB_SERVER',
                         'GIT_REMOTE_PUSH_ENABLED', 'GIT_REMOTE_PULL_ENABLED'):
                db.session.add(Preferences(name=name, value='true'))
            db.session.add(Preferences(name='STORAGE_BACKEND', value='git'))
            db.session.commit()
        update_app_config()
        assert app.config['STORAGE_BACKEND'] == 'files'
        for name in ('PAGE_SAVE_AUTOCOMMIT', 'GIT_WEB_SERVER',
                     'GIT_REMOTE_PUSH_ENABLED', 'GIT_REMOTE_PULL_ENABLED'):
            assert app.config[name] is False
        client = app.test_client()
        assert client.get('/').status_code == 200
        assert client.get('/-/healthz').status_code == 200
        assert Path(storage.path, 'home.md').is_file()
        assert not Path(storage.path, '.git').exists()
        forbidden.assert_not_called()
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        env=env,
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert git_snapshot(parent.repo) == before
    assert not (root / ".git").exists()
    if preexisting:
        assert (root / "home.md").read_text() == "# Existing files home\n"
        assert (root / "empty.md").read_text() == ""


def test_files_storage_never_opens_or_reloads_git(tmp_path, monkeypatch):
    forbidden = Mock(side_effect=AssertionError("Unexpected Git operation"))
    monkeypatch.setattr(GitStorage, "_read_repo", forbidden)
    storage = GitStorage(tmp_path, initialize=True, git_enabled=False)
    assert not storage.supports_git
    assert storage.repo is None
    assert not (tmp_path / ".git").exists()
    assert storage.update("page.md", "# Files page\n", stage=True)
    assert not storage.update("page.md", "# Files page\n", stage=True)
    (tmp_path / ".git").mkdir()
    reload_marker = tmp_path / ".git" / "RELOAD_GIT"
    reload_marker.write_text("leave untouched")
    assert storage.load("page.md") == "# Files page\n"
    storage._check_reload()
    assert reload_marker.read_text() == "leave untouched"
    assert ".git/RELOAD_GIT" not in storage.list()[0]
    with pytest.raises(StorageNotFound):
        storage.metadata("page.md")
    with pytest.raises(StorageNotFound):
        storage.log("page.md")
    with pytest.raises(StorageNotFound):
        storage.load("page.md", revision="HEAD")
    assert storage.log() == []
    forbidden.assert_not_called()


@pytest.mark.parametrize(
    "method,args",
    [
        ("store", ("new/page.md", "must not be written")),
        ("commit", (["page.md"],)),
        ("rename", ("page.md", "new/renamed.md")),
        ("delete", (["page.md", "empty"],)),
        ("restore", (["page.md"],)),
        ("revert", ("HEAD",)),
        ("gc", ()),
    ],
)
def test_files_rejects_git_mutators_before_writes(tmp_path, method, args):
    storage = GitStorage(tmp_path, git_enabled=False)
    (tmp_path / "page.md").write_text("# Keep this page\n")
    (tmp_path / "empty").mkdir()
    before = file_snapshot(tmp_path)

    with pytest.raises(StorageError):
        getattr(storage, method)(*args)

    assert file_snapshot(tmp_path) == before


@pytest.mark.parametrize("content", ["", "# Existing page\n\n**body**\n"])
def test_files_reads_existing_and_empty_pages(files_app, content):
    (files_app.root / "page.md").write_text(content)
    response = files_app.client.get("/Page")
    assert response.status_code == 200
    html = response.data.decode()
    assert "Not under version control" not in html
    if content:
        assert "<strong>body</strong>" in html
    source = files_app.client.get("/Page/source?raw")
    assert source.status_code == 200
    assert source.data.decode().startswith(content + "\n[]: # (")
    editor = files_app.client.get("/Page?edit")
    assert editor.status_code == 200
    assert "cm_editor" in editor.data.decode()


def test_files_create_save_noop_hooks_and_preview(files_app):
    from otterwiki.plugins import hookimpl, plugin_manager

    calls = []

    class Recorder:
        @hookimpl
        def repository_changed(self, changed_files):
            calls.append(("repository", changed_files))

        @hookimpl
        def page_saved(self, pagepath, content, author, message):
            calls.append(("page", (pagepath, content, message)))

    recorder = Recorder()
    plugin_manager.register(recorder)
    try:
        client = files_app.client
        created = client.post(
            "/-/create",
            data={"pagename": "Parent/Child"},
            follow_redirects=True,
        )
        assert created.status_code == 200
        soup = BeautifulSoup(created.data, "html.parser")
        assert soup.select_one("#modal-commit") is None
        assert soup.select_one("#saveform #save-page-btn") is not None
        assert "inlineAttachmentOptions" not in created.data.decode()
        content = "# Saved child\n\n**files mode**\n"
        preview = client.post(
            "/Parent/Child/preview", data={"content": content}
        )
        assert preview.status_code == 200
        assert "<strong>files mode</strong>" in preview.json["preview_content"]
        assert not (files_app.root / "parent" / "child.md").exists()
        saved = client.post(
            "/Parent/Child/save",
            data={"content": content},
            follow_redirects=True,
        )
        assert saved.status_code == 200
        assert "<strong>files mode</strong>" in saved.data.decode()
        assert (files_app.root / "parent" / "child.md").read_text() == content
        assert calls == [
            ("repository", ["parent/child.md"]),
            ("page", ("Parent/Child", content, "")),
        ]
        calls.clear()
        unchanged = client.post(
            "/Parent/Child/save",
            data={"content": content},
            follow_redirects=True,
        )
        assert unchanged.status_code == 200
        assert "Nothing changed." in unchanged.data.decode()
        assert calls == []
    finally:
        plugin_manager.unregister(recorder)


@pytest.mark.parametrize("legacy_revision", ["", "abcd1234"])
def test_files_drafts_use_sqlite_and_current_file(files_app, legacy_revision):
    from otterwiki.models import Drafts
    from otterwiki.server import db

    current = "# Page\n\nOriginal current content\n"
    draft_content = "# Page\n\nDraft changed content\n"
    (files_app.root / "page.md").write_text(current)
    client = files_app.client
    response = client.post(
        "/Page/draft",
        data={"content": draft_content, "revision": legacy_revision},
    )
    assert response.status_code == 200
    assert response.json["status"] == "draft saved"
    assert (files_app.root / "page.md").read_text() == current
    with files_app.app.app_context():
        assert db.engine.dialect.name == "sqlite"
        draft = Drafts.query.filter_by(pagepath="Page").one()
        assert draft.content == draft_content
        # Include old database drafts made before switching the backend.
        draft.revision = legacy_revision
        db.session.commit()
    overview = client.get("/Page?edit")
    assert overview.status_code == 200
    soup = BeautifulSoup(overview.data, "html.parser")
    assert "Latest saved version" in soup.get_text()
    diff = soup.select_one("#draft-diff")
    assert diff is not None
    assert "Original current content" in diff.get_text()
    assert "Draft changed content" in diff.get_text()
    assert "New page" not in diff.get_text()
    assert soup.select_one("#draft-outdated") is None
    continued = client.post(
        "/Page?edit", data={"draft": "edit"}, follow_redirects=True
    )
    assert continued.status_code == 200
    assert draft_content in continued.data.decode()
    assert (
        "has been changed since the draft was started"
        not in continued.data.decode()
    )
    saved = client.post("/Page/save", data={"content": draft_content})
    assert saved.status_code == 302
    assert (files_app.root / "page.md").read_text() == draft_content
    with files_app.app.app_context():
        assert Drafts.query.filter_by(pagepath="Page").count() == 0


def test_files_search_sidebar_hierarchy_and_mermaid(files_app):
    root, client = files_app.root, files_app.client
    (root / "parent.md").write_text("# Parent heading\n")
    (root / "parent").mkdir()
    (root / "parent" / "child.md").write_text(
        "# Child heading\n\nneedle content\n\n```mermaid\nflowchart LR\nA-->B\n```\n"
    )
    (root / "directory").mkdir()
    (root / "directory" / "nested.md").write_text("# Directory child\n")
    response = client.get("/Parent/Child")
    assert response.status_code == 200
    soup = BeautifulSoup(response.data, "html.parser")
    tree = soup.select_one(".parent-sidebar-menu")
    assert tree is not None
    parent_link = tree.find("a", href="/Parent")
    assert parent_link is not None
    assert (
        parent_link.find_parent("details").find("a", href="/Parent/Child")
        is not None
    )
    assert soup.select_one("pre.mermaid") is not None
    scripts = [
        element.get("src", "") for element in soup.select("script[src]")
    ]
    mermaid_assets = [src for src in scripts if "mermaid@" in src]
    assert mermaid_assets and all(
        src.startswith("/static/") for src in mermaid_assets
    )
    assert client.get(mermaid_assets[0]).status_code == 200
    search = client.post("/-/search", data={"query": "needle"})
    assert search.status_code == 200
    assert 'href="/Parent/Child"' in search.data.decode()
    assert "needle" in search.data.decode()
    assert client.get("/Directory").status_code == 200
    assert 'href="/Directory/Nested"' in client.get("/Directory").data.decode()
    index = client.get("/-/index")
    assert index.status_code == 200
    assert 'href="/Parent/Child"' in index.data.decode()
    preview = client.post(
        "/Parent/Child/preview",
        data={"content": "```mermaid\nflowchart LR\nA-->B\n```"},
    )
    assert preview.status_code == 200
    assert 'class="mermaid"' in preview.json["preview_content"]
    assert preview.json["library_requirements"]["requires_mermaid"]


def test_files_unsupported_routes_and_ui(files_app, admin_client):
    client = admin_client
    (files_app.root / "page.md").write_text("# Page\n")
    (files_app.root / "page").mkdir()
    (files_app.root / "page" / "file.txt").write_text("existing attachment\n")
    routes = {
        "/Page/rename": ("GET", "POST"),
        "/Page/delete": ("GET", "POST"),
        "/Page/attachments": ("GET", "POST"),
        "/Page/attachment/file.txt": ("GET", "POST"),
        "/Page/inline_attachment": ("POST",),
        "/Page/history": ("GET", "POST"),
        "/Page/blame": ("GET",),
        "/Page/diff/abcd/ef01": ("GET", "POST"),
        "/-/revert/abcd": ("GET", "POST"),
        "/-/commit/abcd": ("GET",),
        "/-/changelog": ("GET",),
        "/-/changelog/feed.rss": ("GET",),
        "/-/changelog/feed.atom": ("GET",),
        "/Page/view/abcd": ("GET",),
        "/Page?revision=abcd": ("GET",),
        "/Page?edit&revision=abcd": ("GET",),
        "/Page/edit/abcd": ("GET",),
        "/Page/source/abcd": ("GET",),
        "/Page/a/file.txt/abcd": ("GET",),
        "/.git/info/refs?service=git-upload-pack": ("GET", "POST"),
        "/.git/git-upload-pack": ("POST",),
        "/.git/git-receive-pack": ("POST",),
        "/-/api/v1/pull/abcd": ("GET", "POST"),
        "/-/admin/repository_management": ("GET", "POST"),
        "/-/housekeeping": ("GET", "POST"),
    }
    before = file_snapshot(files_app.root)
    for url, methods in routes.items():
        for method in methods:
            response = (
                client.get(url)
                if method == "GET"
                else client.post(url, data={})
            )
            assert response.status_code == 404, (
                method,
                url,
                response.status_code,
            )
    assert file_snapshot(files_app.root) == before
    page = BeautifulSoup(client.get("/Page").data, "html.parser")
    links = [element.get("href", "") for element in page.select("a[href]")]
    for endpoint in (
        "/history",
        "/blame",
        "/rename",
        "/delete",
        "/attachments",
        "/-/changelog",
    ):
        assert not any(endpoint in href for href in links)
    settings = client.get("/-/settings")
    assert settings.status_code == 200
    assert "Repository Management" not in settings.data.decode()
    assert "Git Repository Access" not in settings.data.decode()
    assert client.get("/Page/a/file.txt").status_code == 200


def test_files_existing_image_thumbnail_and_sitemap_mtime(files_app):
    root, client = files_app.root, files_app.client
    page = root / "page.md"
    page.write_text("# Page\n\n![existing image](Page/image.png)\n")
    (root / "page").mkdir()
    image = root / "page" / "image.png"
    shutil.copyfile(Path(__file__).parent / "test_image.png", image)
    timestamp = 1609502400
    os.utime(page, (timestamp, timestamp))
    os.utime(image, (timestamp, timestamp))

    original = client.get("/Page/image.png")
    assert original.status_code == 200
    assert original.data == image.read_bytes()
    assert original.headers["Last-Modified"] == http_date(timestamp)
    thumbnail = client.get("/Page/image.png?thumbnail=16")
    assert thumbnail.status_code == 200
    assert thumbnail.mimetype == "image/png"
    with Image.open(BytesIO(thumbnail.data)) as resized:
        assert resized.width <= 16 and resized.height <= 16
    assert thumbnail.headers["Last-Modified"] == http_date(timestamp)

    sitemap = client.get("/sitemap.xml")
    assert sitemap.status_code == 200
    xml = BeautifulSoup(sitemap.data, "xml")
    location = next(
        loc for loc in xml.find_all("loc") if loc.text.endswith("/Page")
    )
    assert location.find_next_sibling(
        "lastmod"
    ).text == datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d")
    assert any(loc.text.endswith("/") for loc in xml.find_all("loc"))


@pytest.mark.parametrize("target_kind", ["external", "git"])
def test_files_unsafe_symlink_reads_and_writes(
    files_app, tmp_path, target_kind
):
    root, storage, client = files_app.root, files_app.storage, files_app.client
    if target_kind == "external":
        target = tmp_path / "outside.md"
    else:
        (root / ".git").mkdir()
        target = root / ".git" / "hidden.md"
    target.write_text("# Private target\n\nnever disclose this marker\n")
    original = target.read_bytes()
    alias = root / "alias.md"
    alias.symlink_to(target)
    try:
        with pytest.raises(StorageUnsafePath):
            storage.load("alias.md")
        with pytest.raises(StorageUnsafePath):
            storage.update("alias.md", "overwritten", stage=True)
        if target_kind == "git":
            with pytest.raises(StorageUnsafePath):
                storage.load(".git/hidden.md")
            with pytest.raises(StorageUnsafePath):
                storage.update(".git/hidden.md", "overwritten")
        for url in ("/Alias", "/Alias/source?raw"):
            response = client.get(url)
            assert response.status_code == 404
            assert "never disclose this marker" not in response.data.decode()
        response = client.post("/Alias/save", data={"content": "overwritten"})
        assert response.status_code == 403
        assert target.read_bytes() == original
    finally:
        alias.unlink()
        if target_kind == "git":
            target.unlink()
            target.parent.rmdir()


@pytest.mark.parametrize("denied", [os.R_OK, os.W_OK, os.X_OK])
def test_files_health_checks_filesystem_access(files_app, monkeypatch, denied):
    import otterwiki.helper

    client = files_app.client
    assert client.get("/-/healthz").status_code == 200
    access = otterwiki.helper.os.access
    monkeypatch.setattr(
        otterwiki.helper.os,
        "access",
        lambda target, mode: (
            False
            if str(target) == str(files_app.root) and mode & denied
            else access(target, mode)
        ),
    )
    assert client.get("/-/healthz").status_code == 503


def test_files_health_checks_database(files_app, monkeypatch):
    import otterwiki.helper

    failed_query = Mock(
        all=Mock(side_effect=RuntimeError("database unavailable"))
    )
    monkeypatch.setattr(
        otterwiki.helper, "Preferences", SimpleNamespace(query=failed_query)
    )
    response = files_app.client.get("/-/healthz")
    assert response.status_code == 503
    assert "DB Error" in response.data.decode()


def test_default_backend_keeps_git_enabled(create_app):
    assert create_app.config["STORAGE_BACKEND"] == "git"
    assert create_app.storage.supports_git
    assert create_app.storage.repo is not None
