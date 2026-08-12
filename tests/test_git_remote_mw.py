# Copyright (C) 2026 Brett Viren <brett.viren@gmail.com>
#
# This program is free software; you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation; either version 2 of the License, or (at your option) any later
# version.  See the COPYING file for the full text.
#
# SPDX-License-Identifier: GPL-2.0-or-later
"""End-to-end tests: real git talking to a fake MediaWiki through the helper.

    python3 -m unittest discover -s tests -v

Each test spins up tests/fake_mediawiki.py on a loopback port, puts a
git-remote-mw wrapper on PATH, and drives actual `git clone/pull/push'.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_mediawiki import Wiki, demo_wiki, serve  # noqa: E402

HELPER = Path(__file__).resolve().parent.parent / "git-remote-mw"

PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\nIDAT\x78\x9c\x63"
    b"\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


class WikiTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.wiki = self.make_wiki()
        self.server, self.url = serve(self.wiki, verbose=bool(os.environ.get("V")))
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

        self.tmp = Path(tempfile.mkdtemp(prefix="git-remote-mw-test."))
        self.addCleanup(shutil.rmtree, self.tmp, True)

        # git finds the helper by name on PATH; wrap it so that it runs under
        # the interpreter running the tests, whatever the shebang says.
        bindir = self.tmp / "bin"
        bindir.mkdir()
        wrapper = bindir / "git-remote-mw"
        wrapper.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{HELPER}" "$@"\n')
        wrapper.chmod(0o755)

        self.env = dict(os.environ)
        self.env.update(
            PATH=f"{bindir}{os.pathsep}{os.environ['PATH']}",
            HOME=str(self.tmp),
            GIT_CONFIG_NOSYSTEM="1",
            GIT_CONFIG_GLOBAL=str(self.tmp / "gitconfig"),
            GIT_TERMINAL_PROMPT="0",
            GIT_AUTHOR_NAME="Git User",
            GIT_AUTHOR_EMAIL="git@example.com",
            GIT_COMMITTER_NAME="Git User",
            GIT_COMMITTER_EMAIL="git@example.com",
            GIT_AUTHOR_DATE="2021-01-01T00:00:00Z",
            GIT_COMMITTER_DATE="2021-01-01T00:00:00Z",
        )
        (self.tmp / "gitconfig").write_text("[init]\n\tdefaultBranch = master\n")

    def make_wiki(self) -> Wiki:
        return demo_wiki()

    # -- helpers -----------------------------------------------------------

    def git(self, *args: str, cwd: Path | None = None, check: bool = True):
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd or self.tmp),
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if check and proc.returncode != 0:
            self.fail(
                f"git {' '.join(args)} failed ({proc.returncode})\n"
                f"--- stdout ---\n{proc.stdout.decode('utf-8', 'replace')}\n"
                f"--- stderr ---\n{proc.stderr.decode('utf-8', 'replace')}"
            )
        return proc

    def out(self, *args: str, cwd: Path | None = None) -> str:
        return self.git(*args, cwd=cwd).stdout.decode("utf-8").strip()

    def clone(self, name: str = "repo", *config: str) -> Path:
        args = ["clone"]
        for item in config:
            args += ["-c", item]
        args += [f"mw::{self.url}", name]
        self.git(*args)
        return self.tmp / name

    def commit_all(self, repo: Path, message: str) -> None:
        self.git("add", "-A", cwd=repo)
        self.git("commit", "-m", message, cwd=repo)

    def latest(self, title: str) -> str:
        """Wikitext of the newest revision of a page, with a final newline.

        MediaWiki right trims what it stores; adding the newline back makes
        the expectations read like the contents of the file on the git side.
        """
        return self.wiki.pages[title]["revisions"][-1]["content"] + "\n"


class TestClone(WikiTestCase):
    def test_clone_all_pages(self):
        repo = self.clone()
        self.assertEqual(
            sorted(p.name for p in repo.iterdir() if p.name != ".git"),
            ["Deep%2FPage.mw", "Main_Page.mw", "Sandbox.mw"],
        )
        self.assertEqual(
            (repo / "Main_Page.mw").read_text(), "Welcome to the *wiki*.\n"
        )
        self.assertEqual((repo / "Deep%2FPage.mw").read_text(), "A page with a slash.\n")

        # One commit per wiki revision, in revision order.
        subjects = self.out("log", "--format=%s", "--reverse", cwd=repo).splitlines()
        self.assertEqual(
            subjects, ["create", "emphasis", "create sandbox", "create"]
        )
        # The author is the wiki user, the date the wiki timestamp.
        self.assertEqual(self.out("log", "-1", "--format=%an", cwd=repo), "WikiUser")
        self.assertEqual(
            self.out("log", "--format=%ad", "--date=iso-strict", "--reverse", cwd=repo)
            .splitlines()[0],
            "2020-01-01T00:01:00Z",
        )
        # The last imported revision is recorded in a note.
        note = self.out(
            "notes", "--ref=origin/mediawiki", "show", "refs/mediawiki/origin/master",
            cwd=repo,
        )
        self.assertEqual(note, "mediawiki_revision: 4")

    def test_clone_is_idempotent(self):
        repo = self.clone()
        head = self.out("rev-parse", "HEAD", cwd=repo)
        proc = self.git("pull", cwd=repo)
        self.assertEqual(self.out("rev-parse", "HEAD", cwd=repo), head)
        self.assertIn(b"up to date", proc.stdout + proc.stderr)

    def test_clone_quiet(self):
        # option verbosity 0 silences our progress reports.
        proc = self.git("clone", "-q", f"mw::{self.url}", "quiet")
        self.assertNotIn(b"Listing pages", proc.stderr)
        self.assertTrue((self.tmp / "quiet" / "Sandbox.mw").exists())
        # ... but not the interesting warnings.
        proc = self.git(
            "clone", "-q", "-c", "remote.origin.pages=No_Such_Page",
            f"mw::{self.url}", "quiet2", check=False,
        )
        self.assertIn(b"not found on wiki", proc.stderr)

    def test_clone_shallow(self):
        repo = self.clone("repo", "remote.origin.shallow=true")
        # Only the last revision of each page is imported.
        self.assertEqual(len(self.out("log", "--format=%s", cwd=repo).splitlines()), 3)
        self.assertEqual(
            (repo / "Main_Page.mw").read_text(), "Welcome to the *wiki*.\n"
        )

    def test_clone_tracked_pages(self):
        repo = self.clone("repo", "remote.origin.pages=Sandbox Nonexistent_Page")
        self.assertEqual(
            [p.name for p in repo.iterdir() if p.name != ".git"], ["Sandbox.mw"]
        )

    def test_clone_tracked_categories(self):
        self.wiki.edit("Tagged", "In a category.\n[[Category:Tracked]]\n")
        repo = self.clone("repo", "remote.origin.categories=Tracked")
        self.assertEqual(
            [p.name for p in repo.iterdir() if p.name != ".git"], ["Tagged.mw"]
        )

    def test_clone_tracked_namespaces(self):
        self.wiki.edit("Talk:Sandbox", "Let's discuss.\n", comment="discuss")
        repo = self.clone("repo", "remote.origin.namespaces=Talk")
        self.assertEqual(
            [p.name for p in repo.iterdir() if p.name != ".git"], ["Talk:Sandbox.mw"]
        )
        # The namespace id is remembered in the config, not asked for twice.
        self.assertIn(
            "Talk:1",
            self.out(
                "config", "--get-all", "remote.origin.namespaceCache", cwd=repo
            ).splitlines(),
        )

    def test_clone_tracked_main_namespace(self):
        self.wiki.edit("Talk:Sandbox", "Let's discuss.\n", comment="discuss")
        repo = self.clone("repo", "remote.origin.namespaces=(Main)")
        self.assertNotIn(
            "Talk:Sandbox.mw", [p.name for p in repo.iterdir()]
        )
        self.assertIn("Sandbox.mw", [p.name for p in repo.iterdir()])

    def test_clone_unknown_namespace(self):
        proc = self.git(
            "clone",
            "-c",
            "remote.origin.namespaces=Nowhere",
            f"mw::{self.url}",
            "repo",
            check=False,
        )
        self.assertIn(b"No such namespace Nowhere", proc.stderr)

    def test_ls_remote(self):
        answer = self.out("ls-remote", f"mw::{self.url}")
        self.assertIn("refs/heads/master", answer)

    def test_clone_by_rev(self):
        repo = self.clone("repo", "remote.origin.fetchStrategy=by_rev")
        self.assertEqual(
            sorted(p.name for p in repo.iterdir() if p.name != ".git"),
            ["Deep%2FPage.mw", "Main_Page.mw", "Sandbox.mw"],
        )
        self.assertEqual(len(self.out("log", "--format=%s", cwd=repo).splitlines()), 4)

    def test_clone_empty_wiki(self):
        self.wiki.pages.clear()
        proc = self.git("clone", f"mw::{self.url}", "empty", check=False)
        self.assertIn(b"empty MediaWiki", proc.stderr)

    def test_clone_utf8(self):
        self.wiki.edit("Ünïcødé Pàge", "Contenu accentué: ☺\n", user="Ütilisateur")
        repo = self.clone()
        self.assertEqual(
            (repo / "Ünïcødé_Pàge.mw").read_text(encoding="utf-8"),
            "Contenu accentué: ☺\n",
        )
        self.assertEqual(self.out("log", "-1", "--format=%an", cwd=repo), "Ütilisateur")

    def test_clone_empty_page_and_message(self):
        self.wiki.edit("Blank", "<!-- empty page -->\n", comment="")
        repo = self.clone()
        # The placeholder MediaWiki needs for "empty" is stripped again.
        self.assertEqual((repo / "Blank.mw").read_text(), "\n")
        self.assertEqual(
            self.out("log", "-1", "--format=%s", cwd=repo),
            "*Empty MediaWiki Message*",
        )


class TestFetchStrategies(WikiTestCase):
    """How many requests it takes to find out what to fetch."""

    def make_wiki(self) -> Wiki:
        wiki = demo_wiki()
        for i in range(20):
            wiki.edit(f"Page {i}", f"Page number {i}.\n", comment=f"create {i}")
        return wiki

    def requests_matching(self, **match) -> list[dict]:
        return [
            params
            for params in self.wiki.requests
            if all(params.get(k) == v for k, v in match.items())
        ]

    def test_allrevisions_is_the_default(self):
        repo = self.clone()
        self.assertEqual(len(list(repo.glob("*.mw"))), 23)
        # Revisions are listed wiki-wide, never page by page.
        self.assertTrue(self.requests_matching(list="allrevisions"))
        self.assertFalse(
            [
                params
                for params in self.wiki.requests
                if params.get("prop") == "revisions" and "pageids" in params
            ]
        )

    def test_up_to_date_fetch_does_not_list_pages(self):
        repo = self.clone()
        self.wiki.requests.clear()
        self.git("pull", cwd=repo)
        # One resume-point lookup plus the revision listing, and that is all:
        # no page listing, no per-page queries.
        self.assertFalse(self.requests_matching(list="allpages"))
        self.assertLessEqual(len(self.wiki.requests), 4)

    def test_incremental_fetch_resumes_by_timestamp(self):
        repo = self.clone()
        self.wiki.requests.clear()
        self.wiki.edit("Sandbox", "Changed again.\n", comment="later edit")
        self.git("pull", cwd=repo)
        self.assertEqual((repo / "Sandbox.mw").read_text(), "Changed again.\n")
        started = [
            params for params in self.requests_matching(list="allrevisions")
            if "arvstart" in params
        ]
        self.assertTrue(started)
        # Only the new revision is imported, not the whole wiki again.
        self.assertEqual(
            self.out("log", "-1", "--format=%s", cwd=repo), "later edit"
        )

    def test_shallow_with_allrevisions(self):
        repo = self.clone("repo", "remote.origin.shallow=true")
        # One commit per page, the newest revision of each.
        self.assertEqual(
            len(self.out("log", "--format=%s", cwd=repo).splitlines()), 23
        )
        self.assertEqual(
            (repo / "Main_Page.mw").read_text(), "Welcome to the *wiki*.\n"
        )

    def test_namespaces_are_filtered_by_the_wiki(self):
        self.wiki.edit("Talk:Sandbox", "Discussion.\n", comment="talk")
        repo = self.clone("repo", "remote.origin.namespaces=Talk")
        self.assertEqual(
            [p.name for p in repo.iterdir() if p.name != ".git"], ["Talk:Sandbox.mw"]
        )
        self.assertEqual(
            [p["arvnamespace"] for p in self.requests_matching(list="allrevisions")],
            ["1"],
        )

    def test_fallback_when_the_wiki_is_too_old(self):
        self.wiki.support_allrevisions = False
        repo = self.clone()
        self.assertEqual(len(list(repo.glob("*.mw"))), 23)
        self.assertTrue(
            [
                params
                for params in self.wiki.requests
                if params.get("prop") == "revisions" and "pageids" in params
            ],
            "should have fallen back to listing revisions page by page",
        )

    def test_tracked_pages_still_go_page_by_page(self):
        self.clone("repo", "remote.origin.pages=Sandbox")
        self.assertFalse(self.requests_matching(list="allrevisions"))

    def test_connections_are_reused_and_answers_compressed(self):
        self.clone()
        # git runs the helper a couple of times; each run should need one
        # connection, not one per request.
        self.assertGreater(len(self.wiki.requests), 10)
        self.assertLess(self.wiki.connections, 6)
        self.assertGreater(self.wiki.gzipped, 0)


class TestApiLimits(WikiTestCase):
    """How many revisions are asked for per content request."""

    def make_wiki(self) -> Wiki:
        wiki = demo_wiki()
        for i in range(60):
            wiki.edit("Busy Page", f"Revision {i}\n", comment=f"edit {i}")
        wiki.bots = {"WikiUser"}
        return wiki

    def login(self) -> tuple[str, str]:
        return (
            "remote.origin.mwLogin=WikiUser",
            "remote.origin.mwPassword=s3cret",
        )

    def batch_sizes(self) -> list[int]:
        return [
            len(params["revids"].split("|"))
            for params in self.wiki.requests
            if "revids" in params
            and params.get("rvprop", "").startswith("content")
        ]

    def test_anonymous_asks_for_fifty(self):
        repo = self.clone()
        self.assertTrue(self.batch_sizes())
        self.assertTrue(all(n <= 50 for n in self.batch_sizes()))
        self.assertEqual(len(self.out("log", "--format=%s", cwd=repo).splitlines()), 64)

    def test_high_limits_are_used_when_granted(self):
        repo = self.clone("repo", *self.login())
        self.assertTrue(
            any(n > 50 for n in self.batch_sizes()),
            f"batches were {self.batch_sizes()}",
        )
        self.assertEqual(len(self.out("log", "--format=%s", cwd=repo).splitlines()), 64)

    def test_high_limits_refused_by_the_wiki(self):
        # The wiki grants the right but a stricter layer refuses the values.
        self.wiki.honour_high_limits = False
        repo = self.clone("repo", *self.login())
        self.assertTrue(any(n <= 50 for n in self.batch_sizes()))
        self.assertEqual(len(self.out("log", "--format=%s", cwd=repo).splitlines()), 64)


class TestPull(WikiTestCase):
    def test_pull_new_revision(self):
        repo = self.clone()
        self.wiki.edit("Sandbox", "Played here.\n", user="Someone", comment="update")
        self.wiki.edit("Brand New", "Fresh page.\n", comment="new page")
        self.git("pull", cwd=repo)
        self.assertEqual((repo / "Sandbox.mw").read_text(), "Played here.\n")
        self.assertEqual((repo / "Brand_New.mw").read_text(), "Fresh page.\n")
        self.assertEqual(
            self.out("log", "-2", "--format=%s", "--reverse", cwd=repo).splitlines(),
            ["update", "new page"],
        )
        note = self.out(
            "notes", "--ref=origin/mediawiki", "show", "refs/mediawiki/origin/master",
            cwd=repo,
        )
        self.assertEqual(note, "mediawiki_revision: 6")

    def test_pull_by_rev(self):
        repo = self.clone("repo", "remote.origin.fetchStrategy=by_rev")
        self.wiki.edit("Sandbox", "Played here.\n", comment="update")
        self.git("pull", cwd=repo)
        self.assertEqual((repo / "Sandbox.mw").read_text(), "Played here.\n")


class TestPush(WikiTestCase):
    def test_push_modified_page(self):
        repo = self.clone()
        (repo / "Sandbox.mw").write_text("Rewritten from git.\n")
        self.commit_all(repo, "rewrite the sandbox")
        self.git("push", cwd=repo)
        self.assertEqual(self.latest("Sandbox"), "Rewritten from git.\n")
        self.assertEqual(
            self.wiki.pages["Sandbox"]["revisions"][-1]["comment"],
            "rewrite the sandbox",
        )
        # The pushed revision is recorded, so a fetch has nothing to do.
        proc = self.git("pull", cwd=repo)
        self.assertIn(b"up to date", proc.stdout + proc.stderr)

    def test_push_new_page_and_deletion(self):
        repo = self.clone()
        (repo / "New_From_Git.mw").write_text("Created in git.\n")
        (repo / "Sandbox.mw").unlink()
        self.commit_all(repo, "add one page, remove another")
        self.git("push", cwd=repo)
        self.assertEqual(self.latest("New From Git"), "Created in git.\n")
        # Pages are not deleted, they are blanked with a marker category.
        self.assertEqual(self.latest("Sandbox"), "[[Category:Deleted]]\n")

    def test_push_page_with_slash_and_utf8(self):
        repo = self.clone()
        (repo / "Deep%2FPage.mw").write_text("Slashed and accentué ☺\n")
        self.commit_all(repo, "édit")
        self.git("push", cwd=repo)
        self.assertEqual(self.latest("Deep/Page"), "Slashed and accentué ☺\n")
        self.assertEqual(self.wiki.pages["Deep/Page"]["revisions"][-1]["comment"], "édit")

    def test_push_several_commits_keeps_order(self):
        repo = self.clone()
        for i in range(3):
            (repo / "Sandbox.mw").write_text(f"Step {i}\n")
            self.commit_all(repo, f"step {i}")
        self.git("push", cwd=repo)
        comments = [
            rev["comment"] for rev in self.wiki.pages["Sandbox"]["revisions"][1:]
        ]
        self.assertEqual(comments, ["step 0", "step 1", "step 2"])
        self.assertEqual(self.latest("Sandbox"), "Step 2\n")

    def test_push_empty_page_gets_placeholder(self):
        repo = self.clone()
        (repo / "Empty_Page.mw").write_text("")
        self.commit_all(repo, "empty page")
        self.git("push", cwd=repo)
        self.assertEqual(self.latest("Empty Page"), "<!-- empty page -->\n")

    def test_push_trailing_whitespace_is_trimmed(self):
        repo = self.clone()
        (repo / "Sandbox.mw").write_text("Trailing junk.\n\n\n   \n")
        self.commit_all(repo, "trim me")
        self.git("push", cwd=repo)
        self.assertEqual(self.latest("Sandbox"), "Trailing junk.\n")

    def test_push_rejects_non_master(self):
        repo = self.clone()
        self.git("checkout", "-b", "side", cwd=repo)
        (repo / "Sandbox.mw").write_text("On a branch.\n")
        self.commit_all(repo, "branch edit")
        proc = self.git("push", "origin", "side:refs/heads/side", cwd=repo, check=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(b"only master", proc.stdout + proc.stderr)

    def test_push_rejects_deletion_of_remote_branch(self):
        repo = self.clone()
        proc = self.git(
            "push", "origin", ":refs/heads/master", cwd=repo, check=False
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(b"cannot delete", proc.stdout + proc.stderr)

    def test_push_detects_conflict(self):
        repo = self.clone()
        (repo / "Sandbox.mw").write_text("From git.\n")
        self.commit_all(repo, "git edit")
        # Someone edits the same page on the wiki in the meantime.
        self.wiki.edit("Sandbox", "From the web.\n", comment="web edit")
        proc = self.git("push", cwd=repo, check=False)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(b"non-fast-forward", proc.stdout + proc.stderr)
        self.assertEqual(self.latest("Sandbox"), "From the web.\n")

    def test_push_whole_history_without_clone(self):
        # No wiki metadata at all: the complete history has to be exported,
        # including the very first commit.
        repo = self.tmp / "fresh"
        repo.mkdir()
        self.git("init", cwd=repo)
        self.git("remote", "add", "origin", f"mw::{self.url}", cwd=repo)
        (repo / "Root_Page.mw").write_text("From the root commit.\n")
        self.commit_all(repo, "root commit")
        (repo / "Second_Page.mw").write_text("From the second commit.\n")
        self.commit_all(repo, "second commit")
        self.git("push", "origin", "master:refs/heads/master", cwd=repo)
        self.assertEqual(self.latest("Root Page"), "From the root commit.\n")
        self.assertEqual(self.latest("Second Page"), "From the second commit.\n")

    def test_dumb_push_leaves_metadata_alone(self):
        repo = self.clone("repo", "remote.origin.dumbPush=true")
        before = self.out("rev-parse", "refs/mediawiki/origin/master", cwd=repo)
        (repo / "Sandbox.mw").write_text("Dumb push.\n")
        self.commit_all(repo, "dumb push")
        proc = self.git("push", cwd=repo)
        self.assertEqual(self.latest("Sandbox"), "Dumb push.\n")
        self.assertEqual(
            self.out("rev-parse", "refs/mediawiki/origin/master", cwd=repo), before
        )
        self.assertIn(b"re-imported", proc.stderr)
        # The push has to be re-imported, and it is.
        self.git("pull", "--rebase", cwd=repo)
        self.assertEqual((repo / "Sandbox.mw").read_text(), "Dumb push.\n")


class TestMedia(WikiTestCase):
    def make_wiki(self) -> Wiki:
        wiki = demo_wiki()
        wiki.upload("logo.png", PNG, comment="upload the logo")
        wiki.edit("Illustrated", "See [[File:logo.png]].\n", comment="illustrate")
        return wiki

    def test_media_not_imported_by_default(self):
        repo = self.clone()
        self.assertFalse((repo / "logo.png").exists())
        self.assertFalse((repo / "File:logo.png.mw").exists())

    def test_media_import(self):
        repo = self.clone("repo", "remote.origin.mediaimport=true")
        self.assertEqual((repo / "logo.png").read_bytes(), PNG)
        self.assertEqual(
            (repo / "File:logo.png.mw").read_text(), "Uploaded logo.png\n"
        )

    def test_media_import_of_linked_files_only(self):
        repo = self.clone(
            "repo",
            "remote.origin.mediaimport=true",
            "remote.origin.pages=Illustrated",
        )
        names = sorted(p.name for p in repo.iterdir() if p.name != ".git")
        self.assertEqual(names, ["File:logo.png.mw", "Illustrated.mw", "logo.png"])
        self.assertEqual((repo / "logo.png").read_bytes(), PNG)

    def test_media_export(self):
        repo = self.clone("repo", "remote.origin.mediaimport=true")
        (repo / "logo.png").write_bytes(PNG + b"\x00extra")
        self.commit_all(repo, "new logo")
        self.git("push", cwd=repo)
        self.assertEqual(self.wiki.files["logo.png"][-1]["content"], PNG + b"\x00extra")

    def test_media_delete(self):
        repo = self.clone("repo", "remote.origin.mediaimport=true")
        (repo / "logo.png").unlink()
        self.commit_all(repo, "remove the logo")
        self.git("push", cwd=repo)
        self.assertNotIn("logo.png", self.wiki.files)
        self.assertNotIn("File:logo.png", self.wiki.pages)

    def test_media_export_refuses_banned_extension(self):
        repo = self.clone()
        (repo / "virus.exe").write_bytes(b"MZ")
        self.commit_all(repo, "not allowed")
        proc = self.git("push", cwd=repo)
        self.assertIn(b"not a permitted file", proc.stderr)
        self.assertNotIn("virus.exe", self.wiki.files)

    def test_media_export_disabled(self):
        repo = self.clone("repo", "remote.origin.mediaexport=false")
        (repo / "extra.png").write_bytes(PNG)
        self.commit_all(repo, "media not exported")
        self.git("push", cwd=repo)
        self.assertNotIn("extra.png", self.wiki.files)


class TestLogin(WikiTestCase):
    def make_wiki(self) -> Wiki:
        wiki = demo_wiki()
        wiki.require_login = True
        return wiki

    def test_push_with_login(self):
        repo = self.clone(
            "repo",
            "remote.origin.mwLogin=WikiUser",
            "remote.origin.mwPassword=s3cret",
        )
        (repo / "Sandbox.mw").write_text("Logged in.\n")
        self.commit_all(repo, "authenticated edit")
        proc = self.git("push", cwd=repo)
        self.assertIn(b'Logged in mediawiki user "WikiUser"', proc.stderr)
        self.assertEqual(self.latest("Sandbox"), "Logged in.\n")

    def test_bad_password_is_fatal(self):
        proc = self.git(
            "clone",
            "-c",
            "remote.origin.mwLogin=WikiUser",
            "-c",
            "remote.origin.mwPassword=wrong",
            f"mw::{self.url}",
            "repo",
            check=False,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(b"Failed to log in mediawiki user", proc.stderr)


class TestGateway(WikiTestCase):
    """The wiki <-> plain-git-host gateway of README.org.

    A gateway repository has the wiki as one remote and an ordinary git host
    as another; clones made from the mirror can push to the wiki too, provided
    they get the wiki metadata.
    """

    def gateway(self) -> Path:
        """A repository with the wiki as a remote named "wiki"."""
        repo = self.tmp / "gateway"
        repo.mkdir()
        self.git("init", "-q", cwd=repo)
        self.git("remote", "add", "wiki", f"mw::{self.url}", cwd=repo)
        self.git("pull", "-q", "wiki", "master", cwd=repo)
        return repo

    def mirror(self, repo: Path, name: str = "hub.git") -> Path:
        """A bare repository standing in for GitHub, with the metadata."""
        hub = self.tmp / name
        self.git("init", "-q", "--bare", str(hub))
        self.git("remote", "add", "hub", str(hub), cwd=repo)
        self.push_to_mirror(repo)
        return hub

    def push_to_mirror(self, repo: Path) -> None:
        self.git("push", "-q", "hub", "master", cwd=repo)
        self.git("push", "-q", "hub", "refs/notes/*:refs/notes/*", cwd=repo)
        self.git(
            "push", "-q", "hub",
            "+refs/mediawiki/wiki/master:refs/heads/wiki-state",
            cwd=repo,
        )

    def from_mirror(self, hub: Path, name: str, restore: bool = True) -> Path:
        repo = self.tmp / name
        self.git("clone", "-q", str(hub), str(repo))
        self.git("remote", "add", "wiki", f"mw::{self.url}", cwd=repo)
        if restore:
            self.git("fetch", "-q", "origin", "refs/notes/*:refs/notes/*", cwd=repo)
            self.git(
                "update-ref", "refs/mediawiki/wiki/master",
                "refs/remotes/origin/wiki-state", cwd=repo,
            )
        return repo

    def revision_count(self) -> int:
        return sum(len(page["revisions"]) for page in self.wiki.pages.values())

    def test_mirror_carries_the_metadata(self):
        repo = self.gateway()
        hub = self.mirror(repo)
        refs = self.out("for-each-ref", "--format=%(refname)", cwd=hub).splitlines()
        self.assertIn("refs/heads/master", refs)
        self.assertIn("refs/heads/wiki-state", refs)
        self.assertIn("refs/notes/wiki/mediawiki", refs)

    def test_clone_of_the_mirror_pushes_incrementally(self):
        hub = self.mirror(self.gateway())
        repo = self.from_mirror(hub, "second")
        before = self.revision_count()
        (repo / "Sandbox.mw").write_text("Edited from the mirror clone.\n")
        self.commit_all(repo, "edit from the mirror clone")
        self.git("push", "-q", "wiki", "master:refs/heads/master", cwd=repo)
        self.assertEqual(self.revision_count(), before + 1)
        self.assertEqual(self.latest("Sandbox"), "Edited from the mirror clone.\n")

    def test_clone_of_the_mirror_without_metadata_re_exports(self):
        # The hazard the README warns about: with no wiki metadata, the whole
        # history is exported again, one wiki edit per commit.
        hub = self.mirror(self.gateway())
        repo = self.from_mirror(hub, "naive", restore=False)
        before = self.revision_count()
        proc = self.git("push", "wiki", "master:refs/heads/master", cwd=repo)
        self.assertIn(b"no common ancestor", proc.stderr)
        self.assertGreater(self.revision_count(), before + 1)

    def test_round_trip_through_the_mirror(self):
        repo = self.gateway()
        hub = self.mirror(repo)

        # Somebody clones the mirror and edits there.
        contrib = self.tmp / "contrib"
        self.git("clone", "-q", str(hub), str(contrib))
        (contrib / "Sandbox.mw").write_text("Edited through the mirror.\n")
        self.commit_all(contrib, "mirror contribution")
        self.git("push", "-q", "origin", "master", cwd=contrib)

        # The gateway forwards it to the wiki.
        self.git("pull", "-q", "--no-rebase", "hub", "master", cwd=repo)
        self.git("push", "-q", "wiki", "master:refs/heads/master", cwd=repo)
        self.assertEqual(self.latest("Sandbox"), "Edited through the mirror.\n")

        # An edit made on the wiki goes the other way, and the mirror stays
        # fast-forward: no force needed.
        self.wiki.edit("Main Page", "Edited on the wiki.\n", comment="web edit")
        self.git("pull", "-q", "--rebase", "wiki", "master", cwd=repo)
        self.assertEqual((repo / "Main_Page.mw").read_text(), "Edited on the wiki.\n")
        self.git("push", "-q", "hub", "master", cwd=repo)

        # And no echo: a second round trip has nothing to do.
        before = self.revision_count()
        self.git("pull", "-q", "--rebase", "wiki", "master", cwd=repo)
        self.git("push", "-q", "wiki", "master:refs/heads/master", cwd=repo)
        self.assertEqual(self.revision_count(), before)


class TestHelperProtocol(WikiTestCase):
    """Drive the helper by hand, the way git does."""

    def talk(self, *lines: str, cwd: Path | None = None) -> str:
        proc = subprocess.run(
            [sys.executable, str(HELPER), "origin", self.url],
            input="".join(line + "\n" for line in lines).encode(),
            cwd=str(cwd or self.tmp),
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        return proc.stdout.decode("utf-8")

    def test_capabilities_and_list(self):
        answer = self.talk("capabilities", "list")
        self.assertIn("refspec refs/heads/*:refs/mediawiki/origin/*\n", answer)
        self.assertIn("import\n", answer)
        self.assertIn("push\n", answer)
        self.assertNotIn("no-private-update", answer)
        self.assertIn("? refs/heads/master\n", answer)
        self.assertIn("@refs/heads/master HEAD\n", answer)

    def test_options(self):
        answer = self.talk("option verbosity 0", "option no-such-option yes")
        self.assertEqual(answer.splitlines(), ["ok", "unsupported"])

    def test_usage_error(self):
        proc = subprocess.run(
            [sys.executable, str(HELPER)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(b"git clone mw::", proc.stderr)

    def test_import_stream_is_fast_import_data(self):
        answer = self.talk("capabilities", "import refs/heads/master")
        self.assertIn("commit refs/mediawiki/origin/master\n", answer)
        self.assertIn("mark :1\n", answer)
        self.assertIn("M 644 inline \"Main_Page.mw\"\n", answer)
        self.assertIn("commit refs/notes/origin/mediawiki\n", answer)
        self.assertTrue(answer.endswith("done\n"))


class TestUnits(unittest.TestCase):
    """The pure functions of the helper, loaded as a module."""

    @classmethod
    def setUpClass(cls):
        import importlib.util

        spec = importlib.util.spec_from_loader(
            "git_remote_mw",
            importlib.machinery.SourceFileLoader("git_remote_mw", str(HELPER)),
        )
        cls.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mod)

    def test_title_to_filename(self):
        self.assertEqual(self.mod.smudge_filename("With Space"), "With_Space")
        self.assertEqual(self.mod.smudge_filename("Deep/Page"), "Deep%2FPage")
        self.assertEqual(self.mod.smudge_filename("Ünïcødé"), "Ünïcødé")

    def test_filename_round_trip(self):
        # A git file name survives the trip through a MediaWiki title, even
        # when it holds characters MediaWiki forbids in titles.
        for name in ("Simple", "With_Space", "Deep%2FPage", "A|B[C]{D}", "Ünïcødé"):
            title = self.mod.clean_filename(name)
            self.assertEqual(self.mod.smudge_filename(title), name)

    def test_smudge_truncates_to_name_max(self):
        smudged = self.mod.smudge_filename("é" * 300)
        self.assertLessEqual(len((smudged + ".mw").encode("utf-8")), self.mod.NAME_MAX)

    def test_fast_import_path_quoting(self):
        self.assertEqual(self.mod.fe_escape_path('a"b\\c'), '"a\\"b\\\\c"')

    def test_content_filters(self):
        self.assertEqual(self.mod.mediawiki_clean("  text \n\n", False), "  text\n")
        self.assertEqual(
            self.mod.mediawiki_clean("", True), self.mod.EMPTY_CONTENT
        )
        self.assertEqual(self.mod.mediawiki_clean("", False), "\n")
        self.assertEqual(self.mod.mediawiki_smudge(self.mod.EMPTY_CONTENT), "\n")
        self.assertEqual(self.mod.mediawiki_smudge("<!-- empty page -->"), "\n")
        self.assertEqual(self.mod.mediawiki_smudge("text"), "text\n")

    def test_url_normalization(self):
        self.assertEqual(
            self.mod.normalize_url("http://wiki.example/w"), "http://wiki.example/w"
        )
        self.assertEqual(
            self.mod.normalize_url("mw://wiki.example/w"), "https://wiki.example/w"
        )
        self.assertEqual(
            self.mod.normalize_url("mw+http://wiki.example/w"),
            "http://wiki.example/w",
        )

    def test_api_url(self):
        self.assertEqual(
            self.mod.MediaWiki("http://wiki.example/w/").api_url,
            "http://wiki.example/w/api.php",
        )
        self.assertEqual(
            self.mod.MediaWiki("http://wiki.example/w/api.php").api_url,
            "http://wiki.example/w/api.php",
        )

    def test_result_shapes_of_both_formatversions(self):
        v1 = {"query": {"pages": {"7": {"title": "A", "revisions": [{"*": "text"}]}}}}
        v2 = {
            "query": {
                "pages": [
                    {
                        "title": "A",
                        "revisions": [{"slots": {"main": {"content": "text"}}}],
                    }
                ]
            }
        }
        for result in (v1, v2):
            pages = self.mod.page_list(result)
            self.assertEqual(len(pages), 1)
            self.assertEqual(
                self.mod.revision_content(pages[0]["revisions"][0]), "text"
            )
        self.assertIsNone(self.mod.revision_content({"texthidden": True}))

    def test_continuation_of_both_api_generations(self):
        self.assertEqual(
            self.mod.continuation({"continue": {"rvcontinue": "3", "continue": "||"}}),
            {"rvcontinue": "3", "continue": "||"},
        )
        self.assertEqual(
            self.mod.continuation(
                {"query-continue": {"revisions": {"rvstartid": 42}}}
            ),
            {"rvstartid": 42},
        )
        self.assertIsNone(self.mod.continuation({}))

    def test_timestamp_parsing(self):
        date = self.mod.parse_timestamp("2020-01-01T00:01:00Z")
        self.assertEqual(int(date.timestamp()), 1577836860)


if __name__ == "__main__":
    unittest.main()
