"""一个 capability 允许触达的 SQL 表面。

本模块是 SQL 面的**唯一声明处**：允许的方言、表、列、输出别名、时间列，以及
行数与窗口上限。参数 schema（``planning.starrocks.params``）与 SQLGuard
（``governance.sqlguard``）都从这里取值，因此"谁定的上限"有单一答案，两处断言
不会各自漂移。

上限常量定义在**契约层**而不是参数模块：``capabilities`` 不得依赖 ``planning``
（分层白名单承重），而声明 surface 的一侧与消费 surface 的一侧都必须看到同一个
取值。放在两边都能依赖的最低层，是唯一不产生第二真源的落点。
"""

from typing import Final

from pydantic import Field

from xiaowei_agent.contracts.base import Contract, StrictInt, StrictStr

MAX_WINDOW_MINUTES: Final[int] = 360
"""时间窗跨度上限：6 小时。全项目唯一定义处。"""

MAX_ROW_LIMIT: Final[int] = 200
"""单次取数行数上限。全项目唯一定义处。"""


class SqlSurface(Contract):
    """一个 capability 允许触达的 SQL 表面。

    ``allowed_tables`` 的元素形如 ``"db.table"``，**大小写敏感**，与
    ``sqlguard.table_key()`` 的输出格式逐字一致——比较口径只有一处定义。

    大小写敏感是刻意的：编译器只会发出声明中的确切写法，任何大小写差异都意味着
    这条 SQL 不是编译器产物，fail-closed。
    """

    surface_id: StrictStr
    dialect: StrictStr
    allowed_tables: tuple[StrictStr, ...]
    allowed_columns: tuple[StrictStr, ...]
    allowed_output_aliases: tuple[StrictStr, ...]
    time_column: StrictStr
    max_row_limit: StrictInt = Field(gt=0)
    max_window_minutes: StrictInt = Field(gt=0)
