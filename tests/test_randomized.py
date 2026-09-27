"""随机化一致性测试：同一操作序列用三种方式执行，结果必须完全一致。

方式一：逐秒推进；方式二：从不推进；方式三：随机推进并随机关闭重开数据库。
"""

import os
import random
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from approval_engine import Engine, EngineError

ORG = {
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
        {"id": 14, "name": "dev1", "dept_id": 4},
        {"id": 15, "name": "dev2", "dept_id": 4},
        {"id": 16, "name": "dev3", "dept_id": 4},
        {"id": 17, "name": "dev4", "dept_id": 3},
    ],
}
EMP_IDS = [e["id"] for e in ORG["employees"]]
STATUSES = ["Active", "OnLeave", "Offboarding", "Departed"]


def edge_case_ops():
    """手工构造的边界情形：连环超时、恰在截止时刻动作、
    与超时同一时刻的状态变更、加签后两条并行待办先后超时。"""
    tpl1 = [{"timeout": 5, "approvers": [{"employee": 13}]}]
    tpl2 = [{"timeout": 4, "approvers": [{"employee": 15}]}]
    return [
        ("submit", 0, 14, tpl1),               # 实例1: 13 截止5
        ("act", 5, 13, 1, "approve", None),    # 恰在截止时刻同意 -> 已超时
        ("set_status", 10, 12, "OnLeave"),     # 与 12 的待办超时同一时刻
        ("todo", 10, 11),
        ("submit", 20, 14, tpl2),              # 实例2: 15 截止24
        ("act", 21, 15, 2, "addsign", 16),     # 加签16 截止25，先后超时
        ("todo", 30, 13),
        ("todo", 30, 11),
        ("history", 30, 1),
        ("history", 30, 2),
    ]


def gen_ops(rng, n, t0, iid0):
    ops = []
    t = t0
    iid = iid0
    for _ in range(n):
        t += rng.choice([0, 0, 1, 1, 2, 3, 5, 10])
        kind = rng.choices(
            ["submit", "act", "withdraw", "set_status", "todo", "history"],
            weights=[20, 35, 5, 12, 15, 13])[0]
        if kind == "submit" or iid == 0:
            layers = []
            for _ in range(rng.randint(1, 3)):
                approvers = []
                for _ in range(rng.randint(1, 2)):
                    if rng.random() < 0.5:
                        approvers.append(
                            {"employee": rng.choice(EMP_IDS)})
                    else:
                        approvers.append({"manager": rng.randint(1, 3)})
                layers.append({"timeout": rng.randint(1, 8),
                               "approvers": approvers})
            ops.append(("submit", t, rng.choice(EMP_IDS), layers))
            iid += 1
        elif kind == "act":
            action = rng.choices(
                ["approve", "reject", "transfer", "addsign"],
                weights=[45, 15, 20, 20])[0]
            target = rng.choice(EMP_IDS) if action in (
                "transfer", "addsign") else None
            ops.append(("act", t, rng.choice(EMP_IDS),
                        rng.randint(1, iid), action, target))
        elif kind == "withdraw":
            ops.append(("withdraw", t, rng.choice(EMP_IDS),
                        rng.randint(1, iid)))
        elif kind == "set_status":
            ops.append(("set_status", t, rng.choice(EMP_IDS),
                        rng.choice(STATUSES)))
        elif kind == "todo":
            ops.append(("todo", t, rng.choice(EMP_IDS)))
        else:
            ops.append(("history", t, rng.randint(1, iid)))
    return ops, t


def exec_op(eng, op):
    try:
        if op[0] == "submit":
            return ("ok", eng.submit(op[1], op[2], op[3]))
        if op[0] == "act":
            return ("ok", eng.act(op[1], op[2], op[3], op[4],
                                  target_id=op[5]))
        if op[0] == "withdraw":
            return ("ok", eng.withdraw(op[1], op[2], op[3]))
        if op[0] == "set_status":
            return ("ok", eng.set_status(op[1], op[2], op[3]))
        if op[0] == "todo":
            return ("ok", eng.todo(op[1], op[2]))
        if op[0] == "history":
            return ("ok", eng.history(op[1], op[2]))
        raise AssertionError("bad op " + repr(op))
    except EngineError as ex:
        return ("err", type(ex).__name__, str(ex))


def final_dump(eng, t, max_iid):
    dump = {"instances": {}, "todos": {}}
    for iid in range(1, max_iid + 1):
        dump["instances"][iid] = (
            eng.instance_status(t, iid), eng.history(t, iid))
    for emp in EMP_IDS:
        dump["todos"][emp] = eng.todo(t, emp)
    return dump


def run(ops, max_iid, final_t, mode, seed):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    rng = random.Random(seed)
    try:
        eng = Engine(path, ORG)
        results = []
        cur = 0
        for op in ops:
            t = op[1]
            if mode == "step":
                for s in range(cur + 1, t + 1):
                    eng.advance(s)
            elif mode == "random":
                if rng.random() < 0.4:
                    eng.advance(t)
                if rng.random() < 0.3:
                    eng.close()
                    eng = Engine(path)
            results.append(exec_op(eng, op))
            cur = t
        dump = final_dump(eng, final_t, max_iid)
        eng.close()
        return results, dump
    finally:
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(path + suffix)
            except OSError:
                pass


class RandomizedConsistencyTest(unittest.TestCase):
    def check_sequence(self, ops, tag):
        max_iid = sum(1 for op in ops if op[0] == "submit")
        final_t = max(op[1] for op in ops)
        expected = run(ops, max_iid, final_t, "step", 0)
        for mode in ("none", "random"):
            got = run(ops, max_iid, final_t, mode, 12345)
            self.assertEqual(expected, got,
                             "%s: mode %r diverges" % (tag, mode))

    def test_edge_cases(self):
        self.check_sequence(edge_case_ops(), "edge")

    def test_random_sequences(self):
        for seed in range(10):
            rng = random.Random(seed)
            ops, _ = gen_ops(rng, 150, t0=30, iid0=0)
            self.check_sequence(ops, "seed=%d" % seed)

    def test_edge_then_random(self):
        rng = random.Random(999)
        tail, _ = gen_ops(rng, 120, t0=30, iid0=2)
        self.check_sequence(edge_case_ops() + tail, "edge+random")


if __name__ == "__main__":
    unittest.main()
