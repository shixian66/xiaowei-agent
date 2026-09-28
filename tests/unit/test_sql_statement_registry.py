"""SQL 语句识别登记表的完整性（设计 §5.1）。

``STARROCKS_STATEMENT_DOCS`` 是 StarRocks 仓库 ``docs/en/sql-reference/sql-statements``
下全部文档页的快照（StarRocks main ``5cb44e451b3603c31934a4598dd6d9cb4807628f``，
2026-09-28 取得）。每一页的顶层 SQL 语句都必须在登记表里有形式，并由该页“Examples”里的
真实语句证明被识别为 SQL；否则这条语句会按对话进入模型。升级 StarRocks 大版本时刷新快照，
新增的文档页会让本测试失败，直到登记。
"""

import pytest

from xiaowei_agent.governance.sql_message import (
    SQL_STATEMENT_KEYWORDS,
    SqlTextKind,
    classify_sql_text,
)
from xiaowei_agent.governance.sql_statements import SQL_STATEMENT_FORMS

STARROCKS_STATEMENT_DOCS = frozenset(
    """
    ADD_BACKEND_BLACKLIST ADD_SQLBLACKLIST ADMIN_CANCEL_REPAIR ADMIN_CHECK_TABLET ADMIN_REPAIR
    ADMIN_SET_CONFIG ADMIN_SET_PARTITION_VERSION ADMIN_SET_REPLICA_STATUS ADMIN_SHOW_CONFIG
    ADMIN_SHOW_REPLICA_DISTRIBUTION ADMIN_SHOW_REPLICA_STATUS ADMIN_SHOW_TABLET_STATUS
    ADMIN_SKIP_COMMITTED_TRANSACTION ALTER_AI_PROVIDER ALTER_DATABASE ALTER_LOAD
    ALTER_MATERIALIZED_VIEW ALTER_PIPE ALTER_RESOURCE ALTER_RESOURCE_GROUP ALTER_ROUTINE_LOAD
    ALTER_STORAGE_VOLUME ALTER_SYSTEM ALTER_TABLE ALTER_TASK ALTER_USER ALTER_VIEW ANALYZE_PROFILE
    ANALYZE_TABLE AWS_CREDENTIAL_SUPPORT BACKUP BROKER_LOAD CANCEL_ALTER_TABLE CANCEL_BACKUP
    CANCEL_DECOMMISSION CANCEL_EXPORT CANCEL_LOAD CANCEL_REFRESH_DICTIONARY
    CANCEL_REFRESH_MATERIALIZED_VIEW CANCEL_RESTORE CREATE_AI_PROVIDER CREATE_ANALYZE
    CREATE_DATABASE CREATE_DICTIONARY CREATE_EXTERNAL_CATALOG CREATE_FILE CREATE_FUNCTION
    CREATE_INDEX CREATE_MATERIALIZED_VIEW CREATE_PIPE CREATE_REPOSITORY CREATE_RESOURCE
    CREATE_RESOURCE_GROUP CREATE_ROLE CREATE_ROUTINE_LOAD CREATE_STORAGE_VOLUME CREATE_TABLE
    CREATE_TABLE_AS_SELECT CREATE_TABLE_LIKE CREATE_USER CREATE_VIEW DELETE
    DELETE_BACKEND_BLACKLIST DELETE_SQLBLACKLIST DESCRIBE DESC_AI_PROVIDER DESC_STORAGE_VOLUME
    DROP_AI_PROVIDER DROP_ANALYZE DROP_CATALOG DROP_DATABASE DROP_DICTIONARY DROP_FILE
    DROP_FUNCTION DROP_INDEX DROP_MATERIALIZED_VIEW DROP_PIPE DROP_REPOSITORY DROP_RESOURCE
    DROP_RESOURCE_GROUP DROP_ROLE DROP_SNAPSHOT DROP_STATS DROP_STORAGE_VOLUME DROP_TABLE
    DROP_TASK DROP_USER DROP_VIEW EXECUTE_AS EXPLAIN EXPLAIN_ANALYZE EXPORT GRANT INSERT
    INSTALL_PLUGIN KILL KILL_ANALYZE PAUSE_ROUTINE_LOAD RECOVER REFRESH_CONNECTIONS
    REFRESH_DICTIONARY REFRESH_EXTERNAL_TABLE REFRESH_MATERIALIZED_VIEW RESTORE
    RESUME_ROUTINE_LOAD RETRY_FILE REVOKE SELECT SELECT_CTE SELECT_DISTINCT SELECT_EXCEPT_MINUS
    SELECT_EXCLUDE SELECT_GROUP_BY SELECT_HAVING SELECT_INTERSECT SELECT_JOIN SELECT_LIMIT
    SELECT_OFFSET SELECT_ORDER_BY SELECT_PIVOT SELECT_UNION SELECT_WHERE_operator SELECT_alias
    SELECT_subquery SET SET_CATALOG SET_DEFAULT_AI_PROVIDER SET_DEFAULT_ROLE
    SET_DEFAULT_STORAGE_VOLUME SET_PASSWORD SET_ROLE SHOW_AI_PROVIDERS SHOW_ALTER
    SHOW_ALTER_MATERIALIZED_VIEW SHOW_ANALYZE_JOB SHOW_ANALYZE_STATUS SHOW_AUTHENTICATION
    SHOW_BACKENDS SHOW_BACKEND_BLACKLIST SHOW_BACKUP SHOW_BROKER SHOW_CATALOGS SHOW_COMPUTE_NODES
    SHOW_CREATE_CATALOG SHOW_CREATE_DATABASE SHOW_CREATE_FUNCTION SHOW_CREATE_MATERIALIZED_VIEW
    SHOW_CREATE_TABLE SHOW_CREATE_VIEW SHOW_DATA SHOW_DATABASES SHOW_DELETE SHOW_DICTIONARY
    SHOW_DYNAMIC_PARTITION_TABLES SHOW_EXPORT SHOW_FILE SHOW_FRONTENDS SHOW_FULL_COLUMNS
    SHOW_FUNCTIONS SHOW_GRANTS SHOW_INDEX SHOW_LOAD SHOW_MATERIALIZED_VIEW SHOW_META
    SHOW_PARTITIONS SHOW_PIPES SHOW_PLUGINS SHOW_PROC SHOW_PROCESSLIST SHOW_PROFILELIST
    SHOW_PROPERTY SHOW_REPOSITORIES SHOW_RESOURCES SHOW_RESOURCE_GROUP SHOW_RESTORE SHOW_ROLES
    SHOW_ROUTINE_LOAD SHOW_ROUTINE_LOAD_TASK SHOW_RUNNING_QUERIES SHOW_SNAPSHOT SHOW_SQLBLACKLIST
    SHOW_STORAGE_VOLUMES SHOW_TABLES SHOW_TABLET SHOW_TABLE_STATUS SHOW_TRANSACTION
    SHOW_USAGE_RESOURCE_GROUPS SHOW_USERS SHOW_VARIABLES SHOW_WARNINGS SPARK_LOAD
    STOP_ROUTINE_LOAD STREAM_LOAD SUBMIT_TASK SUSPEND_or_RESUME_PIPE SYNC TRANSLATE_TRINO
    TRUNCATE_TABLE UNINSTALL_PLUGIN UPDATE USE auto_increment generated_columns keywords
    prepared_statement
    """.split()
)

