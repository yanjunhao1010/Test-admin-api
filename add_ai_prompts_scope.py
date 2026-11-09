"""提示词个人专属改造 · 加 scope 列（幂等，可重复执行）

- ai_prompts 增加 scope 字段：global=全局默认(所有人共用) / personal=个人专属(owner=create_by)
- 历史残留行全部标 global，确保不影响现网（现网本就无生效自定义版，全部走内置默认）
"""
import pymysql
import yaml

cfg = yaml.safe_load(open('src/config/config.yml', encoding='utf-8'))['mysql']
db_name = cfg['database']
conn = pymysql.connect(
    host=cfg['host'], user=cfg['user'], password=cfg['password'],
    database=db_name, charset='utf8mb4',
)
cur = conn.cursor()

# 幂等：列不存在才加
cur.execute(
    "SELECT 1 FROM information_schema.COLUMNS "
    "WHERE TABLE_SCHEMA=%s AND TABLE_NAME='ai_prompts' AND COLUMN_NAME='scope'",
    (db_name,),
)
if not cur.fetchone():
    cur.execute(
        "ALTER TABLE ai_prompts "
        "ADD COLUMN scope VARCHAR(8) NOT NULL DEFAULT 'global' "
        "COMMENT 'global=全局默认(所有人共用) personal=个人专属(owner=create_by，仅本人可见可用)' "
        "AFTER is_del"
    )
    print('已添加 scope 列')
else:
    print('scope 列已存在，跳过 ADD COLUMN')

# 历史残留行（scope 为空/空串）统一标 global
cur.execute("UPDATE ai_prompts SET scope='global' WHERE scope='' OR scope IS NULL")
print('已确保历史行 scope=global，影响行数:', cur.rowcount)

conn.commit()
cur.close()
conn.close()
print('OK')
