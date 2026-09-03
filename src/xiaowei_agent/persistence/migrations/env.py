"""Alembic 运行环境。

**连接串只从注入来**，三个来源按优先级：程序化设置的 ``connection`` attribute、
``-x dsn=...`` 命令行参数、``sqlalchemy.url`` 主选项。三者都没有就抛错。

**不读 ``.env``、不读 ``XIAOWEI_*``**。迁移的连接来源与 ``load_settings()`` 的应用
配置来源互相隔离：把它们并到一处，改一边就会静默改变另一边的行为，而"迁移连到了
哪个库"是这个项目里最不该靠推断的事。
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
