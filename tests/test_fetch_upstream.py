"""Source integrity checks run without network access or third-party packages."""

import importlib.util
import io
import json
import shutil
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "fetch_upstream.py"
SPEC = importlib.util.spec_from_file_location("fetch_upstream", SCRIPT)
fetch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(fetch)


class FetchUpstreamTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.commit = "a" * 40
        self.archive_root = "example-" + self.commit
        self.archive = self.root / "fixture.tar.gz"
        self.make_archive([(self.archive_root + "/README.md", b"original\n", None)])
        fixture_tree = self.root / "fixture"
        fixture_tree.mkdir()
        (fixture_tree / "README.md").write_bytes(b"original\n")
        self.source = {
            "repository": "https://github.com/example/example",
            "commit": self.commit,
            "source_url": "https://codeload.github.com/example/example/tar.gz/" + self.commit,
            "archive_sha256": fetch.file_sha256(self.archive),
            "tree_sha256": fetch.tree_sha256(fetch.file_manifest(fixture_tree)),
        }

    def make_archive(self, entries):
        with tarfile.open(self.archive, "w:gz") as bundle:
            for name, data, kind in entries:
                member = tarfile.TarInfo(name)
                member.mode = 0o644
                if kind is not None:
                    member.type = kind
                    member.linkname = "../../outside"
                else:
                    member.size = len(data)
                bundle.addfile(member, io.BytesIO(data) if kind is None else None)

    def fetch_fixture(self):
        with patch.object(fetch, "download", side_effect=lambda _, path: shutil.copyfile(self.archive, path)):
            return fetch.fetch_source(self.root, "example", self.source)

    def test_fetch_and_idempotent_verification(self):
        self.assertEqual(self.fetch_fixture(), "fetched")
        with patch.object(fetch, "download", side_effect=AssertionError("unexpected network")):
            self.assertEqual(fetch.fetch_source(self.root, "example", self.source), "reused")
            self.assertEqual(fetch.fetch_source(self.root, "example", self.source, True), "verified")
        metadata = json.loads((self.root / "vendor/example.source.json").read_text())
        self.assertEqual(list(metadata["files"]), ["README.md"])

    def test_changed_source_is_not_overwritten(self):
        self.fetch_fixture()
        readme = self.root / "vendor/example/README.md"
        readme.write_text("local work\n")
        with self.assertRaises(fetch.SourceError):
            self.fetch_fixture()
        self.assertEqual(readme.read_text(), "local work\n")

    def test_platform_metadata_does_not_change_the_manifest(self):
        tree = self.root / "tree"
        (tree / "pkg").mkdir(parents=True)
        (tree / "pkg" / "mod.py").write_text("x = 1\n")
        (tree / ".DS_Store").write_bytes(b"finder metadata")
        (tree / "pkg" / "._mod.py").write_bytes(b"appledouble")
        manifest = fetch.file_manifest(tree)
        self.assertEqual(set(manifest), {"pkg/mod.py"})

    def test_additional_source_file_is_rejected(self):
        self.fetch_fixture()
        (self.root / "vendor/example/unexpected.py").write_text("print('extra')")
        with self.assertRaises(fetch.SourceError):
            fetch.fetch_source(self.root, "example", self.source, True)

    def test_metadata_cannot_redefine_pinned_tree(self):
        self.fetch_fixture()
        path = self.root / "vendor/example.source.json"
        metadata = json.loads(path.read_text())
        (self.root / "vendor/example/README.md").write_text("changed")
        metadata["files"] = fetch.file_manifest(self.root / "vendor/example")
        metadata["tree_sha256"] = fetch.tree_sha256(metadata["files"])
        path.write_text(json.dumps(metadata))
        with self.assertRaises(fetch.SourceError):
            fetch.fetch_source(self.root, "example", self.source, True)

    def test_existing_provenance_must_match(self):
        self.fetch_fixture()
        path = self.root / "vendor/example.source.json"
        metadata = json.loads(path.read_text())
        metadata["commit"] = "b" * 40
        path.write_text(json.dumps(metadata))
        with self.assertRaises(fetch.SourceError):
            self.fetch_fixture()

    def test_manifest_adoption_checks_pinned_archive(self):
        self.fetch_fixture()
        path = self.root / "vendor/example.source.json"
        metadata = json.loads(path.read_text())
        del metadata["files"]
        path.write_text(json.dumps(metadata))
        with self.assertRaises(fetch.SourceError):
            fetch.fetch_source(self.root, "example", self.source, True)
        self.assertEqual(self.fetch_fixture(), "manifest-added")

    def test_archive_digest_mismatch_leaves_no_snapshot(self):
        self.source["archive_sha256"] = "0" * 64
        with self.assertRaises(fetch.SourceError):
            self.fetch_fixture()
        self.assertFalse((self.root / "vendor/example").exists())

    def test_archive_rejects_unsafe_members(self):
        cases = [
            ("../outside", None),
            ("/outside", None),
            (self.archive_root + "/../outside", None),
            (self.archive_root + "/.git/config", None),
            (self.archive_root + "/link", tarfile.SYMTYPE),
            (self.archive_root + "/link", tarfile.LNKTYPE),
            (self.archive_root + "/fifo", tarfile.FIFOTYPE),
            ("wrong-root/README.md", None),
        ]
        for index, (name, kind) in enumerate(cases):
            with self.subTest(name=name, kind=kind):
                self.make_archive([(name, b"x", kind)])
                with self.assertRaises(fetch.SourceError):
                    fetch.safe_extract(self.archive, self.root / f"unsafe-{index}", self.archive_root)

    def test_archive_rejects_duplicate_paths(self):
        self.make_archive([
            (self.archive_root + "/README.md", b"one", None),
            (self.archive_root + "/README.md", b"two", None),
        ])
        with self.assertRaises(fetch.SourceError):
            fetch.safe_extract(self.archive, self.root / "duplicate", self.archive_root)

    def test_lock_requires_commit_url_and_tree_digest(self):
        path = self.root / "lock.json"
        path.write_text(json.dumps({"schema_version": 1, "sources": {"example": self.source}}))
        self.assertEqual(fetch.load_lock(path)["example"], self.source)
        self.source["source_url"] = "https://example.com/unpinned.tar.gz"
        path.write_text(json.dumps({"schema_version": 1, "sources": {"example": self.source}}))
        with self.assertRaises(fetch.SourceError):
            fetch.load_lock(path)


if __name__ == "__main__":
    unittest.main()