NOT_A_STATEMENT = frozenset(
    {
        # 概念或 HTTP 接口说明，不是用户可发送的顶层 SQL 语句。
        "AWS_CREDENTIAL_SUPPORT",
        "STREAM_LOAD",
        "auto_increment",
        "generated_columns",
        "keywords",
    }
)

# 每个文档页至少一条取自该页的真实语句（占位符换成具体名字）。
EXAMPLES: dict[str, tuple[str, ...]] = {
    "ADD_BACKEND_BLACKLIST": ("ADD BACKEND BLACKLIST 10001", "ADD COMPUTE NODE BLACKLIST 1, 2"),
    "ADD_SQLBLACKLIST": ('ADD SQLBLACKLIST "select count\\\\(\\\\*\\\\) from .+"',),
    "ADMIN_CANCEL_REPAIR": ("ADMIN CANCEL REPAIR TABLE tbl PARTITION(p1)",),
    "ADMIN_CHECK_TABLET": ('ADMIN CHECK TABLET (10000, 10001) PROPERTIES("type" = "consistency")',),
    "ADMIN_REPAIR": ("ADMIN REPAIR TABLE tbl1", "ADMIN REPAIR TABLE tbl1 PARTITION (p1, p2)"),
    "ADMIN_SET_CONFIG": ('ADMIN SET FRONTEND CONFIG ("disable_balance" = "true")',),
    "ADMIN_SET_PARTITION_VERSION": ("ADMIN SET TABLE t1 PARTITION(t1) VERSION TO 10",),
    "ADMIN_SET_REPLICA_STATUS": (
        'ADMIN SET REPLICA STATUS PROPERTIES("tablet_id" = "10003", "status" = "bad")',
    ),
    "ADMIN_SHOW_CONFIG": (
        "ADMIN SHOW FRONTEND CONFIG",
        "ADMIN SHOW FRONTEND CONFIG LIKE '%check%'",
    ),
    "ADMIN_SHOW_REPLICA_DISTRIBUTION": ("ADMIN SHOW REPLICA DISTRIBUTION FROM tbl1",),
    "ADMIN_SHOW_REPLICA_STATUS": ("ADMIN SHOW REPLICA STATUS FROM db1.tbl1",),
    "ADMIN_SHOW_TABLET_STATUS": ("ADMIN SHOW TABLET STATUS FROM my_cloud_table",),
    "ADMIN_SKIP_COMMITTED_TRANSACTION": (
        "ADMIN SKIP COMMITTED TRANSACTION 10001 REASON 'lost files'",
    ),
    "ALTER_AI_PROVIDER": ('ALTER AI PROVIDER openai SET ("model" = "text-embedding-3-large")',),
    "ALTER_DATABASE": ("ALTER DATABASE example_db SET DATA QUOTA 10995116277760B",),
    "ALTER_LOAD": ("ALTER LOAD FOR test_db.label1 PROPERTIES ('priority'='HIGHEST')",),
    "ALTER_MATERIALIZED_VIEW": (
        "ALTER MATERIALIZED VIEW lo_mv1 RENAME lo_mv1_new_name",
        "ALTER MATERIALIZED VIEW order_mv ACTIVE",
    ),
    "ALTER_PIPE": ('ALTER PIPE user_behavior_replica SET ("AUTO_INGEST" = "FALSE")',),
    "ALTER_RESOURCE": (
        'ALTER RESOURCE \'hive0\' SET PROPERTIES ("hive.metastore.uris" = "thrift://h:9083")',
    ),
    "ALTER_RESOURCE_GROUP": (
        "ALTER RESOURCE GROUP rg1 ADD (user='root', query_type in ('select'))",
    ),
    "ALTER_ROUTINE_LOAD": (
        'ALTER ROUTINE LOAD FOR example_tbl PROPERTIES ("desired_concurrent_number" = "5")',
    ),
    "ALTER_STORAGE_VOLUME": ('ALTER STORAGE VOLUME my_s3_volume SET ("enabled" = "false")',),
    "ALTER_SYSTEM": ('ALTER SYSTEM ADD FOLLOWER "x.x.x.x:9010"',),
    "ALTER_TABLE": ('ALTER TABLE example_db.my_table SET ("default.replication_num" = "2")',),
    "ALTER_TASK": ("ALTER TASK etl_task SUSPEND",),
    "ALTER_USER": ("ALTER USER 'jack' IDENTIFIED BY '123456'",),
    "ALTER_VIEW": ("ALTER VIEW example_db.example_view (c1, c2) AS SELECT k1, k2 FROM t",),
    "ANALYZE_PROFILE": ("ANALYZE PROFILE FROM '7a4d0a28-bd1c-11ee-8b36-0242ac110002'",),
    "ANALYZE_TABLE": ("ANALYZE TABLE tbl_name", "ANALYZE FULL TABLE tbl_name(c1, c2)"),
    "BACKUP": (
        'BACKUP SNAPSHOT example_db.snapshot_label1 TO example_repo PROPERTIES ("type" = "full")',
    ),
    "BROKER_LOAD": (
        'LOAD LABEL test_db.label1 (DATA INFILE("hdfs://h:8000/f") INTO TABLE t) WITH BROKER',
    ),
    "CANCEL_ALTER_TABLE": ("CANCEL ALTER TABLE COLUMN FROM example_db.example_table",),
    "CANCEL_BACKUP": ("CANCEL BACKUP FROM example_db",),
    "CANCEL_DECOMMISSION": ('CANCEL DECOMMISSION BACKEND "host1:port", "host2:port"',),
    "CANCEL_EXPORT": ('CANCEL EXPORT WHERE queryid = "921d8f80-7c9d-11eb-9342-acde48001121"',),
    "CANCEL_LOAD": ('CANCEL LOAD WHERE LABEL = "example_label"',),
    "CANCEL_REFRESH_DICTIONARY": ("CANCEL REFRESH DICTIONARY dict_obj",),
    "CANCEL_REFRESH_MATERIALIZED_VIEW": ("CANCEL REFRESH MATERIALIZED VIEW lo_mv1",),
    "CANCEL_RESTORE": ("CANCEL RESTORE FROM example_db",),
    "CREATE_AI_PROVIDER": (
        'CREATE AI PROVIDER openai TYPE embedding PROPERTIES ("endpoint" = "https://x")',
    ),
    "CREATE_ANALYZE": ("CREATE ANALYZE ALL", "CREATE ANALYZE FULL DATABASE db_name"),
    "CREATE_DATABASE": ("CREATE DATABASE db_test", "CREATE DATABASE IF NOT EXISTS demo"),
    "CREATE_DICTIONARY": (
        "CREATE DICTIONARY dict_obj USING dict (order_uuid KEY, order_id_int VALUE)",
    ),
    "CREATE_EXTERNAL_CATALOG": (
        'CREATE EXTERNAL CATALOG hive_catalog PROPERTIES ("type" = "hive")',
    ),
    "CREATE_FILE": ('CREATE FILE "test.pem" PROPERTIES ("url" = "https://x/test.pem")',),
    "CREATE_FUNCTION": (
        'CREATE FUNCTION my_add(INT, INT) RETURNS INT PROPERTIES ("symbol" = "add")',
    ),
    "CREATE_INDEX": ("CREATE INDEX index3 ON sales_records (item_id) USING BITMAP",),
    "CREATE_MATERIALIZED_VIEW": (
        "CREATE MATERIALIZED VIEW mv1 AS SELECT k1, sum(v1) FROM t GROUP BY k1",
    ),
    "CREATE_PIPE": (
        'CREATE PIPE user_behavior_replica PROPERTIES ("AUTO_INGEST" = "TRUE") '
        "AS INSERT INTO user_behavior_replica SELECT * FROM FILES ('path' = 's3://b/f')",
    ),
    "CREATE_REPOSITORY": (
        'CREATE REPOSITORY hdfs_repo WITH BROKER ON LOCATION "hdfs://h:8000/repo"',
    ),
    "CREATE_RESOURCE": ('CREATE EXTERNAL RESOURCE "spark0" PROPERTIES ("type" = "spark")',),
    "CREATE_RESOURCE_GROUP": (
        "CREATE RESOURCE GROUP rg1 TO (user='rg1_user1') WITH ('cpu_weight' = '10')",
    ),
    "CREATE_ROLE": ("CREATE ROLE role1",),
    "CREATE_ROUTINE_LOAD": (
        "CREATE ROUTINE LOAD example_db.job1 ON example_tbl1 COLUMNS TERMINATED BY ',' "
        'FROM KAFKA ("kafka_topic" = "t")',
    ),
    "CREATE_STORAGE_VOLUME": (
        "CREATE STORAGE VOLUME my_s3_volume TYPE = S3 LOCATIONS = ('s3://b/test/')",
    ),
    "CREATE_TABLE": (
        "CREATE TABLE example_db.table_hash (k1 TINYINT, v1 CHAR(10)) ENGINE=olap "
        "DUPLICATE KEY(k1) DISTRIBUTED BY HASH(k1)",
    ),
    "CREATE_TABLE_AS_SELECT": ("CREATE TABLE order_new AS SELECT * FROM `order`",),
    "CREATE_TABLE_LIKE": ("CREATE TABLE test1.order_1 LIKE test1.orders",),
    "CREATE_USER": ("CREATE USER 'jack' IDENTIFIED BY '123456'", "CREATE USER 'jack'@'%'"),
    "CREATE_VIEW": ("CREATE VIEW example_db.example_view (k1, v1) AS SELECT c1, v1 FROM t",),
    "DELETE": ("DELETE FROM my_table PARTITION p1 WHERE k1 = 3", "DELETE FROM t WHERE a = 1"),
    "DELETE_BACKEND_BLACKLIST": ("DELETE BACKEND BLACKLIST 10001",),
    "DELETE_SQLBLACKLIST": ("DELETE SQLBLACKLIST 3, 4",),
    "DESCRIBE": ("DESC example_table", "DESCRIBE example_db.example_table ALL"),
    "DESC_AI_PROVIDER": ("DESC AI PROVIDER openai",),
    "DESC_STORAGE_VOLUME": ("DESC STORAGE VOLUME my_s3_volume",),
    "DROP_AI_PROVIDER": ("DROP AI PROVIDER openai",),
    "DROP_ANALYZE": ("DROP ANALYZE 266030",),
    "DROP_CATALOG": ("DROP CATALOG hive1",),
    "DROP_DATABASE": ("DROP DATABASE db_test", "DROP DATABASE IF EXISTS db_test FORCE"),
    "DROP_DICTIONARY": ("DROP DICTIONARY dict_obj CACHE",),
    "DROP_FILE": ('DROP FILE "ca.pem" properties("catalog" = "kafka")',),
    "DROP_FUNCTION": ("DROP FUNCTION my_add(INT, INT)",),
    "DROP_INDEX": ("DROP INDEX index_name ON example_db.table_name",),
    "DROP_MATERIALIZED_VIEW": ("DROP MATERIALIZED VIEW order_mv1",),
    "DROP_PIPE": ("DROP PIPE user_behavior_replica",),
    "DROP_REPOSITORY": ("DROP REPOSITORY `oss_repo`",),
    "DROP_RESOURCE": ("DROP RESOURCE 'spark0'",),
    "DROP_RESOURCE_GROUP": ("DROP RESOURCE GROUP rg1",),
    "DROP_ROLE": ("DROP ROLE role1",),
    "DROP_SNAPSHOT": ("DROP SNAPSHOT backup1 ON example_repo",),
    "DROP_STATS": ("DROP STATS tbl_name",),
    "DROP_STORAGE_VOLUME": ("DROP STORAGE VOLUME my_s3_volume",),
    "DROP_TABLE": ("DROP TABLE my_table", "DROP TABLE IF EXISTS example_db.my_table FORCE"),
    "DROP_TASK": ("DROP TASK `ctas`",),
    "DROP_USER": ("DROP USER 'jack'@'192.%'",),
    "DROP_VIEW": ("DROP VIEW IF EXISTS example_db.example_view",),
    "EXECUTE_AS": ("EXECUTE AS test2 WITH NO REVERT",),
    "EXPLAIN": ("EXPLAIN SELECT 1", "EXPLAIN VERBOSE SELECT a FROM t"),
    "EXPLAIN_ANALYZE": (
        "EXPLAIN ANALYZE SELECT * FROM t",
        "EXPLAIN ANALYZE INSERT INTO t SELECT 1",
    ),
    "EXPORT": ('EXPORT TABLE testTbl TO "s3a://s3-package/export/" WITH BROKER ("k" = "v")',),
    "GRANT": (
        "GRANT SELECT ON *.* TO 'jack'@'%'",
        "GRANT SELECT ON TABLE db1.tbl1 TO ROLE role1",
        "GRANT role1 TO USER 'jack'@'%'",
    ),
    "INSERT": ("INSERT INTO test VALUES (1, 2)", "INSERT OVERWRITE test SELECT * FROM test2"),
    "INSTALL_PLUGIN": ('INSTALL PLUGIN FROM "/home/users/starrocks/auditdemo.zip"',),
    "KILL": ("KILL 1", "KILL QUERY 'a3b6d0e1-7c4b-11ee-8b36-0242ac110002'", "KILL CONNECTION 12"),
    "KILL_ANALYZE": ("KILL ANALYZE 266030",),
    "PAUSE_ROUTINE_LOAD": ("PAUSE ROUTINE LOAD FOR example_db.example_tbl1_ordertest1",),
    "RECOVER": ("RECOVER DATABASE example_db", "RECOVER PARTITION p1 FROM example_tbl"),
    "REFRESH_CONNECTIONS": ("REFRESH CONNECTIONS", "REFRESH CONNECTIONS FORCE"),
    "REFRESH_DICTIONARY": ("REFRESH DICTIONARY dict_obj",),
    "REFRESH_EXTERNAL_TABLE": ("REFRESH EXTERNAL TABLE hive1.hive_db.hive_table",),
    "REFRESH_MATERIALIZED_VIEW": (
        "REFRESH MATERIALIZED VIEW lo_mv1",
        "REFRESH MATERIALIZED VIEW lo_mv1 WITH SYNC MODE",
    ),
    "RESTORE": (
        "RESTORE SNAPSHOT example_db.snapshot_label1 FROM example_repo ON (backup_tbl) "
        'PROPERTIES ("backup_timestamp" = "2018-05-04-16-45-08")',
    ),
    "RESUME_ROUTINE_LOAD": ("RESUME ROUTINE LOAD FOR example_db.example_tbl1_ordertest1",),
    "RETRY_FILE": ("ALTER PIPE user_behavior_replica RETRY ALL",),
    "REVOKE": (
        "REVOKE SELECT ON TABLE sr_member FROM USER 'jack'@'192.%'",
        "REVOKE role1 FROM USER 'jack'@'%'",
    ),
    "SELECT": ("SELECT 1", "select * from big_table"),
    "SELECT_CTE": ("WITH x AS (SELECT 1 AS a) SELECT * FROM x",),
    "SELECT_DISTINCT": ("select distinct tiny_column from big_table limit 2",),
    "SELECT_EXCEPT_MINUS": ("SELECT id FROM t1 EXCEPT SELECT id FROM t2",),
    "SELECT_EXCLUDE": ("SELECT * EXCLUDE (name, email) FROM test_table",),
    "SELECT_GROUP_BY": ("SELECT k1, COUNT(*) FROM t GROUP BY k1",),
    "SELECT_HAVING": (
        "select tiny_column, sum(short_column) from small_table group by tiny_column "
        "having sum(short_column) = 1",
    ),
    "SELECT_INTERSECT": ("SELECT id FROM t1 INTERSECT SELECT id FROM t2",),
    "SELECT_JOIN": ("SELECT lhs.id, rhs.c2 FROM t lhs, t rhs WHERE lhs.id = rhs.parent",),
    "SELECT_LIMIT": ("select tiny_column from big_table limit 10",),
    "SELECT_OFFSET": (
        "select varchar_column from big_table order by varchar_column limit 3 offset 1",
    ),
    "SELECT_ORDER_BY": ("select * from big_table order by tiny_column, short_column desc",),
    "SELECT_PIVOT": ("SELECT * FROM t1 PIVOT (SUM(c1) FOR c2 IN (1, 2, 3))",),
    "SELECT_UNION": ("SELECT id FROM select1 UNION ALL SELECT id FROM select2",),
    "SELECT_WHERE_operator": ("SELECT * FROM t WHERE a BETWEEN 1 AND 10",),
    "SELECT_alias": ("select tiny_column as name, int_column as sex from big_table",),
    "SELECT_subquery": (
        "SELECT name FROM employees WHERE salary = (SELECT MAX(salary) FROM employees)",
    ),
    "SET": ("SET time_zone = 'Asia/Shanghai'", "SET GLOBAL exec_mem_limit = 137438953472"),
    "SET_CATALOG": ("SET CATALOG hive_metastore",),
    "SET_DEFAULT_AI_PROVIDER": ("SET openai AS DEFAULT AI PROVIDER",),
    "SET_DEFAULT_ROLE": ("SET DEFAULT ROLE db_admin, user_admin TO test",),
    "SET_DEFAULT_STORAGE_VOLUME": ("SET my_s3_volume AS DEFAULT STORAGE VOLUME",),
    "SET_PASSWORD": ("SET PASSWORD = PASSWORD('123456')", "SET PASSWORD FOR 'jack'@'%' = '*6BB'"),
    "SET_ROLE": ("SET ROLE db_admin", "SET ROLE ALL EXCEPT user_admin"),
    "SHOW_AI_PROVIDERS": ("SHOW AI PROVIDERS",),
    "SHOW_ALTER": ("SHOW ALTER TABLE COLUMN",),
    "SHOW_ALTER_MATERIALIZED_VIEW": ("SHOW ALTER MATERIALIZED VIEW FROM example_db",),
    "SHOW_ANALYZE_JOB": ("SHOW ANALYZE JOB",),
    "SHOW_ANALYZE_STATUS": ("SHOW ANALYZE STATUS",),
    "SHOW_AUTHENTICATION": ("SHOW AUTHENTICATION", "SHOW ALL AUTHENTICATION"),
    "SHOW_BACKENDS": ("SHOW BACKENDS",),
    "SHOW_BACKEND_BLACKLIST": ("SHOW BACKEND BLACKLIST",),
    "SHOW_BACKUP": ("SHOW BACKUP FROM example_db",),
    "SHOW_BROKER": ("SHOW BROKER",),
    "SHOW_CATALOGS": ("SHOW CATALOGS LIKE 'iceberg%'",),
    "SHOW_COMPUTE_NODES": ("SHOW COMPUTE NODES",),
    "SHOW_CREATE_CATALOG": ("SHOW CREATE CATALOG hive_catalog_hms",),
    "SHOW_CREATE_DATABASE": ("show create database zj_test",),
    "SHOW_CREATE_FUNCTION": ("SHOW CREATE FUNCTION default_db.python_add(BIGINT)",),
    "SHOW_CREATE_MATERIALIZED_VIEW": ("SHOW CREATE MATERIALIZED VIEW lo_mv1",),
    "SHOW_CREATE_TABLE": ("SHOW CREATE TABLE example_db.example_table",),
    "SHOW_CREATE_VIEW": ("SHOW CREATE VIEW example_db.example_view",),
    "SHOW_DATA": ("SHOW DATA", "SHOW DATA FROM example_db.test"),
    "SHOW_DATABASES": ("SHOW DATABASES", "SHOW DATABASES FROM hive1"),
    "SHOW_DELETE": ("SHOW DELETE FROM database",),
    "SHOW_DICTIONARY": ("SHOW DICTIONARY", "SHOW DICTIONARY dict_obj"),
    "SHOW_DYNAMIC_PARTITION_TABLES": ("SHOW DYNAMIC PARTITION TABLES FROM db_test",),
    "SHOW_EXPORT": ("SHOW EXPORT", "SHOW EXPORT FROM example_db WHERE STATE = 'FINISHED'"),
    "SHOW_FILE": ("SHOW FILE FROM example_db",),
    "SHOW_FRONTENDS": ("SHOW FRONTENDS",),
    "SHOW_FULL_COLUMNS": ("SHOW FULL COLUMNS FROM tbl",),
    "SHOW_FUNCTIONS": ("SHOW FULL FUNCTIONS IN testDb LIKE 'year%'", "SHOW BUILTIN FUNCTIONS"),
    "SHOW_GRANTS": ("SHOW GRANTS", "SHOW GRANTS FOR ROLE example_role"),
    "SHOW_INDEX": ("SHOW INDEX FROM example_db.table_name",),
    "SHOW_LOAD": ("SHOW LOAD", "SHOW LOAD FROM example_db WHERE LABEL LIKE '%null%'"),
    "SHOW_MATERIALIZED_VIEW": ("SHOW MATERIALIZED VIEWS FROM example_db",),
    "SHOW_META": ("SHOW STATS META",),
    "SHOW_PARTITIONS": ("SHOW TEMPORARY PARTITIONS FROM test.site_access",),
    "SHOW_PIPES": ("SHOW PIPES", "SHOW PIPES FROM mydatabase WHERE NAME = 'p'"),
    "SHOW_PLUGINS": ("SHOW PLUGINS",),
    "SHOW_PROC": ("show proc '/current_queries'",),
    "SHOW_PROCESSLIST": ("SHOW PROCESSLIST", "SHOW FULL PROCESSLIST"),
    "SHOW_PROFILELIST": ("SHOW PROFILELIST LIMIT 5",),
    "SHOW_PROPERTY": ("SHOW PROPERTY", "SHOW PROPERTY FOR 'jack' LIKE 'max_user_connections'"),
    "SHOW_REPOSITORIES": ("SHOW REPOSITORIES",),
    "SHOW_RESOURCES": ("SHOW RESOURCES",),
    "SHOW_RESOURCE_GROUP": ("SHOW RESOURCE GROUPS ALL", "SHOW RESOURCE GROUP rg1"),
    "SHOW_RESTORE": ("SHOW RESTORE FROM example_db",),
    "SHOW_ROLES": ("SHOW ROLES",),
    "SHOW_ROUTINE_LOAD": ("SHOW ROUTINE LOAD FOR example_db.example_job", "SHOW ALL ROUTINE LOAD"),
    "SHOW_ROUTINE_LOAD_TASK": (
        'SHOW ROUTINE LOAD TASK WHERE JobName = "example_tbl_ordertest"',
    ),
    "SHOW_RUNNING_QUERIES": ("SHOW RUNNING QUERIES",),
    "SHOW_SNAPSHOT": ("SHOW SNAPSHOT ON example_repo",),
    "SHOW_SQLBLACKLIST": ("SHOW SQLBLACKLIST",),
    "SHOW_STORAGE_VOLUMES": ("SHOW STORAGE VOLUMES",),
    "SHOW_TABLES": ("show tables from example_db", "SHOW TABLES"),
    "SHOW_TABLET": ("SHOW TABLET FROM test_show_tablet partition(p20210103)", "SHOW TABLET 10000"),
    "SHOW_TABLE_STATUS": ("SHOW TABLE STATUS", 'SHOW TABLE STATUS FROM db LIKE "%t%"'),
    "SHOW_TRANSACTION": ("SHOW TRANSACTION WHERE ID=4005",),
    "SHOW_USAGE_RESOURCE_GROUPS": ("SHOW USAGE RESOURCE GROUPS",),
    "SHOW_USERS": ("SHOW USERS",),
    "SHOW_VARIABLES": ("SHOW VARIABLES LIKE 'max_connections'", "SHOW GLOBAL VARIABLES"),
    "SHOW_WARNINGS": ("SHOW WARNINGS", "SHOW ERRORS LIMIT 10"),
    "SPARK_LOAD": (
        'LOAD LABEL example_db.label1 (DATA INFILE("hdfs://h:8000/f") INTO TABLE `my_table`) '
        "WITH RESOURCE 'spark0'",
    ),
    "STOP_ROUTINE_LOAD": ("STOP ROUTINE LOAD FOR example_db.example_tbl1_ordertest1",),
    "SUBMIT_TASK": ("SUBMIT TASK etl0 AS CREATE TABLE tbl1 AS SELECT * FROM src_tbl",),
    "SUSPEND_or_RESUME_PIPE": ("ALTER PIPE user_behavior_replica SUSPEND",),
    "SYNC": ("SYNC",),
    "TRANSLATE_TRINO": ("TRANSLATE TRINO SELECT * FROM products WHERE name = 'Dell'",),
    "TRUNCATE_TABLE": ("TRUNCATE TABLE example_db.tbl",),
    "UNINSTALL_PLUGIN": ("UNINSTALL PLUGIN auditdemo",),
    "UPDATE": ("UPDATE Employees SET Salary = 5000 WHERE EmployeeID = 1",),
    "USE": ("USE default_catalog.example_db",),
    "prepared_statement": (
        "PREPARE select_by_id_stmt FROM 'SELECT * FROM demo.users WHERE id = ?'",
        "SET @id1 = 1",
        "EXECUTE select_by_id_stmt USING @id1",
        "DEALLOCATE PREPARE select_by_id_stmt",
        "DROP PREPARE select_by_id_stmt",
    ),
}

