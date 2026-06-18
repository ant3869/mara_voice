from __future__ import annotations

import os
from pathlib import Path
import tempfile
import unittest

from mara_env import load_dotenv


class EnvLoadingTests(unittest.TestCase):
    def test_blank_values_do_not_override_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            env_path = Path(tmp_dir) / ".env"
            env_path.write_text(
                "MARA_CAPTURE_DIR=\nMARA_OPENCLAW_MODEL=gemini 3.1 pro\n",
                encoding="utf-8",
            )
            original_capture_dir = os.environ.pop("MARA_CAPTURE_DIR", None)
            original_model = os.environ.pop("MARA_OPENCLAW_MODEL", None)
            try:
                loaded = load_dotenv(env_path)
                self.assertEqual(loaded, 1)
                self.assertNotIn("MARA_CAPTURE_DIR", os.environ)
                self.assertEqual(os.environ["MARA_OPENCLAW_MODEL"], "gemini 3.1 pro")
            finally:
                os.environ.pop("MARA_CAPTURE_DIR", None)
                os.environ.pop("MARA_OPENCLAW_MODEL", None)
                if original_capture_dir is not None:
                    os.environ["MARA_CAPTURE_DIR"] = original_capture_dir
                if original_model is not None:
                    os.environ["MARA_OPENCLAW_MODEL"] = original_model


if __name__ == "__main__":
    unittest.main()
