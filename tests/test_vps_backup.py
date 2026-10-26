"""Backup boundary tests with synthetic bytes and no Docker/Drive credentials."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import os
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location(
    "forgeflow_vps_backup_test", Path(__file__).resolve().parents[1] / "scripts/vps_backup.py")
backup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(backup)


class BackupTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.key = bytes(range(32))  # Public synthetic test fixture, never a production key.
        self.source = self.root / "source.json"
        self.source.write_text('{"approval":"synthetic"}')
        self.archive = self.root / "backup.ffbackup"

    def make_archive(self):
        metadata = {"schema": 1, "runtime_binding": {"instance_id": "test", "generation": "1"},
                    "directories": {"empty-filestore": {"mode": 0o700, "uid": 0, "gid": 0}}}
        backup.encrypt_archive(self.archive, self.key, [("cases/state.json", self.source)], metadata)

    def config(self):
        return {key: str(self.root / name) for key, name in {
            "project_root": "project", "odoo_state": "runtime/odoo", "runtime_root": "runtime",
            "case_root": "api/cases", "api_data_root": "api/runs", "output_root": "backups",
            "encryption_key_file": "secrets/backup.key",
        }.items()} | {"odoo_container": "forgeflow-odoo",
                     "maintenance_units": ["forgeflow-runtime.service"]}

    def test_encryption_and_verified_staging_roundtrip(self):
        self.make_archive()
        self.assertNotIn(b"synthetic", self.archive.read_bytes())
        destination = self.root / "restored"
        result = backup.verify_archive(self.archive, self.key, destination)
        self.assertEqual(result["runtime_binding"]["generation"], "1")
        self.assertEqual((destination / "cases/state.json").read_bytes(), self.source.read_bytes())
        self.assertTrue((destination / "empty-filestore").is_dir())

    def test_wrong_key_releases_no_staged_files(self):
        self.make_archive()
        destination = self.root / "restored"
        with self.assertRaisesRegex(backup.BackupError, "archive_authentication_failed"):
            backup.verify_archive(self.archive, b"x" * 32, destination)
        self.assertFalse(destination.exists())

    def test_changed_ciphertext_and_truncation_are_rejected(self):
        self.make_archive()
        original = self.archive.read_bytes()
        changed = bytearray(original)
        changed[len(backup.MAGIC) + 30] ^= 1
        for data in (bytes(changed), original[:-1]):
            self.archive.write_bytes(data)
            with self.assertRaisesRegex(backup.BackupError, "archive_authentication_failed"):
                backup.verify_archive(self.archive, self.key)

    def test_restore_never_overwrites_existing_directory(self):
        self.make_archive()
        destination = self.root / "existing"
        destination.mkdir()
        with self.assertRaisesRegex(backup.BackupError, "restore_destination_must_not_exist"):
            backup.verify_archive(self.archive, self.key, destination)

    def test_staging_is_rechecked_before_database_restore(self):
        self.make_archive()
        destination = self.root / "staging"
        backup.verify_archive(self.archive, self.key, destination)
        backup.verify_staging(destination)
        (destination / "cases/state.json").write_bytes(b"changed")
        with self.assertRaisesRegex(backup.BackupError, "staging_content_changed"):
            backup.verify_staging(destination)

    def test_additional_staging_files_are_rejected(self):
        self.make_archive()
        destination = self.root / "staging"
        backup.verify_archive(self.archive, self.key, destination)
        (destination / "unexpected").write_bytes(b"extra")
        with self.assertRaisesRegex(backup.BackupError, "staging_file_set_changed"):
            backup.verify_staging(destination)

    def test_archive_path_traversal_is_rejected(self):
        plain = self.root / "malicious.tar"
        with tarfile.open(plain, "w") as archive:
            info = tarfile.TarInfo("../escape")
            info.size = 3
            archive.addfile(info, io.BytesIO(b"bad"))
        with self.assertRaisesRegex(backup.BackupError, "unsafe_or_duplicate"):
            backup.verify_plaintext(plain)

    def test_archive_manifest_must_match_contents(self):
        plain = self.root / "wrong-manifest.tar"
        with tarfile.open(plain, "w") as archive:
            payload = json.dumps({"schema": 1, "files": {"missing": {}}}).encode()
            info = tarfile.TarInfo("backup-manifest.json")
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
        with self.assertRaisesRegex(backup.BackupError, "archive_manifest_mismatch"):
            backup.verify_plaintext(plain)

    def test_backup_key_cannot_be_in_repository_or_state(self):
        for path in (self.root / "project/key", self.root / "runtime/key"):
            config = self.config() | {"encryption_key_file": str(path)}
            with self.assertRaises(backup.BackupError):
                backup.validate_config(config)

    def test_ssh_private_keys_are_not_backup_content(self):
        config = self.config() | {"secret_files": {"host-key": str(self.root / ".ssh/id_ed25519")}}
        with self.assertRaisesRegex(backup.BackupError, "ssh_keys"):
            backup.validate_config(config)

    def test_raw_postgres_is_excluded_even_when_odoo_is_nested_in_runtime(self):
        config = backup.validate_config(self.config())
        for key in ("odoo_state", "runtime_root", "case_root", "api_data_root"):
            config[key].mkdir(parents=True, exist_ok=True)
        pg = config["odoo_state"] / "postgresql"
        pg.mkdir()
        (pg / "secret-raw-pg").write_bytes(b"never included")
        (config["odoo_state"] / "state.json").write_text("{}")
        dumps = self.root / "dumps"
        dumps.mkdir()
        (dumps / "bench.dump").write_bytes(b"logical test dump")
        files, directories = backup.gather_files(config, dumps)
        names = [name for name, _ in files]
        self.assertIn("odoo/state.json", names)
        self.assertIn("database/bench.dump", names)
        self.assertFalse(any("postgresql" in name or name.startswith("runtime/odoo") for name in names))
        self.assertIn("cases", directories)

    def test_running_writer_prevents_backup(self):
        config = backup.validate_config(self.config())
        with patch.object(backup, "run", side_effect=["loaded", "active"]):
            with self.assertRaisesRegex(backup.BackupError, "writer_service_not_stopped"):
                backup.assert_writers_stopped(config)

    def test_running_odoo_prevents_backup_even_if_api_is_stopped(self):
        config = backup.validate_config(self.config())
        with patch.object(backup, "run", side_effect=["loaded", "inactive", "0", "", '{"Running":true}']):
            with self.assertRaisesRegex(backup.BackupError, "odoo_must_be_stopped"):
                backup.assert_writers_stopped(config)

    def test_well_formed_stopped_container_requires_matching_mount(self):
        config = backup.validate_config(self.config())
        mounts = [{"Type": "bind", "Source": str(config["odoo_state"]),
                   "Destination": "/var/lib/forgeflow"}]
        output = ["loaded", "inactive", "0", "", '{"Running":false}', json.dumps(mounts), "sha256:" + "a" * 64]
        with patch.object(backup, "run", side_effect=output):
            self.assertEqual(backup.assert_writers_stopped(config), "sha256:" + "a" * 64)

    def test_wrong_length_encryption_key_is_rejected(self):
        path = self.root / "key"
        path.write_bytes(b"password")
        os.chmod(path, 0o600)
        with patch.object(backup, "secure_file", return_value=b"password"):
            with self.assertRaisesRegex(backup.BackupError, "32_random_bytes"):
                backup.read_key(path)

    def test_drive_success_requires_remote_size_and_md5(self):
        self.make_archive()
        upload = {"remote": "drive-test", "folder": "ForgeFlow/backups",
                  "config_file": str(self.root / "rclone.conf")}
        synthetic_config = b"[drive-test]\ntype=drive\n"
        with patch.object(backup, "secure_file", return_value=synthetic_config):
            with patch.object(backup, "run", side_effect=["", json.dumps({
                    "Size": self.archive.stat().st_size, "Hashes": {"MD5": "wrong"}})]):
                with self.assertRaisesRegex(backup.BackupError, "drive_upload_not_verified"):
                    backup.upload_drive(self.archive, upload)

    def test_other_remote_types_are_not_reported_as_google_drive(self):
        self.make_archive()
        upload = {"remote": "drive-test", "folder": "ForgeFlow/backups",
                  "config_file": str(self.root / "rclone.conf")}
        with patch.object(backup, "secure_file", return_value=b"[drive-test]\ntype=s3\n"):
            with self.assertRaisesRegex(backup.BackupError, "google_drive_remote_required"):
                backup.upload_drive(self.archive, upload)

    def test_rotated_runtime_cannot_be_mixed_with_old_odoo_backup(self):
        config = backup.validate_config(self.config())
        state = {"instance_id": "old-instance", "generation": "old-generation"}
        runtime = {"repo_root": str(config["project_root"]),
                   "sandbox_root": str(config["runtime_root"]),
                   "case_root": str(config["case_root"]), "data_root": str(config["api_data_root"]),
                   "container": config["odoo_container"]}
        instance = {**state, "phase": "ready", "image_id": "image-id",
                    "volume": str(config["odoo_state"]), "container": config["odoo_container"]}
        backup.check_generation_binding(config, state, "image-id", runtime, instance)
        with self.assertRaisesRegex(backup.BackupError, "configuration_stale"):
            backup.check_generation_binding(config, state, "image-id",
                                            {**runtime, "sandbox_root": "/new-generation"}, instance)
        with self.assertRaisesRegex(backup.BackupError, "generation_mismatch"):
            backup.check_generation_binding(config, state, "image-id", runtime,
                                            {**instance, "generation": "new-generation"})

    def test_restore_container_has_no_network_port_or_initialization_permission(self):
        argv = backup.restore_app_command("isolated", self.root / "restored", "image-id",
                                          {"instance_id": "test", "generation": "g1"})
        self.assertEqual(argv[argv.index("--network") + 1], "none")
        self.assertFalse(any(option in argv for option in ("-p", "--publish", "--publish-all")))
        self.assertFalse(any("ALLOW_INITIALIZE" in option for option in argv))
        self.assertIn("FORGEFLOW_GENERATION=g1", argv)

    def test_existing_container_volume_cannot_be_used_as_restore_target(self):
        path = self.root / "live-odoo"
        path.mkdir()
        with patch.object(backup, "run", side_effect=["existing-container", json.dumps([
                {"Source": str(path), "Destination": "/var/lib/forgeflow"}])]):
            with self.assertRaisesRegex(backup.BackupError, "already_attached"):
                backup.refuse_attached_restore_state(path)

    def image_tar(self, tags=None, unsafe=False):
        path = self.root / "image.tar"
        config = b'{"architecture":"amd64","rootfs":{"type":"layers","diff_ids":[]}}'
        image = "sha256:" + hashlib.sha256(config).hexdigest()
        files = {"config.json": config, "layer/layer.tar": b"synthetic layer bytes",
                 "manifest.json": json.dumps([{"Config": "config.json", "RepoTags": tags,
                                                "Layers": ["layer/layer.tar"]}]).encode()}
        if unsafe:
            files["../escape"] = b"not permitted"
        with tarfile.open(path, "w") as archive:
            for name, payload in files.items():
                info = tarfile.TarInfo(name)
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
        return path, image

    def test_image_archive_requires_exact_config_digest(self):
        path, image = self.image_tar()
        backup.validate_docker_image_tar(path, image)
        with self.assertRaisesRegex(backup.BackupError, "image_config_digest_mismatch"):
            backup.validate_docker_image_tar(path, "sha256:" + "f" * 64)

    def test_image_import_cannot_override_existing_tags(self):
        path, image = self.image_tar(tags=["production:latest"])
        with self.assertRaisesRegex(backup.BackupError, "single_untagged_image"):
            backup.validate_docker_image_tar(path, image)

    def test_unsafe_image_tar_is_rejected_before_docker_load(self):
        path, image = self.image_tar(unsafe=True)
        with self.assertRaisesRegex(backup.BackupError, "unsafe_docker_image_tar"):
            backup.validate_docker_image_tar(path, image)

    def test_encrypted_exports_use_independent_nonces(self):
        self.make_archive()
        first = self.archive.read_bytes()[len(backup.MAGIC):len(backup.MAGIC) + 12]
        second_path = self.root / "another.ffbackup"
        backup.encrypt_archive(second_path, self.key, [("file", self.source)], {"schema": 1})
        second = second_path.read_bytes()[len(backup.MAGIC):len(backup.MAGIC) + 12]
        self.assertNotEqual(first, second)

    def test_compressed_image_envelope_authenticates_and_restores(self):
        backup.encrypt_archive(self.archive, self.key, [("file", self.source)],
                               {"schema": 1, "kind": "docker-image"}, compress=True)
        destination = self.root / "compressed-stage"
        backup.verify_archive(self.archive, self.key, destination)
        self.assertEqual((destination / "file").read_bytes(), self.source.read_bytes())

    def oci_tar(self, tamper_layer=False, tagged=False):
        path = self.root / "oci.tar"
        config, layer = b'{"architecture":"amd64"}', b"original layer"
        config_id = "sha256:" + hashlib.sha256(config).hexdigest()
        layer_id = "sha256:" + hashlib.sha256(layer).hexdigest()
        manifest = json.dumps({"config": {"digest": config_id},
                               "layers": [{"digest": layer_id}]}).encode()
        image = "sha256:" + hashlib.sha256(manifest).hexdigest()
        config_path = "blobs/sha256/" + config_id.split(":")[1]
        layer_path = "blobs/sha256/" + layer_id.split(":")[1]
        descriptor = {"digest": image}
        if tagged:
            descriptor["annotations"] = {"io.containerd.image.name": "production:latest"}
        files = {config_path: config, layer_path: b"changed layer" if tamper_layer else layer,
                 "blobs/sha256/" + image.split(":")[1]: manifest,
                 "manifest.json": json.dumps([{"Config": config_path, "RepoTags": None,
                                                "Layers": [layer_path]}]).encode(),
                 "index.json": json.dumps({"manifests": [descriptor]}).encode()}
        with tarfile.open(path, "w") as archive:
            for name, payload in files.items():
                info = tarfile.TarInfo(name)
                info.size = len(payload)
                archive.addfile(info, io.BytesIO(payload))
        return path, image

    def test_containerd_manifest_image_id_is_supported(self):
        path, image = self.oci_tar()
        backup.validate_docker_image_tar(path, image)

    def test_containerd_layer_digest_must_match(self):
        path, image = self.oci_tar(tamper_layer=True)
        with self.assertRaisesRegex(backup.BackupError, "oci_layer_digest_mismatch"):
            backup.validate_docker_image_tar(path, image)

    def test_oci_index_cannot_import_tags_for_either_image_id_style(self):
        path, image = self.oci_tar(tagged=True)
        config_id = "sha256:" + hashlib.sha256(b'{"architecture":"amd64"}').hexdigest()
        for expected in (image, config_id):
            with self.subTest(image_id=expected):
                with self.assertRaisesRegex(backup.BackupError, "tagged_oci_index_refused"):
                    backup.validate_docker_image_tar(path, expected)

    def test_restore_target_symlink_cannot_redirect_to_live_volume(self):
        live, link = self.root / "live", self.root / "redirect"
        live.mkdir()
        try:
            link.symlink_to(live, target_is_directory=True)
        except OSError:
            self.skipTest("symlink creation unavailable on this host")
        with self.assertRaisesRegex(backup.BackupError, "symlink_or_noncanonical"):
            backup.refuse_attached_restore_state(link)


if __name__ == "__main__":
    unittest.main()
