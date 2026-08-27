from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from src.runtime_state import (
    add_correction_audit,
    load_runtime_state,
    mark_task_channel,
    runtime_status,
    save_monitor_state,
    task_complete,
)


class RuntimeStateTests(unittest.TestCase):
    def test_monitor_and_task_state_survive_reload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            save_monitor_state(
                alerts=[{"created_at": "2026-08-26 10:00:00"}],
                cooldowns={"002179:1d_est": datetime(2026, 8, 26, 10, 0)},
                alert_zones={"002179:1d_est": "low"},
                path=path,
            )
            mark_task_channel("next_day_plan", "2026-08-26", "email", True, path=path)
            mark_task_channel("next_day_plan", "2026-08-26", "pushplus", True, path=path)
            loaded = load_runtime_state(path=path)
            self.assertEqual(len(loaded["alerts"]), 1)
            self.assertEqual(loaded["alert_zones"]["002179:1d_est"], "low")
            self.assertTrue(task_complete("next_day_plan", "2026-08-26", ["email", "pushplus"], path=path))

    def test_correction_audit_never_stores_wrong_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            add_correction_audit("002179", "trade-1", ["price"], "replace", path=path)
            audit = runtime_status(path=path)["correction_audit"][0]
            self.assertEqual(audit["fields_changed"], ["price"])
            self.assertNotIn("old_value", audit)
            self.assertNotIn("new_value", audit)


if __name__ == "__main__":
    unittest.main()
