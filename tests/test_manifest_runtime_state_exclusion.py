"""Synthetic contract test for excluding runtime state from release manifests."""
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import tools.recalc_manifest as recalc


class ManifestRuntimeStateExclusionTests(unittest.TestCase):
    def test_runtime_state_directory_is_not_collected(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "sud_cli").mkdir()
            (root / "sud_cli" / "keep.py").write_text("synthetic", encoding="utf-8")
            alert_dir = root / ".case-search" / "alerts"
            alert_dir.mkdir(parents=True)
            (alert_dir / "synthetic-alert.json").write_text("{}", encoding="utf-8")

            collected = recalc.collect(root, root / "MANIFEST.json")

            relative = {record["path"] for record in collected}
            self.assertEqual(relative, {"sud_cli/keep.py"})


if __name__ == "__main__":
    unittest.main()
