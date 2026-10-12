import unittest
import uuid
from session_storage import storage_paths


class SessionStorageTests(unittest.TestCase):
    def test_sessions_do_not_share_storage(self):
        first = str(uuid.uuid4())
        second = str(uuid.uuid4())
        self.assertEqual(storage_paths(first), storage_paths(first))
        self.assertTrue(set(storage_paths(first)).isdisjoint(storage_paths(second)))

    def test_invalid_path_is_rejected(self):
        with self.assertRaises(ValueError):
            storage_paths("../../other-session")
