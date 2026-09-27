"""常量与数据类型定义。只放类型，不放逻辑。"""

# 员工状态
ACTIVE = "Active"
ON_LEAVE = "OnLeave"
OFFBOARDING = "Offboarding"
DEPARTED = "Departed"
EMPLOYEE_STATUSES = (ACTIVE, ON_LEAVE, OFFBOARDING, DEPARTED)

# 实例状态
INST_PENDING = "Pending"
INST_APPROVED = "Approved"
INST_REJECTED = "Rejected"
INST_WITHDRAWN = "Withdrawn"
INST_EXPIRED = "Expired"

# 待办状态
TODO_OPEN = "Open"
TODO_CLOSED = "Closed"

# act 动作
ACT_APPROVE = "approve"
ACT_REJECT = "reject"
ACT_TRANSFER = "transfer"
ACT_ADDSIGN = "addsign"
