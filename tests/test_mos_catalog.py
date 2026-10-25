"""Moscow district court catalog + slug resolver tests."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from case_search.mos.live import (  # noqa: E402
    KNOWN_COURTS, resolve_mos_slug)


class CatalogTests(unittest.TestCase):
    def test_catalog_has_at_least_thirty_unique_slugs(self):
        self.assertGreaterEqual(len(KNOWN_COURTS), 30)
        self.assertEqual(len(KNOWN_COURTS), len(set(KNOWN_COURTS)))

    def test_all_names_are_non_empty_strings(self):
        for slug, name in KNOWN_COURTS.items():
            self.assertIsInstance(slug, str)
            self.assertIsInstance(name, str)
            self.assertTrue(slug.strip())
            self.assertTrue(name.strip())


class ResolveMosSlugTests(unittest.TestCase):
    def test_resolve_by_slug_exact_case(self):
        self.assertEqual(resolve_mos_slug("tverskoj"), "tverskoj")

    def test_resolve_by_slug_case_insensitive(self):
        self.assertEqual(resolve_mos_slug("TVERSKOJ"), "tverskoj")
        self.assertEqual(resolve_mos_slug("Tverskoj"), "tverskoj")

    def test_resolve_by_exact_russian_name(self):
        self.assertEqual(
            resolve_mos_slug("Тверской районный суд"), "tverskoj")

    def test_resolve_by_partial_russian_name_case_insensitive(self):
        self.assertEqual(resolve_mos_slug("тверской"), "tverskoj")
        self.assertEqual(resolve_mos_slug("ХАМОВНИЧЕСКИЙ"), "hamovnichesky")
        self.assertEqual(resolve_mos_slug("хорошевский"), "horoshevskij")

    def test_resolve_returns_none_for_garbage(self):
        self.assertIsNone(resolve_mos_slug(""))
        self.assertIsNone(resolve_mos_slug("   "))
        self.assertIsNone(resolve_mos_slug("абракадабра"))
        self.assertIsNone(resolve_mos_slug("noginsk"))


if __name__ == "__main__":
    unittest.main()
