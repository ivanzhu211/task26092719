# task26092719 — 审批流引擎库

纯标准库实现的审批流引擎，全部状态落在单个 sqlite3 文件中。

## 用法

```python
from approval_engine import Engine

org = {
    "departments": [  # 最多 4 层，每个部门一名负责人
        {"id": 1, "name": "root", "parent_id": None, "manager_id": 10},
        {"id": 2, "name": "eng", "parent_id": 1, "manager_id": 11},
    ],
    "employees": [
        {"id": 10, "name": "ceo", "dept_id": 1},          # 默认 Active
        {"id": 11, "name": "lead", "dept_id": 2, "status": "OnLeave"},
    ],
}

eng = Engine("approval.db", org)   # 首次创建需传 org；重开时只需路径
eng = Engine("approval.db")        # 重开：组织与全部状态从文件恢复

# 所有调用显式带整数秒时刻 t（单调不减），引擎不读系统时钟。
iid = eng.submit(0, 11, [
    {"timeout": 3600, "approvers": [{"manager": 1}]},      # 提交人上级部门负责人
    {"timeout": 7200, "approvers": [{"employee": 10}]},    # 指定员工
])
eng.act(10, 10, iid, "approve")                  # 同意 / reject / transfer / addsign
eng.act(20, 10, iid, "transfer", target_id=11)   # 转交：截止不变
eng.act(30, 10, iid, "addsign", target_id=11)    # 加签：截止 = t + 本层超时
eng.withdraw(40, 11, iid)                        # 提交人撤回
eng.set_status(50, 11, "OnLeave")                # 非 Active 的未关闭待办当即转出
eng.advance(10**6)                               # 处理截止 <= t 的全部超时
eng.todo(10**6, 10)          # 待办：按 (截止, 创建顺序) 升序
eng.history(10**6, iid)      # 带时间戳的完整事件序列
eng.instance_status(10**6, iid)
eng.close()
```

## 语义要点

- 任何带时刻 `t` 的调用执行前，所有截止 `<= t` 的超时已按
  （截止时刻, 待办创建顺序）处理完毕；事件时间戳一律取截止时刻，
  因此恰在截止时刻的同意无效。
- 可观测结果只由带时间戳的操作序列决定：与 `advance` 的调用次数、
  粒度无关，也与中途关闭重开数据库文件无关。
- 升级目标从原持有人所在部门负责人开始（本人是负责人则从上级部门）
  逐级向上，跳过非 Active 者、提交人、已持有本实例未关闭待办者；
  找到根仍无人则实例以 `Expired` 终止。
- 超时处理按需驱动：一次跨越 30 天的 `advance`，开销只与期间真正
  到期的事件数成正比（索引：`todos(status, deadline, id)`），
  不扫描全部待办。

## 测试

```
python3 -m unittest discover -s tests
```

- `tests/test_deterministic.py`：单次 `advance` 跨过三层升级链，
  逐条断言升级事件时间戳。
- `tests/test_edge_semantics.py`：恰在截止时刻动作、与超时同刻的
  状态变更、加签后并行待办先后超时。
- `tests/test_randomized.py`：随机操作序列分别用逐秒推进 / 从不推进 /
  随机推进+随机关闭重开三种方式执行，任何操作结果、history 或
  todo 不一致即失败。
