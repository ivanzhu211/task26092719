"""引擎异常类型。"""


class EngineError(Exception):
    """所有引擎调用失败时抛出的异常基类。"""


class NotFoundError(EngineError):
    """员工、实例等待办对象不存在。"""


class InvalidActionError(EngineError):
    """操作在当前状态下不合法（如对非 Active 员工转交）。"""
