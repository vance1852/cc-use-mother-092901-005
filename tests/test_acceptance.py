import unittest

from transport_coordination.acceptance import run


class AcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertFalse(result["submitted_executable"])
        self.assertTrue(result["issued_executable"])
        self.assertEqual("executable", result["issued_status"])
        # 封路生成新版本，行程保持履约中，甲地区段沿用 v1 批准
        self.assertEqual(2, result["closure_version"])
        self.assertEqual("in_progress", result["closure_head_status"])
        self.assertEqual(1, result["seg_a_carried_from_version"])
        self.assertTrue(result["recovered_executable"])
        # 执法核验看到两区段当前有效许可与异常处置链
        self.assertEqual(2, len(result["inspection_segments"]))
        self.assertTrue(all(s["effective"] for s in result["inspection_segments"]))
        self.assertIn("road_closure", result["inspection_event_types"])


if __name__ == "__main__":
    unittest.main()
