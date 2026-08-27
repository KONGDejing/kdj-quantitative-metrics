from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from src.auth import get_write_token, verify_write_token


class AuthTests(unittest.TestCase):
    def test_generated_token_is_required(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "token"
            token = get_write_token(path=path)
            self.assertTrue(token)
            self.assertTrue(verify_write_token(token, path=path))
            self.assertFalse(verify_write_token("wrong", path=path))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
