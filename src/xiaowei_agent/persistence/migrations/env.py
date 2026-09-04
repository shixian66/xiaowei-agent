"""Alembic 运行环境。

**连接串只从注入来**，三个来源按优先级：程序化设置的 ``connection`` attribute、
``-x dsn=...`` 命令行参数、``sqlalchemy.url`` 主选项。三者都没有就抛错。

**不读 ``.env``、不读 ``XIAOWEI_*``**。迁移的连接来源与 ``load_settings()`` 的应用
配置来源互相隔离：把它们并到一处，改一边就会静默改变另一边的行为，而"迁移连到了
哪个库"是这个项目里最不该靠推断的事。

**为什么散文写在这里而不是 ``alembic.ini``**：Alembic 用
``ConfigParser.read(..., encoding="locale")`` 读那个 ini（``alembic/util/compat.py``），
``"locale"`` 由**进程环境**决定，不由仓库决定，且 Alembic 没有任何选项能覆盖它。
于是 ini 里的一个非 ASCII 字节，会在 locale 编码不覆盖它的机器上直接让文件无法解码
——不只是测试红，``alembic upgrade head`` 同样跑不起来。``.py`` 文件没有这个问题：
PEP 263 规定源文件默认按 UTF-8 解码，与 locale 无关。因此 ini 保持 ASCII-only，
解释性文字全部放在这里。该约束由 ``tests/contract/test_config_encoding.py`` 承重。
"""

from alembic import context
from sqlalchemy import engine_from_config, pool

from xiaowei_agent.persistence.schema import METADATA

config = context.config
target_metadata = METADATA


def _require_url() -> str:
    injected = context.get_x_argument(as_dictionary=True).get("dsn")
    url = injected or config.get_main_option("sqlalchemy.url")
    if not url:
        raise RuntimeError(
            "no database URL supplied; pass -x dsn=... or set sqlalchemy.url programmatically"
        )
    return url


def run_migrations_offline() -> None:
    context.configure(
        url=_require_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = config.attributes.get("connection")
    if connectable is None:
        config.set_main_option("sqlalchemy.url", _require_url())
        connectable = engine_from_config(
            config.get_section(config.config_ini_section, {}),
            prefix="sqlalchemy.",
            poolclass=pool.NullPool,
        )
        with connectable.connect() as connection:
            context.configure(connection=connection, target_metadata=target_metadata)
            with context.begin_transaction():
                context.run_migrations()
        return
    context.configure(connection=connectable, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
