import json
import tempfile
import unittest
from pathlib import Path

from storage.save_package import MAX_SAVE_PACKAGE_BYTES, store_uploaded_save, validate_save_payload


def valid_payload():
    return {
        "version": "v6",
        "saved_at": "20261029_030000",
        "meta": {"day": 2},
        "engine": {"players": {"1": {"alive": True}}, "roles": {"1": "平民"}},
        "manager": {"agent_memory": {}},
    }


class PortableSaveTests(unittest.TestCase):
    def test_valid_v6_save_is_stored_with_server_generated_name(self):
        raw = json.dumps(valid_payload(), ensure_ascii=False).encode("utf-8")
        with tempfile.TemporaryDirectory() as directory:
            name = store_uploaded_save(raw, directory)
            self.assertTrue(name.startswith("portable_D2_"))
            self.assertTrue(name.endswith(".json"))
            self.assertNotIn("/", name)
            saved = json.loads((Path(directory) / name).read_text(encoding="utf-8"))
            self.assertEqual(saved["version"], "v6")
            self.assertEqual(saved["engine"]["roles"]["1"], "平民")

    def test_rejects_invalid_json_wrong_version_and_missing_state(self):
        cases = [
            (b"{bad", "JSON"),
            (json.dumps({**valid_payload(), "version": "v5"}).encode(), "版本"),
            (json.dumps({"version": "v6", "engine": [], "manager": {}}).encode(), "引擎状态"),
            (json.dumps({"version": "v6", "engine": {}, "manager": {}}).encode(), "引擎状态"),
        ]
        for raw, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(ValueError, message):
                validate_save_payload(raw)

    def test_rejects_credential_fields_and_oversized_upload(self):
        payload = valid_payload()
        payload["manager"]["api_key"] = "must-not-import"
        with self.assertRaisesRegex(ValueError, "凭证"):
            validate_save_payload(json.dumps(payload).encode())
        with self.assertRaisesRegex(ValueError, "16 MiB"):
            validate_save_payload(b"x" * (MAX_SAVE_PACKAGE_BYTES + 1))

    def test_failed_import_does_not_create_partial_file(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(ValueError):
                store_uploaded_save(b'{"version":"v1"}', directory)
            self.assertEqual(list(Path(directory).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
