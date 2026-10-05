"""Do not expose invalid content-addressed bytes during a failed copy."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import reproforge.storage as storage
from reproforge.storage import Store, StorageError, hash_file


class AtomicObjectTests(unittest.TestCase):
    def test_concurrent_publication_validates_without_overwriting(self):
        for content, valid in ((b"original", True), (b"damaged", False)):
            with self.subTest(valid=valid), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                source = root / "source"
                source.write_bytes(b"original")
                store = Store(root / "store")
                def competing_writer(temporary_path, target):
                    target.write_bytes(content)
                    raise FileExistsError("concurrent object")
                with patch("reproforge.storage.os.link", side_effect=competing_writer):
                    if valid:
                        reference = store.put(source)
                        self.assertEqual(store.check(reference), [])
                    else:
                        with self.assertRaisesRegex(StorageError, "Corrupt concurrent"):
                            store.put(source)
                digest, _ = hash_file(source)
                self.assertEqual(store.object_path(digest).read_bytes(), content)
                self.assertEqual(list(store.objects.rglob(".tmp-*")), [])

    def test_publication_io_failure_leaves_no_partial_object(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.write_bytes(b"original")
            store = Store(root / "store")
            with patch("reproforge.storage.os.link", side_effect=OSError("disk failure")):
                with self.assertRaises(StorageError):
                    store.put(source)
            self.assertEqual(list(store.objects.rglob(".tmp-*")), [])
            self.assertFalse(store.object_path(hash_file(source)[0]).exists())

    def test_source_mutation_never_publishes_wrong_object(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.write_bytes(b"original")
            store = Store(root / "store")
            digest, _ = hash_file(source)
            original_hash = storage.hash_file
            published = []
            def hash_then_mutate(path):
                result = original_hash(path)
                if path == source:
                    source.write_bytes(b"changed bytes")
                if path == store.object_path(digest) and path.exists():
                    published.append(path.read_bytes())
                return result
            with patch("reproforge.storage.hash_file", side_effect=hash_then_mutate):
                with self.assertRaises(StorageError):
                    store.put(source)
            self.assertEqual(published, [], "Invalid bytes became visible under a trusted digest")
            self.assertFalse(store.object_path(digest).exists())
            self.assertEqual(list(store.objects.rglob(".tmp-*")), [])

    def test_object_publication_never_uses_replace(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.write_bytes(b"original")
            with patch("reproforge.storage.os.replace", side_effect=AssertionError("Immutable object was overwritten")):
                reference = Store(root / "store").put(source)
            self.assertEqual(reference["size"], 8)
