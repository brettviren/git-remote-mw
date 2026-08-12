# Copyright (C) 2026 Brett Viren <brett.viren@gmail.com>
#
# This program is free software; you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation; either version 2 of the License, or (at your option) any later
# version.  See the COPYING file for the full text.
#
# SPDX-License-Identifier: GPL-2.0-or-later
"""A minimal in-memory MediaWiki Action API, enough to exercise git-remote-mw.

Run stand-alone for manual poking:

    python3 tests/fake_mediawiki.py 8000

The wiki starts with a couple of pages.  Only the API bits git-remote-mw uses
are implemented, and list queries deliberately paginate in small batches so
that continuation handling gets exercised.
"""

from __future__ import annotations

import gzip
import json
import re
import sys
import threading
import urllib.parse
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

NAMESPACES = {
    0: {"id": 0, "case": "first-letter", "name": "", "content": True},
    1: {"id": 1, "case": "first-letter", "name": "Talk", "canonical": "Talk"},
    6: {"id": 6, "case": "first-letter", "name": "File", "canonical": "File"},
    14: {
        "id": 14,
        "case": "first-letter",
        "name": "Category",
        "canonical": "Category",
    },
}
NAMESPACE_BY_NAME = {ns["name"]: i for i, ns in NAMESPACES.items() if ns["name"]}
FILE_EXTENSIONS = ["png", "gif", "jpg", "jpeg", "txt"]

TOKEN = "cafebabe+\\"
LOGIN_TOKEN = "deadbeef+\\"


class Wiki:
    """The wiki content, as plain data."""

    def __init__(self) -> None:
        self.pages: dict[str, dict] = {}
        self.files: dict[str, list[dict]] = {}
        self.next_pageid = 1
        self.next_revid = 1
        self.clock = datetime(2020, 1, 1, tzinfo=timezone.utc)
        self.users = {"WikiUser": "s3cret"}
        self.logged_in: set[str] = set()
        self.require_login = False
        self.edits: list[dict] = []
        self.lock = threading.Lock()
        # Bookkeeping the tests use to check how chatty the helper is.
        self.requests: list[dict] = []
        self.connections = 0
        self.gzipped = 0
        # Pretend to be a MediaWiki older than 1.27 when False.
        self.support_allrevisions = True
        # Users whose rights include apihighlimits, and a switch to claim the
        # right without honouring it (a wiki behind a stricter proxy).
        self.bots: set[str] = set()
        self.honour_high_limits = True

    # -- content -----------------------------------------------------------

    def tick(self) -> str:
        self.clock += timedelta(minutes=1)
        return self.clock.strftime("%Y-%m-%dT%H:%M:%SZ")

    @staticmethod
    def normalize(title: str) -> str:
        """MediaWiki title normalization (underscores are spaces).

        Simplified: the real thing also upper-cases the first letter of the
        page name, which we leave alone to keep the tests readable.
        """
        return title.replace("_", " ").strip()

    def edit(
        self,
        title: str,
        text: str,
        user: str = "WikiUser",
        comment: str = "",
    ) -> dict:
        title = self.normalize(title)
        # MediaWiki right trims the text of every revision it stores.
        text = text.rstrip()
        page = self.pages.get(title)
        if page is None:
            page = {"pageid": self.next_pageid, "title": title, "revisions": []}
            self.next_pageid += 1
            self.pages[title] = page
        rev = {
            "revid": self.next_revid,
            "timestamp": self.tick(),
            "user": user,
            "comment": comment,
            "content": text,
        }
        self.next_revid += 1
        page["revisions"].append(rev)
        return rev

    def upload(self, filename: str, content: bytes, comment: str = "") -> dict:
        rev = self.edit(f"File:{filename}", f"Uploaded {filename}", comment=comment)
        self.files.setdefault(filename, []).append(
            {"timestamp": rev["timestamp"], "content": content}
        )
        return rev

    def delete(self, title: str) -> None:
        title = self.normalize(title)
        self.pages.pop(title, None)
        if title.startswith("File:"):
            self.files.pop(title[len("File:") :], None)

    def namespace_of(self, title: str) -> int:
        prefix, sep, _ = title.partition(":")
        if sep and prefix in NAMESPACE_BY_NAME:
            return NAMESPACE_BY_NAME[prefix]
        return 0

    def revision(self, revid: int):
        for page in self.pages.values():
            for rev in page["revisions"]:
                if rev["revid"] == revid:
                    return page, rev
        return None, None

    def page_by_id(self, pageid: int) -> dict | None:
        for page in self.pages.values():
            if page["pageid"] == pageid:
                return page
        return None


