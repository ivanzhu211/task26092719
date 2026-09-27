"""确定性用例：单次 advance 整体跨过一条三层升级链。"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from approval_engine import Engine


def make_org():
    return {
        "departments": [
            {"id": 1, "name": "root", "parent_id": None, "manager_id": 10},
            {"id": 2, "name": "eng", "parent_id": 1, "manager_id": 11},
            {"id": 3, "name": "backend", "parent_id": 2, "manager_id": 12},
            {"id": 4, "name": "team", "parent_id": 3, "manager_id": 13},
        ],
        "employees": [
            {"id": 10, "name": "ceo", "dept_id": 1},
            {"id": 11, "name": "vp", "dept_id": 2},
            {"id": 12, "name": "dir", "dept_id": 3},
            {"id": 13, "name": "lead", "dept_id": 4},
            {"id": 14, "name": "dev", "dept_id": 4},
        ],
    }


class EscalationChainTest(unittest.TestCase):
    def setUp(self):
        self.path = tempfile.mktemp(suffix=".db")
        self.engine = Engine(self.path, make_org())

    def tearDown(self):
        self.engine.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(self.path + suffix)
            except OSError:
                pass

    def test_single_advance_crosses_three_level_chain(self):
        # 提交人 14（team 部门），审批人指定为 13（team 负责人）。
        iid = self.engine.submit(
            0, 14, [{"timeout": 5, "approvers": [{"employee": 13}]}])
        # 中途不调用任何 advance，一次性跨过整条升级链。
        self.engine.advance(1000)

        history = self.engine.history(1000, iid)
        timeouts = [e for e in history if e["type"] == "timeout"]
        creates = [e for e in history
                   if e["type"] == "todo_create"
                   and e["payload"]["reason"] == "escalation"]
        completes = [e for e in history if e["type"] == "complete"]

        # 链：13(截止5) -> 12(截止10) -> 11(截止15) -> 10(截止20) -> 过期
        self.assertEqual([e["t"] for e in timeouts], [5, 10, 15, 20])
        self.assertEqual([e["payload"]["holder_id"] for e in timeouts],
                         [13, 12, 11, 10])
        self.assertEqual([e["t"] for e in creates], [5, 10, 15])
        self.assertEqual([e["payload"]["holder_id"] for e in creates],
                         [12, 11, 10])
        self.assertEqual([e["payload"]["deadline"] for e in creates],
                         [10, 15, 20])
        self.assertEqual(len(completes), 1)
        self.assertEqual(completes[0]["t"], 20)
        self.assertEqual(completes[0]["payload"]["result"], "Expired")
        self.assertEqual(self.engine.instance_status(1000, iid), "Expired")
        # 所有待办均已关闭
        for emp in (10, 11, 12, 13, 14):
            self.assertEqual(self.engine.todo(1000, emp), [])

    def test_same_chain_without_any_advance(self):
        # 从不调用 advance，可观测结果必须一致。
        iid = self.engine.submit(
            0, 14, [{"timeout": 5, "approvers": [{"employee": 13}]}])
        self.engine.todo(1000, 14)  # 任意一个带 t 的调用把时间推到 1000
        history = self.engine.history(1000, iid)
        timeouts = [e for e in history if e["type"] == "timeout"]
        self.assertEqual([e["t"] for e in timeouts], [5, 10, 15, 20])
        self.assertEqual(self.engine.instance_status(1000, iid), "Expired")


if __name__ == "__main__":
    unittest.main()
