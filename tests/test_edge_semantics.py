"""边界语义的直接断言：恰在截止时刻、同刻状态变更、并行待办先后超时。"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from approval_engine import Engine, EngineError
from tests.test_randomized import ORG


class EdgeSemanticsTest(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.engine = Engine(self.path, ORG)

    def tearDown(self):
        self.engine.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self.path + suffix)
            except OSError:
                pass

    def test_approve_exactly_at_deadline_is_invalid(self):
        eng = self.engine
        iid = eng.submit(0, 14, [{"timeout": 5,
                                  "approvers": [{"employee": 13}]}])
        # 截止时刻 5 的同意已无效：5 时超时先处理，待办已升级到 12
        with self.assertRaises(EngineError):
            eng.act(5, 13, iid, "approve")
        todos = eng.todo(5, 12)
        self.assertEqual(len(todos), 1)
        self.assertEqual(todos[0]["deadline"], 10)
        # 4 时刻（截止前）同意则有效
        iid2 = eng.submit(10, 14, [{"timeout": 5,
                                    "approvers": [{"employee": 13}]}])
        eng.act(14, 13, iid2, "approve")
        self.assertEqual(eng.instance_status(14, iid2), "Approved")

    def test_status_change_at_same_moment_as_timeout(self):
        eng = self.engine
        iid = eng.submit(0, 14, [{"timeout": 5,
                                  "approvers": [{"employee": 13}]}])
        # t=5：13 的待办先超时升级给 12（截止10），随后 12 变为 OnLeave，
        # 其刚到手的待办当即转给 11，截止不变。
        eng.set_status(5, 12, "OnLeave")
        todos = eng.todo(5, 11)
        self.assertEqual(len(todos), 1)
        self.assertEqual(todos[0]["deadline"], 10)
        history = eng.history(5, iid)
        kinds = [(e["t"], e["type"]) for e in history]
        self.assertIn((5, "timeout"), kinds)
        self.assertIn((5, "auto_transfer"), kinds)
        auto = [e for e in history if e["type"] == "auto_transfer"][0]
        self.assertEqual(auto["payload"]["from"], 12)
        self.assertEqual(auto["payload"]["to"], 11)

    def test_parallel_todos_timeout_in_sequence(self):
        eng = self.engine
        iid = eng.submit(0, 14, [{"timeout": 4,
                                  "approvers": [{"employee": 15}]}])
        eng.act(1, 15, iid, "addsign", target_id=16)  # 16 截止=1+4=5
        eng.advance(100)
        history = eng.history(100, iid)
        timeouts = [e for e in history if e["type"] == "timeout"]
        # 15 的待办截止 4 先超时，16 的加签截止 5 后超时
        self.assertEqual([e["t"] for e in timeouts][:2], [4, 5])
        self.assertEqual([e["payload"]["holder_id"] for e in timeouts][:2],
                         [15, 16])
        # 两条都升级给 13（team 负责人，Active 且无本实例待办冲突：
        # 第一条升级给 13 后，第二条升级时 13 已持有本实例待办，须跳过）
        creates = [e for e in history if e["type"] == "todo_create"
                   and e["payload"]["reason"] == "escalation"]
        self.assertEqual(creates[0]["payload"]["holder_id"], 13)
        self.assertEqual(creates[0]["payload"]["deadline"], 8)
        self.assertNotEqual(creates[1]["payload"]["holder_id"], 13)
        self.assertEqual(creates[1]["payload"]["deadline"], 9)


if __name__ == "__main__":
    unittest.main()