# MySQL 兼容、不在 StarRocks 文档页里的语句：同样不能按对话进入模型。
MYSQL_EXAMPLES: tuple[str, ...] = (
    "BEGIN",
    "BEGIN WORK",
    "START TRANSACTION",
    "COMMIT",
    "ROLLBACK WORK",
    "CALL p(1)",
    "MERGE INTO t USING s ON t.a = s.a WHEN MATCHED THEN DELETE",
    "RENAME TABLE old_name TO new_name",
    "UPSERT INTO t VALUES (1)",
    "REPLACE INTO t VALUES (1)",
    "LOCK TABLES t READ",
    "UNLOCK TABLES",
    "LOAD DATA INFILE '/tmp/x' INTO TABLE t",
)


def test_every_statement_doc_page_has_examples() -> None:
    assert set(EXAMPLES) == STARROCKS_STATEMENT_DOCS - NOT_A_STATEMENT


def test_registry_covers_exactly_the_documented_statements() -> None:
    documented = {
        doc for form in SQL_STATEMENT_FORMS for doc in form.docs if not doc.startswith("mysql:")
    }
    assert documented == STARROCKS_STATEMENT_DOCS - NOT_A_STATEMENT


@pytest.mark.parametrize(
    "sql",
    [sql for examples in EXAMPLES.values() for sql in examples] + list(MYSQL_EXAMPLES),
)
def test_every_documented_statement_is_recognized_as_sql(sql: str) -> None:
    assert classify_sql_text(sql) is SqlTextKind.SQL


