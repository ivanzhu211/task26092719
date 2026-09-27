"""审批流引擎主体。所有可观测结果只由带时间戳的操作序列决定。"""

import json
import sqlite3

from . import models
from .errors import EngineError, InvalidActionError, NotFoundError

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS departments (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    parent_id INTEGER,
    manager_id INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS employees (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    dept_id INTEGER NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS instances (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    submitter_id INTEGER NOT NULL REFERENCES employees (id),
    template TEXT NOT NULL,
    status TEXT NOT NULL,
    current_layer INTEGER NOT NULL,
    created_t INTEGER NOT NULL,
    closed_t INTEGER
);
CREATE TABLE IF NOT EXISTS todos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id INTEGER NOT NULL REFERENCES instances (id),
    layer_idx INTEGER NOT NULL,
    holder_id INTEGER NOT NULL REFERENCES employees (id),
    created_t INTEGER NOT NULL,
    deadline INTEGER NOT NULL,
    status TEXT NOT NULL,
    closed_t INTEGER,
    close_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_todos_open_deadline
    ON todos (status, deadline, id);
CREATE INDEX IF NOT EXISTS idx_todos_holder
    ON todos (status, holder_id, deadline, id);
CREATE INDEX IF NOT EXISTS idx_todos_instance
    ON todos (instance_id, status, holder_id);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_id INTEGER NOT NULL REFERENCES instances (id),
    t INTEGER NOT NULL,
    type TEXT NOT NULL,
    actor_id INTEGER,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_instance ON events (instance_id, id);
"""


class Engine:
    """以 sqlite3 文件路径构造；组织在首次初始化时载入。"""

    def __init__(self, path, org=None):
        self._path = path
        self._conn = sqlite3.connect(path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        initialized = self._get_meta("initialized")
        if initialized is None:
            if org is None:
                raise EngineError("new database requires org")
            self._load_org(org)
            self._set_meta("initialized", "1")
            self._set_meta("last_t", "0")
        self._conn.commit()

    def close(self):
        self._conn.commit()
        self._conn.close()

    # ---- 内部：meta 与时间守卫 ----

    def _get_meta(self, key):
        row = self._conn.execute(
            "SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def _set_meta(self, key, value):
        self._conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value))

    def _check_time(self, t):
        if not isinstance(t, int) or t < 0:
            raise EngineError("t must be a non-negative integer")
        last = int(self._get_meta("last_t"))
        if t < last:
            raise EngineError("t must be monotonically non-decreasing")
        self._set_meta("last_t", str(t))

    # ---- 内部：组织载入 ----

    def _load_org(self, org):
        departments = org["departments"]
        employees = org["employees"]
        if len(departments) > 0:
            ids = {d["id"] for d in departments}
            depth = {}
            for d in departments:
                if d.get("parent_id") is not None and d["parent_id"] not in ids:
                    raise EngineError("bad parent_id")
            def _depth(d):
                if d["id"] in depth:
                    return depth[d["id"]]
                parent = d.get("parent_id")
                if parent is None:
                    depth[d["id"]] = 1
                else:
                    by_id = {x["id"]: x for x in departments}
                    depth[d["id"]] = _depth(by_id[parent]) + 1
                return depth[d["id"]]
            for d in departments:
                if _depth(d) > 4:
                    raise EngineError("department tree deeper than 4 levels")
        for d in departments:
            self._conn.execute(
                "INSERT INTO departments (id, name, parent_id, manager_id)"
                " VALUES (?, ?, ?, ?)",
                (d["id"], d["name"], d.get("parent_id"), d["manager_id"]))
        for e in employees:
            status = e.get("status", models.ACTIVE)
            if status not in models.EMPLOYEE_STATUSES:
                raise EngineError("bad employee status")
            self._conn.execute(
                "INSERT INTO employees (id, name, dept_id, status)"
                " VALUES (?, ?, ?, ?)",
                (e["id"], e["name"], e["dept_id"], status))


    # ---- 内部：员工与模板 ----

    def _employee(self, emp_id):
        row = self._conn.execute(
            "SELECT id, name, dept_id, status FROM employees WHERE id=?",
            (emp_id,)).fetchone()
        if row is None:
            raise NotFoundError("employee %r not found" % (emp_id,))
        return {"id": row[0], "name": row[1], "dept_id": row[2],
                "status": row[3]}

    def _normalize_template(self, template):
        if not isinstance(template, (list, tuple)) or len(template) == 0:
            raise InvalidActionError("template must be a non-empty list")
        layers = []
        for layer in template:
            timeout = layer.get("timeout")
            if not isinstance(timeout, int) or timeout < 1:
                raise InvalidActionError("layer timeout must be int >= 1")
            approvers = layer.get("approvers")
            if not isinstance(approvers, (list, tuple)) or not approvers:
                raise InvalidActionError("layer needs >= 1 approver")
            specs = []
            for spec in approvers:
                if "employee" in spec:
                    emp = self._employee(spec["employee"])
                    specs.append({"type": "employee", "id": emp["id"]})
                elif "manager" in spec:
                    k = spec["manager"]
                    if not isinstance(k, int) or k < 1:
                        raise InvalidActionError("manager level must be >= 1")
                    specs.append({"type": "manager", "levels": k})
                else:
                    raise InvalidActionError("bad approver spec")
            layers.append({"timeout": timeout, "approvers": specs})
        return layers

    # ---- 内部：审批人解析与向上查找 ----

    def _dept_manager(self, dept_id):
        row = self._conn.execute(
            "SELECT manager_id FROM departments WHERE id=?",
            (dept_id,)).fetchone()
        return row[0] if row else None

    def _dept_parent(self, dept_id):
        row = self._conn.execute(
            "SELECT parent_id FROM departments WHERE id=?",
            (dept_id,)).fetchone()
        return row[0] if row and row[0] is not None else None

    def _resolve_spec(self, spec, submitter):
        """把审批人规格解析成具体员工 id；解析不出返回 None。"""
        if spec["type"] == "employee":
            return spec["id"]
        dept_id = submitter["dept_id"]
        for _ in range(spec["levels"]):
            parent = self._dept_parent(dept_id)
            if parent is None:
                return None
            dept_id = parent
        return self._dept_manager(dept_id)

    def _is_qualified(self, emp_id, instance_id, submitter_id):
        if emp_id is None or emp_id == submitter_id:
            return False
        row = self._conn.execute(
            "SELECT status FROM employees WHERE id=?", (emp_id,)).fetchone()
        if row is None or row[0] != models.ACTIVE:
            return False
        row = self._conn.execute(
            "SELECT 1 FROM todos WHERE instance_id=? AND holder_id=?"
            " AND status=? LIMIT 1",
            (instance_id, emp_id, models.TODO_OPEN)).fetchone()
        return row is None

    def _find_upward(self, from_emp_id, instance_id, submitter_id):
        """从 from_emp 所在部门负责人开始逐级向上找合格审批人。

        本人就是负责人则从上级部门负责人开始。找不到返回 None。
        """
        emp = self._employee(from_emp_id)
        dept_id = emp["dept_id"]
        if self._dept_manager(dept_id) == from_emp_id:
            dept_id = self._dept_parent(dept_id)
        while dept_id is not None:
            candidate = self._dept_manager(dept_id)
            if self._is_qualified(candidate, instance_id, submitter_id):
                return candidate
            dept_id = self._dept_parent(dept_id)
        return None

    # ---- 内部：事件与待办 ----

    def _emit(self, instance_id, t, type_, actor_id=None, payload=None):
        self._conn.execute(
            "INSERT INTO events (instance_id, t, type, actor_id, payload)"
            " VALUES (?, ?, ?, ?, ?)",
            (instance_id, t, type_, actor_id,
             json.dumps(payload or {}, sort_keys=True)))

    def _create_todo(self, instance_id, layer_idx, holder_id, t, deadline,
                     reason):
        cur = self._conn.execute(
            "INSERT INTO todos (instance_id, layer_idx, holder_id,"
            " created_t, deadline, status) VALUES (?, ?, ?, ?, ?, ?)",
            (instance_id, layer_idx, holder_id, t, deadline,
             models.TODO_OPEN))
        todo_id = cur.lastrowid
        self._emit(instance_id, t, "todo_create", None, {
            "todo_id": todo_id, "holder_id": holder_id,
            "layer_idx": layer_idx, "deadline": deadline, "reason": reason})
        return todo_id

    def _close_todo(self, todo_id, t, reason):
        self._conn.execute(
            "UPDATE todos SET status=?, closed_t=?, close_reason=?"
            " WHERE id=?",
            (models.TODO_CLOSED, t, reason, todo_id))

    def _close_instance_todos(self, instance_id, t, reason):
        rows = self._conn.execute(
            "SELECT id FROM todos WHERE instance_id=? AND status=?",
            (instance_id, models.TODO_OPEN)).fetchall()
        for (todo_id,) in rows:
            self._close_todo(todo_id, t, reason)

    def _finish_instance(self, instance_id, t, status):
        self._conn.execute(
            "UPDATE instances SET status=?, closed_t=? WHERE id=?",
            (status, t, instance_id))
        self._close_instance_todos(instance_id, t, "instance_" + status)
        self._emit(instance_id, t, "complete", None, {"result": status})

    def _expire_instance(self, instance_id, t):
        self._finish_instance(instance_id, t, models.INST_EXPIRED)

    # ---- 内部：层激活 ----

    def _activate_layer(self, instance_id, layer_idx, t, layers, submitter):
        self._emit(instance_id, t, "layer_activate", None,
                   {"layer_idx": layer_idx})
        layer = layers[layer_idx]
        deadline = t + layer["timeout"]
        for spec in layer["approvers"]:
            person = self._resolve_spec(spec, submitter)
            if not self._is_qualified(person, instance_id, submitter["id"]):
                if person is None:
                    person = submitter["id"]
                person = self._find_upward(person, instance_id,
                                           submitter["id"])
            if person is None:
                self._expire_instance(instance_id, t)
                return False
            self._create_todo(instance_id, layer_idx, person, t, deadline,
                              "initial")
        return True

    def _process_due(self, t):
        """按 (截止, 创建顺序) 处理所有截止 <= t 的超时。

        每次只取最早的一条到期待办，处理后再取，因此连环升级的开销
        与真正到期的事件数成正比；事件时间戳一律取截止时刻 d。
        """
        while True:
            # 不变式：实例终止时其全部待办同步关闭，
            # 因此 Open 待办必属 Pending 实例，无需 JOIN。
            row = self._conn.execute(
                "SELECT id, instance_id, layer_idx, holder_id, deadline"
                " FROM todos WHERE status=? AND deadline<=?"
                " ORDER BY deadline, id LIMIT 1",
                (models.TODO_OPEN, t)).fetchone()
            if row is None:
                return
            todo_id, instance_id, layer_idx, holder_id, d = row
            self._close_todo(todo_id, d, "timeout")
            self._emit(instance_id, d, "timeout", None, {
                "todo_id": todo_id, "holder_id": holder_id,
                "layer_idx": layer_idx})
            inst = self._conn.execute(
                "SELECT submitter_id, template FROM instances WHERE id=?",
                (instance_id,)).fetchone()
            submitter_id, template = inst
            layers = json.loads(template)
            target = self._find_upward(holder_id, instance_id, submitter_id)
            if target is None:
                self._expire_instance(instance_id, d)
            else:
                new_deadline = d + layers[layer_idx]["timeout"]
                self._create_todo(instance_id, layer_idx, target, d,
                                  new_deadline, "escalation")

    # ---- 公开 API ----

    def submit(self, t, submitter_id, template):
        self._check_time(t)
        self._process_due(t)
        submitter = self._employee(submitter_id)
        layers = self._normalize_template(template)
        cur = self._conn.execute(
            "INSERT INTO instances (submitter_id, template, status,"
            " current_layer, created_t) VALUES (?, ?, ?, 0, ?)",
            (submitter_id, json.dumps(layers), models.INST_PENDING, t))
        instance_id = cur.lastrowid
        self._emit(instance_id, t, "submit", submitter_id, {})
        self._activate_layer(instance_id, 0, t, layers, submitter)
        self._conn.commit()
        return instance_id

    def _open_todo_for(self, instance_id, employee_id):
        return self._conn.execute(
            "SELECT id, layer_idx, deadline FROM todos"
            " WHERE instance_id=? AND holder_id=? AND status=?"
            " ORDER BY id LIMIT 1",
            (instance_id, employee_id, models.TODO_OPEN)).fetchone()

    def _pending_instance(self, instance_id):
        row = self._conn.execute(
            "SELECT submitter_id, template, status, current_layer"
            " FROM instances WHERE id=?", (instance_id,)).fetchone()
        if row is None:
            raise NotFoundError("instance %r not found" % (instance_id,))
        return {"submitter_id": row[0], "layers": json.loads(row[1]),
                "status": row[2], "current_layer": row[3]}

    def _require_qualified_target(self, target_id, instance_id,
                                  submitter_id):
        self._employee(target_id)
        if not self._is_qualified(target_id, instance_id, submitter_id):
            raise InvalidActionError(
                "target %r is not a qualified approver" % (target_id,))

    def _check_layer_done(self, instance_id, t, inst):
        row = self._conn.execute(
            "SELECT COUNT(*) FROM todos WHERE instance_id=? AND layer_idx=?"
            " AND status=?",
            (instance_id, inst["current_layer"], models.TODO_OPEN)).fetchone()
        if row[0] > 0:
            return
        next_layer = inst["current_layer"] + 1
        if next_layer >= len(inst["layers"]):
            self._finish_instance(instance_id, t, models.INST_APPROVED)
            return
        self._conn.execute(
            "UPDATE instances SET current_layer=? WHERE id=?",
            (next_layer, instance_id))
        submitter = self._employee(inst["submitter_id"])
        self._activate_layer(instance_id, next_layer, t, inst["layers"],
                             submitter)

    def act(self, t, employee_id, instance_id, action, target_id=None):
        self._check_time(t)
        self._process_due(t)
        inst = self._pending_instance(instance_id)
        if inst["status"] != models.INST_PENDING:
            raise InvalidActionError("instance is not pending")
        todo = self._open_todo_for(instance_id, employee_id)
        if todo is None:
            raise InvalidActionError("no open todo for employee")
        todo_id, layer_idx, deadline = todo
        if action == models.ACT_APPROVE:
            self._close_todo(todo_id, t, "approved")
            self._emit(instance_id, t, "approve", employee_id,
                       {"todo_id": todo_id})
            self._check_layer_done(instance_id, t, inst)
        elif action == models.ACT_REJECT:
            self._emit(instance_id, t, "reject", employee_id,
                       {"todo_id": todo_id})
            self._finish_instance(instance_id, t, models.INST_REJECTED)
        elif action == models.ACT_TRANSFER:
            if target_id is None:
                raise InvalidActionError("transfer needs target_id")
            self._require_qualified_target(target_id, instance_id,
                                           inst["submitter_id"])
            self._close_todo(todo_id, t, "transfer")
            self._emit(instance_id, t, "transfer", employee_id,
                       {"todo_id": todo_id, "from": employee_id,
                        "to": target_id})
            self._create_todo(instance_id, layer_idx, target_id, t,
                              deadline, "transfer")
        elif action == models.ACT_ADDSIGN:
            if target_id is None:
                raise InvalidActionError("addsign needs target_id")
            self._require_qualified_target(target_id, instance_id,
                                           inst["submitter_id"])
            layer_timeout = inst["layers"][layer_idx]["timeout"]
            self._emit(instance_id, t, "addsign", employee_id,
                       {"todo_id": todo_id, "holder_id": target_id})
            self._create_todo(instance_id, layer_idx, target_id, t,
                              t + layer_timeout, "addsign")
        else:
            raise InvalidActionError("unknown action %r" % (action,))
        self._conn.commit()

    def withdraw(self, t, employee_id, instance_id):
        self._check_time(t)
        self._process_due(t)
        inst = self._pending_instance(instance_id)
        if inst["status"] != models.INST_PENDING:
            raise InvalidActionError("instance is not pending")
        if inst["submitter_id"] != employee_id:
            raise InvalidActionError("only submitter can withdraw")
        self._emit(instance_id, t, "withdraw", employee_id, {})
        self._finish_instance(instance_id, t, models.INST_WITHDRAWN)
        self._conn.commit()

    def set_status(self, t, employee_id, status):
        self._check_time(t)
        self._process_due(t)
        self._employee(employee_id)
        if status not in models.EMPLOYEE_STATUSES:
            raise InvalidActionError("bad status %r" % (status,))
        self._conn.execute("UPDATE employees SET status=? WHERE id=?",
                           (status, employee_id))
        if status != models.ACTIVE:
            rows = self._conn.execute(
                "SELECT id, instance_id, layer_idx, deadline FROM todos"
                " WHERE holder_id=? AND status=? ORDER BY id",
                (employee_id, models.TODO_OPEN)).fetchall()
            for todo_id, instance_id, layer_idx, deadline in rows:
                inst = self._pending_instance(instance_id)
                target = self._find_upward(employee_id, instance_id,
                                           inst["submitter_id"])
                self._close_todo(todo_id, t, "auto_transfer")
                if target is None:
                    self._emit(instance_id, t, "auto_transfer", None,
                               {"todo_id": todo_id, "from": employee_id,
                                "to": None, "cause": status})
                    self._expire_instance(instance_id, t)
                else:
                    self._emit(instance_id, t, "auto_transfer", None,
                               {"todo_id": todo_id, "from": employee_id,
                                "to": target, "cause": status})
                    self._create_todo(instance_id, layer_idx, target, t,
                                      deadline, "auto_transfer")
        self._conn.commit()



    def advance(self, t):
        self._check_time(t)
        self._process_due(t)
        self._conn.commit()

    def todo(self, t, employee_id):
        self._check_time(t)
        self._process_due(t)
        self._employee(employee_id)
        rows = self._conn.execute(
            "SELECT id, instance_id, layer_idx, created_t, deadline"
            " FROM todos WHERE status=? AND holder_id=?"
            " ORDER BY deadline, id",
            (models.TODO_OPEN, employee_id)).fetchall()
        self._conn.commit()
        return [{"todo_id": r[0], "instance_id": r[1], "layer_idx": r[2],
                 "created_t": r[3], "deadline": r[4]} for r in rows]

    def history(self, t, instance_id):
        self._check_time(t)
        self._process_due(t)
        self._conn.commit()
        rows = self._conn.execute(
            "SELECT t, type, actor_id, payload FROM events"
            " WHERE instance_id=? ORDER BY id", (instance_id,)).fetchall()
        if not rows:
            raise NotFoundError("instance %r not found" % (instance_id,))
        return [{"t": r[0], "type": r[1], "actor_id": r[2],
                 "payload": json.loads(r[3])} for r in rows]

    def instance_status(self, t, instance_id):
        self._check_time(t)
        self._process_due(t)
        self._conn.commit()
        row = self._conn.execute(
            "SELECT status FROM instances WHERE id=?",
            (instance_id,)).fetchone()
        if row is None:
            raise NotFoundError("instance %r not found" % (instance_id,))
        return row[0]