def api_error(code: str, info: str) -> dict:
    return {"error": {"code": code, "info": info}}


class Api:
    """Turns request parameters into API answers."""

    # Small on purpose: continuation is a thing we want to test.
    LIST_BATCH = 2
    REV_BATCH = 2

    def __init__(self, wiki: Wiki):
        self.wiki = wiki

    def __call__(self, params: dict[str, str], files: dict[str, bytes]) -> dict:
        action = params.get("action", "help")
        handler = getattr(self, f"do_{action}", None)
        if handler is None:
            return api_error("unknown_action", f"Unrecognized value for action: {action}")
        if action in ("edit", "upload", "delete"):
            if params.get("token") != TOKEN:
                return api_error("badtoken", "Invalid CSRF token.")
            if self.wiki.require_login and not self.wiki.logged_in:
                return api_error("readapidenied", "You need read permission.")
        return handler(params, files)

    # -- meta --------------------------------------------------------------

    def do_login(self, params: dict, files: dict) -> dict:
        name = params.get("lgname", "")
        if params.get("lgtoken") != LOGIN_TOKEN:
            return {"login": {"result": "NeedToken", "token": LOGIN_TOKEN}}
        if self.wiki.users.get(name) != params.get("lgpassword"):
            return {"login": {"result": "Failed", "reason": "Wrong password."}}
        self.wiki.logged_in.add(name)
        return {"login": {"result": "Success", "lgusername": name}}

    def value_limit(self) -> int:
        """How many titles/revids one query may name, as MediaWiki limits it."""
        if self.wiki.honour_high_limits and self.wiki.logged_in & self.wiki.bots:
            return 500
        return 50

    def do_query(self, params: dict, files: dict) -> dict:
        for name in ("titles", "pageids", "revids"):
            if len(params.get(name, "").split("|")) > self.value_limit():
                return api_error(
                    "toomanyvalues",
                    f"Too many values supplied for parameter {name}. "
                    f"The limit is {self.value_limit()}.",
                )

        result: dict = {}
        meta = params.get("meta", "").split("|") if params.get("meta") else []
        if "tokens" in meta:
            kinds = params.get("type", "csrf").split("|")
            tokens = {}
            for kind in kinds:
                tokens[f"{kind}token"] = LOGIN_TOKEN if kind == "login" else TOKEN
            result.setdefault("query", {})["tokens"] = tokens
        if "siteinfo" in meta:
            props = params.get("siprop", "general").split("|")
            query = result.setdefault("query", {})
            if "namespaces" in props:
                query["namespaces"] = {str(i): ns for i, ns in NAMESPACES.items()}
            if "namespacealiases" in props:
                query["namespacealiases"] = [{"id": 6, "alias": "Image"}]
            if "fileextensions" in props:
                query["fileextensions"] = [{"ext": e} for e in FILE_EXTENSIONS]
        if "userinfo" in meta:
            user = next(iter(self.wiki.logged_in), None)
            rights = ["read", "edit"]
            if user in self.wiki.bots:
                rights.append("apihighlimits")
            result.setdefault("query", {})["userinfo"] = {
                "id": 1 if user else 0,
                "name": user or "127.0.0.1",
                "rights": rights,
            }

        list_ = params.get("list")
        if list_ == "allpages":
            result.update(self.list_allpages(params))
        elif list_ == "categorymembers":
            result.update(self.list_categorymembers(params))
        elif list_ == "recentchanges":
            result.update(self.list_recentchanges(params))
        elif list_ == "allrevisions":
            if not self.wiki.support_allrevisions:
                return api_error(
                    "badvalue",
                    'Unrecognized value for parameter "list": allrevisions.',
                )
            result.update(self.list_allrevisions(params))
        elif list_:
            return api_error("unknown_list", f"Unrecognized list: {list_}")

        props = params.get("prop", "").split("|") if params.get("prop") else []
        if "revisions" in props:
            result.update(self.prop_revisions(params))
        if "imageinfo" in props:
            result.update(self.prop_imageinfo(params))
        if "info" in props:
            result.update(self.prop_info(params))
        if "links" in props or "images" in props:
            result.update(self.prop_links(params))
        if not props and not list_ and not meta and "titles" in params:
            result.update(self.titles_only(params))
        return result

    # -- lists -------------------------------------------------------------

    def list_allpages(self, params: dict) -> dict:
        namespace = int(params.get("apnamespace", 0))
        titles = sorted(
            title
            for title in self.wiki.pages
            if self.wiki.namespace_of(title) == namespace
        )
        start = params.get("apcontinue")
        if start:
            titles = [t for t in titles if t >= start]
        batch, rest = titles[: self.LIST_BATCH], titles[self.LIST_BATCH :]
        answer: dict = {
            "query": {
                "allpages": [
                    {"pageid": self.wiki.pages[t]["pageid"], "ns": namespace, "title": t}
                    for t in batch
                ]
            }
        }
        if rest:
            answer["continue"] = {"apcontinue": rest[0], "continue": "-||"}
        return answer

    def list_categorymembers(self, params: dict) -> dict:
        category = params.get("cmtitle", "")
        members = []
        for title, page in sorted(self.wiki.pages.items()):
            text = page["revisions"][-1]["content"] if page["revisions"] else ""
            if f"[[{category}]]" in text:
                members.append(
                    {
                        "pageid": page["pageid"],
                        "ns": self.wiki.namespace_of(title),
                        "title": title,
                    }
                )
        return {"query": {"categorymembers": members}}

    def list_allrevisions(self, params: dict) -> dict:
        """Every revision of every page, oldest first, grouped by page."""
        namespaces = params.get("arvnamespace")
        wanted = (
            {int(ns) for ns in namespaces.split("|")} if namespaces else None
        )
        rvprop = params.get("arvprop", "ids").split("|")
        slots = params.get("arvslots") == "main"

        revisions = []
        for title, page in self.wiki.pages.items():
            if wanted is not None and self.wiki.namespace_of(title) not in wanted:
                continue
            for rev in page["revisions"]:
                revisions.append((rev["timestamp"], rev["revid"], title, rev))
        revisions.sort()
        if params.get("arvdir", "older") == "older":
            revisions.reverse()

        start = params.get("arvcontinue") or params.get("arvstart")
        if start:
            # arvcontinue is "<timestamp>|<revid>", arvstart just a timestamp.
            stamp = start.split("|")[0]
            revisions = [r for r in revisions if r[0] >= stamp]

        batch, rest = revisions[: self.LIST_BATCH], revisions[self.LIST_BATCH :]
        by_page: dict[str, dict] = {}
        for _timestamp, _revid, title, rev in batch:
            entry = by_page.setdefault(
                title,
                {
                    "pageid": self.wiki.pages[title]["pageid"],
                    "ns": self.wiki.namespace_of(title),
                    "title": title,
                    "revisions": [],
                },
            )
            entry["revisions"].append(self._revision_answer(rev, rvprop, slots))
        answer: dict = {"query": {"allrevisions": list(by_page.values())}}
        if rest:
            answer["continue"] = {
                "arvcontinue": f"{rest[0][0]}|{rest[0][1]}",
                "continue": "-||",
            }
        return answer

    def list_recentchanges(self, params: dict) -> dict:
        revisions = [
            (rev["revid"], title)
            for title, page in self.wiki.pages.items()
            for rev in page["revisions"]
        ]
        revisions.sort(reverse=params.get("rcdir", "older") == "older")
        limit = int(params.get("rclimit", 10))
        return {
            "query": {
                "recentchanges": [
                    {"type": "edit", "title": title, "revid": revid}
                    for revid, title in revisions[:limit]
                ]
            }
        }

    # -- page properties ---------------------------------------------------

    def _requested_pages(self, params: dict) -> list[tuple[dict | None, str]]:
        """(page, title) pairs for a titles= or pageids= parameter."""
        pages: list[tuple[dict | None, str]] = []
        if params.get("titles"):
            for title in params["titles"].split("|"):
                title = self.wiki.normalize(title)
                pages.append((self.wiki.pages.get(title), title))
        if params.get("pageids"):
            for raw in params["pageids"].split("|"):
                page = self.wiki.page_by_id(int(raw))
                pages.append((page, page["title"] if page else raw))
        return pages

    def titles_only(self, params: dict) -> dict:
        answer = []
        for page, title in self._requested_pages(params):
            if page is None:
                answer.append({"ns": 0, "title": title, "missing": True})
            else:
                answer.append(
                    {
                        "pageid": page["pageid"],
                        "ns": self.wiki.namespace_of(title),
                        "title": title,
                    }
                )
        return {"query": {"pages": answer}}

    def _revision_answer(self, rev: dict, rvprop: list[str], slots: bool) -> dict:
        answer: dict = {"revid": rev["revid"]}
        if "timestamp" in rvprop:
            answer["timestamp"] = rev["timestamp"]
        if "user" in rvprop:
            answer["user"] = rev["user"]
        if "comment" in rvprop:
            answer["comment"] = rev["comment"]
        if "content" in rvprop:
            if slots:
                answer["slots"] = {
                    "main": {
                        "contentmodel": "wikitext",
                        "contentformat": "text/x-wiki",
                        "content": rev["content"],
                    }
                }
            else:
                answer["content"] = rev["content"]
        return answer

    def prop_revisions(self, params: dict) -> dict:
        rvprop = params.get("rvprop", "ids").split("|")
        slots = params.get("rvslots") == "main"
        answer: dict = {"query": {"pages": []}}

        if params.get("revids"):
            wanted = [int(r) for r in params["revids"].split("|")]
            by_page: dict[str, list[dict]] = {}
            bad = []
            for revid in wanted:
                page, rev = self.wiki.revision(revid)
                if rev is None:
                    bad.append(revid)
                    continue
                by_page.setdefault(page["title"], []).append(rev)
            for title, revs in by_page.items():
                page = self.wiki.pages[title]
                answer["query"]["pages"].append(
                    {
                        "pageid": page["pageid"],
                        "ns": self.wiki.namespace_of(title),
                        "title": title,
                        "revisions": [
                            self._revision_answer(r, rvprop, slots) for r in revs
                        ],
                    }
                )
            if bad:
                answer["query"]["badrevids"] = {
                    str(r): {"revid": r} for r in bad
                }
            return answer

        newer = params.get("rvdir") == "newer"
        startid = int(params.get("rvstartid", 0) or 0)
        rvcontinue = params.get("rvcontinue")
        for page, title in self._requested_pages(params):
            if page is None:
                answer["query"]["pages"].append(
                    {"ns": 0, "title": title, "missing": True}
                )
                continue
            revs = list(page["revisions"])
            if newer:
                revs = [r for r in revs if r["revid"] >= startid]
                if rvcontinue:
                    revs = [r for r in revs if r["revid"] >= int(rvcontinue)]
                batch, rest = revs[: self.REV_BATCH], revs[self.REV_BATCH :]
            else:
                batch, rest = revs[-1:], []
            answer["query"]["pages"].append(
                {
                    "pageid": page["pageid"],
                    "ns": self.wiki.namespace_of(title),
                    "title": title,
                    "revisions": [
                        self._revision_answer(r, rvprop, slots) for r in batch
                    ],
                }
            )
            if rest:
                answer["continue"] = {
                    "rvcontinue": str(rest[0]["revid"]),
                    "continue": "||",
                }
        return answer

    def prop_imageinfo(self, params: dict) -> dict:
        answer: dict = {"query": {"pages": []}}
        iistart = params.get("iistart")
        for page, title in self._requested_pages(params):
            entry: dict = {
                "ns": 6,
                "title": title,
                "pageid": page["pageid"] if page else None,
            }
            filename = title[len("File:") :] if title.startswith("File:") else title
            versions = self.wiki.files.get(filename, [])
            if iistart:
                versions = [v for v in versions if v["timestamp"] == iistart]
            if versions:
                entry["imageinfo"] = [
                    {
                        "timestamp": v["timestamp"],
                        "url": f"{self.base_url}/images/{urllib.parse.quote(filename)}"
                        f"?t={v['timestamp']}",
                    }
                    for v in versions
                ]
            answer["query"]["pages"].append(entry)
        return answer

    def prop_info(self, params: dict) -> dict:
        answer: dict = {"query": {"pages": []}}
        for page, title in self._requested_pages(params):
            if page is None:
                answer["query"]["pages"].append(
                    {"ns": 0, "title": title, "missing": True}
                )
            else:
                answer["query"]["pages"].append(
                    {
                        "pageid": page["pageid"],
                        "ns": self.wiki.namespace_of(title),
                        "title": title,
                        "lastrevid": page["revisions"][-1]["revid"],
                    }
                )
        return answer

    def prop_links(self, params: dict) -> dict:
        """Links and inclusions of media files, taken from the wikitext."""
        answer: dict = {"query": {"pages": []}}
        for page, title in self._requested_pages(params):
            if page is None:
                continue
            text = page["revisions"][-1]["content"] if page["revisions"] else ""
            names = re.findall(r"\[\[(File:[^\]|]+)", text)
            entry = {
                "pageid": page["pageid"],
                "ns": self.wiki.namespace_of(title),
                "title": title,
            }
            if names:
                entry["images"] = [{"ns": 6, "title": n} for n in names]
            answer["query"]["pages"].append(entry)
        return answer

    # -- writes ------------------------------------------------------------

    def do_edit(self, params: dict, files: dict) -> dict:
        title = self.wiki.normalize(params.get("title", ""))
        if not title:
            return api_error("missingparam", "The title parameter must be set.")
        text = params.get("text", "")
        base = params.get("basetimestamp")
        page = self.wiki.pages.get(title)
        if base and page and page["revisions"]:
            if page["revisions"][-1]["timestamp"] > base:
                return api_error(
                    "editconflict", "Edit conflict. Someone else changed the page."
                )
        created = page is None
        rev = self.wiki.edit(
            title, text, user="WikiUser", comment=params.get("summary", "")
        )
        self.wiki.edits.append(
            {"title": title, "text": text, "summary": params.get("summary", "")}
        )
        return {
            "edit": {
                "result": "Success",
                "new" if created else "changed": True,
                "pageid": self.wiki.pages[title]["pageid"],
                "title": title,
                "newrevid": rev["revid"],
                "newtimestamp": rev["timestamp"],
            }
        }

    def do_upload(self, params: dict, files: dict) -> dict:
        filename = params.get("filename", "")
        content = files.get("file")
        if not filename or content is None:
            return api_error("missingparam", "Need filename and file.")
        if filename.rpartition(".")[2].lower() not in FILE_EXTENSIONS:
            return api_error("filetype-banned", "This file type is not allowed.")
        self.wiki.upload(filename, content, params.get("comment", ""))
        return {
            "upload": {
                "result": "Success",
                "filename": filename,
                "imageinfo": {"size": len(content)},
            }
        }

    def do_delete(self, params: dict, files: dict) -> dict:
        title = self.wiki.normalize(params.get("title", ""))
        if title not in self.wiki.pages:
            return api_error("missingtitle", f"The page {title} does not exist.")
        self.wiki.delete(title)
        return {"delete": {"title": title, "reason": params.get("reason", "")}}