def test_keywords_come_only_from_the_registry() -> None:
    assert {form.keyword for form in SQL_STATEMENT_FORMS} == SQL_STATEMENT_KEYWORDS
    firsts = {sql.split()[0].upper() for examples in EXAMPLES.values() for sql in examples}
    assert firsts <= SQL_STATEMENT_KEYWORDS


# 命中语句签名、却不是完整语句：不执行、不进模型，提示放入 sql 代码块重发。
SQL_LIKE: tuple[str, ...] = (
    "show data for yesterday",
    "create table for this report",
    "prepare a report",
    "execute the plan",
    "drop table please now",
    "insert into the report",
    "delete from the list",
    "SELECT 'unterminated",
    "SELECT a FROM t WHERE",
    "kill query please",
    "alter table for me",
    "show create table",
    "grant select on everything",
    "grant role1, role2",
)

# 首词是语句关键字、但签名都不命中：自然语言，继续走对话。
NATURAL_LANGUAGE: tuple[str, ...] = (
    "show me the slow queries",
    "show status of my task",
    "create a dashboard",
    "analyze this",
    "analyze the logs",
    "select the best option",
    "with you all the way",
    "explain why the query is slow",
    "explain it",
    "update me when done",
    "set up alerts",
    "grant me access to the dashboard",
    "revoke it",
    "call me",
    "kill it",
    "stop the job",
    "start over",
    "cancel it",
    "refresh it",
    "load data",
    "export it",
    "lock it",
    "truncate it",
    "pause it",
    "resume work",
    "install the agent",
    "recover my password",
    "sync with me",
    "begin the job",
    "commit to the plan",
    "translate this",
    "add a chart",
    "admin help",
    "desc",
)


# 字符串与注释之外出现中文：混合消息走对话，由嵌入 SQL 检测兜底，不给“放入代码块”的提示。
MIXED_LANGUAGE: tuple[str, ...] = (
    "SHOW USERS 是什么",
    "SELECT a FROM t 为什么慢",
    "show data 昨天的",
)


@pytest.mark.parametrize("text", MIXED_LANGUAGE)
def test_mixed_language_is_conversation(text: str) -> None:
    assert classify_sql_text(text) is SqlTextKind.CONVERSATION


@pytest.mark.parametrize("text", SQL_LIKE)
def test_signature_without_complete_shape_is_sql_like(text: str) -> None:
    assert classify_sql_text(text) is SqlTextKind.SQL_LIKE


@pytest.mark.parametrize("text", NATURAL_LANGUAGE)
def test_keyword_led_natural_language_is_conversation(text: str) -> None:
    assert classify_sql_text(text) is SqlTextKind.CONVERSATION
