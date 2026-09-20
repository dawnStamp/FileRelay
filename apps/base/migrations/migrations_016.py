"""新增寄件通知发件箱，升级不回发历史文件通知。"""

from tortoise import connections


async def migrate():
    # 建表和索引都支持重试；队列仅保存元数据，实际凭据在独立后台配置中维护。
    conn = connections.get("default")
    await conn.execute_script('''
        CREATE TABLE IF NOT EXISTS deliverynotification (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id VARCHAR(36) NOT NULL,
            channel VARCHAR(16) NOT NULL,
            revision VARCHAR(36) NOT NULL,
            payload JSON NOT NULL,
            status VARCHAR(16) NOT NULL DEFAULT 'pending',
            attempts INT NOT NULL DEFAULT 0,
            next_attempt_at TIMESTAMP NOT NULL,
            last_error VARCHAR(100) NOT NULL DEFAULT '',
            is_test INT NOT NULL DEFAULT 0,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            sent_at TIMESTAMP NULL,
            UNIQUE(event_id, channel)
        );
        CREATE INDEX IF NOT EXISTS idx_delivery_notification_due
            ON deliverynotification(status, next_attempt_at);
    ''')