def parse_multipart(body: bytes, content_type: str) -> tuple[dict, dict]:
    """Very small multipart/form-data parser, enough for action=upload."""
    match = re.search(r"boundary=([^;]+)", content_type)
    if not match:
        return {}, {}
    boundary = ("--" + match.group(1).strip('"')).encode()
    fields: dict[str, str] = {}
    files: dict[str, bytes] = {}
    for part in body.split(boundary):
        if not part.strip(b"-\r\n"):
            continue
        head, _, data = part.partition(b"\r\n\r\n")
        data = data[:-2] if data.endswith(b"\r\n") else data
        header = head.decode("utf-8", "replace")
        name = re.search(r'name="([^"]*)"', header)
        if not name:
            continue
        if "filename=" in header:
            files[name.group(1)] = data
        else:
            fields[name.group(1)] = data.decode("utf-8")
    return fields, files


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    wiki: Wiki
    script_path = "/w"

    def log_message(self, fmt, *args):  # keep the test output readable
        if self.server.verbose:  # type: ignore[attr-defined]
            sys.stderr.write("fake-mw: " + fmt % args + "\n")

    # -- plumbing ----------------------------------------------------------

    def setup(self):  # one call per TCP connection: count them
        super().setup()
        with self.wiki.lock:
            self.wiki.connections += 1

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        headers = {"Content-Type": content_type}
        if "gzip" in self.headers.get("Accept-Encoding", "") and len(body) > 64:
            body = gzip.compress(body)
            headers["Content-Encoding"] = "gzip"
            with self.wiki.lock:
                self.wiki.gzipped += 1
        self.send_response(status)
        for name, value in headers.items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _api(self, params: dict, files: dict) -> None:
        api = Api(self.wiki)
        api.base_url = f"http://{self.headers.get('Host')}{self.script_path}"
        with self.wiki.lock:
            self.wiki.requests.append(params)
            result = api(params, files)
        body = json.dumps(result).encode("utf-8")
        status = 200
        self._send(status, body, "application/json; charset=utf-8")

    def do_GET(self) -> None:
        url = urllib.parse.urlsplit(self.path)
        params = {
            k: v[-1] for k, v in urllib.parse.parse_qs(url.query, keep_blank_values=True).items()
        }
        if url.path == f"{self.script_path}/api.php":
            self._api(params, {})
        elif url.path.startswith(f"{self.script_path}/images/"):
            name = urllib.parse.unquote(url.path.rsplit("/", 1)[1])
            versions = self.wiki.files.get(name, [])
            wanted = params.get("t")
            for version in versions:
                if wanted is None or version["timestamp"] == wanted:
                    self._send(200, version["content"], "application/octet-stream")
                    return
            self._send(404, b"no such file", "text/plain")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:
        url = urllib.parse.urlsplit(self.path)
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        content_type = self.headers.get("Content-Type", "")
        if content_type.startswith("multipart/form-data"):
            params, files = parse_multipart(body, content_type)
        else:
            params = {
                k: v[-1]
                for k, v in urllib.parse.parse_qs(
                    body.decode("utf-8"), keep_blank_values=True
                ).items()
            }
            files = {}
        params.update(
            {
                k: v[-1]
                for k, v in urllib.parse.parse_qs(
                    url.query, keep_blank_values=True
                ).items()
            }
        )
        if url.path == f"{self.script_path}/api.php":
            self._api(params, files)
        else:
            self._send(404, b"not found", "text/plain")


def serve(wiki: Wiki, port: int = 0, verbose: bool = False):
    """Start the fake wiki in a background thread, return (server, url)."""
    handler = type("BoundHandler", (Handler,), {"wiki": wiki})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    server.verbose = verbose  # type: ignore[attr-defined]
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, bound = server.server_address[:2]
    return server, f"http://{host}:{bound}{Handler.script_path}"


def demo_wiki() -> Wiki:
    wiki = Wiki()
    wiki.edit("Main Page", "Welcome to the wiki.\n", comment="create")
    wiki.edit("Main Page", "Welcome to the *wiki*.\n", comment="emphasis")
    wiki.edit("Sandbox", "Play here.\n", comment="create sandbox")
    wiki.edit("Deep/Page", "A page with a slash.\n", comment="create")
    return wiki


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8000
    wiki = demo_wiki()
    server, url = serve(wiki, port, verbose=True)
    print(f"fake mediawiki API at {url}/api.php", file=sys.stderr)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        server.shutdown()
