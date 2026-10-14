"""支持永久寄件授权，保留历史期限、次数、口令和关联 ID。"""

from tortoise.transactions import in_transaction


async def migrate():
    # 016 已由通知分支使用；017 单独放宽期限和次数为空，避免后续合并时迁移重名。
    # SQLite 通过事务重建移除 NOT NULL，同时约束期限和次数必须成对配置。
    async with in_transaction() as conn:
        columns = {row["name"]: row for row in await conn.execute_query_dict("PRAGMA table_info(deliverycode)")}
        if not columns["expires_at"]["notnull"] and not columns["max_uploads"]["notnull"]:
            return
        sequences = await conn.execute_query_dict("SELECT seq FROM sqlite_sequence WHERE name = 'deliverycode'")
        sequence = sequences[0]["seq"] if sequences else 0
        await conn.execute_query('''
            CREATE TABLE deliverycode_permanent (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code_value VARCHAR(64) NULL UNIQUE,
                auth_version INT NOT NULL DEFAULT 1,
                name VARCHAR(100) NOT NULL,
                note VARCHAR(2000) NOT NULL DEFAULT '',
                tags JSON NOT NULL DEFAULT '[]',
                expires_at TIMESTAMP NULL,
                max_uploads INT NULL,
                used_count INT NOT NULL DEFAULT 0,
                reserved_count INT NOT NULL DEFAULT 0,
                enabled INT NOT NULL DEFAULT 1,
                deleted INT NOT NULL DEFAULT 0,
                created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                CHECK ((expires_at IS NULL AND max_uploads IS NULL) OR
                       (expires_at IS NOT NULL AND max_uploads IS NOT NULL AND max_uploads >= 1))
            )
        ''')
        await conn.execute_query('''
            INSERT INTO deliverycode_permanent (
                id, code_value, auth_version, name, note, tags, expires_at,
                max_uploads, used_count, reserved_count, enabled, deleted, created_at
            ) SELECT id, code_value, auth_version, name, note, tags, expires_at,
                max_uploads, used_count, reserved_count, enabled, deleted, created_at
            FROM deliverycode
        ''')
        await conn.execute_query("DROP TABLE deliverycode")
        await conn.execute_query("ALTER TABLE deliverycode_permanent RENAME TO deliverycode")
        # 不能因历史行曾被删除而复用旧 ID，避免错误接回历史收件关联。
        await conn.execute_query("UPDATE sqlite_sequence SET seq = MAX(seq, ?) WHERE name = 'deliverycode'", [sequence])
