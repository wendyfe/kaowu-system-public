import os
import sys
import unittest
from pathlib import Path


APP_DIR = Path(__file__).resolve().parents[1] / "app"
os.chdir(APP_DIR)
sys.path.insert(0, str(APP_DIR))

from main import detect_zone


class DetectZoneTests(unittest.TestCase):
    def test_tree_building_uses_classroom_letter(self):
        self.assertEqual(detect_zone("A201", "树人楼"), "A栋")
        self.assertEqual(detect_zone("f306", "树人楼"), "F栋")

    def test_independent_building_uses_building_name(self):
        self.assertEqual(detect_zone("综103", "综合楼"), "综合楼")
        self.assertEqual(detect_zone("103", "综合楼"), "综合楼")

    def test_missing_building_stays_unassigned(self):
        self.assertIsNone(detect_zone("综103"))


if __name__ == "__main__":
    unittest.main()
