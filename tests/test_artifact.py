"""Tests for scripts/_artifact.py — the shared FR-3 artifact emitter.

WHY: emit_json is the single choke point that stamps a top-level ``schema_version``
onto every pipeline artifact FILE. Two invariants are load-bearing and tested here:

  1. NON-MUTATION: emit_json must NOT mutate the caller's in-memory ``doc``. The
     scorers/contract builders are PURE functions whose unit tests assert on their
     return dicts directly; if the stamp leaked into the in-memory object those
     tests would see an unexpected ``schema_version`` key. The stamp lands only on
     the written FILE (via a shallow-merged copy).

  2. FORMATTING PRESERVED: the file is written with the same ``indent=2`` and
     (by default) ``sort_keys=True`` the writers it replaces used, so the on-disk
     byte formatting is unchanged apart from the added key.

stdlib-only; unittest.
"""

import json
import os
import tempfile
import unittest

from scripts import _artifact
from scripts._artifact import OUTPUT_SCHEMA_VERSION, emit_json


class TestSchemaVersionConstant(unittest.TestCase):
    def test_default_version_is_1_0_0(self):
        """The shared output schema version starts at 1.0.0 (FR-3 §5)."""
        self.assertEqual(OUTPUT_SCHEMA_VERSION, "1.0.0")


class TestDocNotMutated(unittest.TestCase):
    def test_caller_doc_unchanged(self):
        """emit_json must not add schema_version (or anything) to the caller's dict."""
        doc = {"skill": "x", "score": 59.9, "nested": {"a": 1}}
        before = json.loads(json.dumps(doc))  # deep snapshot
        with tempfile.TemporaryDirectory() as d:
            emit_json(doc, os.path.join(d, "out.json"))
        self.assertNotIn("schema_version", doc)
        self.assertEqual(doc, before)

    def test_nested_objects_are_not_deep_copied_but_unmodified(self):
        """The shallow merge only stamps the top level; nested objects are shared
        but never mutated by the emitter."""
        nested = {"a": 1}
        doc = {"nested": nested}
        with tempfile.TemporaryDirectory() as d:
            emit_json(doc, os.path.join(d, "out.json"))
        # nested is the SAME object (shallow copy) and is unchanged.
        self.assertIs(doc["nested"], nested)
        self.assertEqual(nested, {"a": 1})


class TestFileHasSchemaVersion(unittest.TestCase):
    def test_written_file_carries_schema_version(self):
        doc = {"skill": "x", "score": 1}
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "out.json")
            emit_json(doc, path)
            with open(path) as fh:
                loaded = json.load(fh)
        self.assertEqual(loaded["schema_version"], OUTPUT_SCHEMA_VERSION)
        self.assertEqual(loaded["skill"], "x")
        self.assertEqual(loaded["score"], 1)

    def test_explicit_schema_version_override(self):
        doc = {"a": 1}
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "out.json")
            emit_json(doc, path, schema_version="9.9.9")
            with open(path) as fh:
                loaded = json.load(fh)
        self.assertEqual(loaded["schema_version"], "9.9.9")


class TestFormattingPreserved(unittest.TestCase):
    def test_indent_and_sort_keys_default(self):
        """Default formatting == json.dump(..., indent=2, sort_keys=True) with the
        stamp merged in — byte-for-byte."""
        doc = {"b": 2, "a": 1}
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "out.json")
            emit_json(doc, path)
            with open(path) as fh:
                text = fh.read()
        expected = json.dumps(
            {**doc, "schema_version": OUTPUT_SCHEMA_VERSION},
            indent=2, sort_keys=True)
        self.assertEqual(text, expected)
        # sort_keys=True => "a" precedes "b" precedes "schema_version".
        self.assertLess(text.index('"a"'), text.index('"b"'))
        self.assertLess(text.index('"b"'), text.index('"schema_version"'))

    def test_sort_keys_false_preserves_insertion_order(self):
        """Writers that historically dumped WITHOUT sort_keys (e.g. charts_manifest)
        pass sort_keys=False; insertion order is preserved and the stamp lands last."""
        doc = {"set": "detail", "charts": [1, 2]}
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "out.json")
            emit_json(doc, path, sort_keys=False)
            with open(path) as fh:
                text = fh.read()
        expected = json.dumps(
            {**doc, "schema_version": OUTPUT_SCHEMA_VERSION},
            indent=2, sort_keys=False)
        self.assertEqual(text, expected)
        # insertion order: set, charts, then the appended schema_version.
        self.assertLess(text.index('"set"'), text.index('"charts"'))
        self.assertLess(text.index('"charts"'), text.index('"schema_version"'))


class TestRejectsNonDict(unittest.TestCase):
    def test_non_dict_raises_typeerror(self):
        """Every pipeline artifact is a top-level JSON object; a non-dict is a bug."""
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "out.json")
            with self.assertRaises(TypeError):
                emit_json([1, 2, 3], path)
        # and nothing was written
        self.assertFalse(os.path.exists(path))


if __name__ == "__main__":
    unittest.main()
