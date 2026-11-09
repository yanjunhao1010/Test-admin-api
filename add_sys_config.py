# -*- coding: utf-8 -*-
"""幂等创建 sys_config（系统配置键值表）并写入微信推送开关默认值。

运行：
    python add_sys_config.py
仅创建表 + 写入默认配置项；若已存在则跳过，可重复执行。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.comment import MySqlconection, yaml_data  # noqa: E402

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'src', 'config', 'config.yml'
)


def main():
    data = yaml_data.BaseYamlData(CONFIG_PATH).read_yaml()
    cfg = data['mysql']
    with MySqlconection.DatabaseHandler(
        cfg['host'], cfg['user'], cfg['password'], cfg['database']
    ) as db:
        db.execute_update(
            """
            CREATE TABLE IF NOT EXISTS `sys_config` (
                `id`         INT          NOT NULL AUTO_INCREMENT,
                `cfg_key`    VARCHAR(64)  NOT NULL                COMMENT '配置项键（唯一）',
                `cfg_value`  VARCHAR(512) NOT NULL DEFAULT ''     COMMENT '配置项值',
                `remark`     VARCHAR(255) DEFAULT ''              COMMENT '说明',
                `updated_at` DATETIME     DEFAULT CURRENT_TIMESTAMP
                                            ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
                PRIMARY KEY (`id`),
                UNIQUE KEY `uk_key` (`cfg_key`)
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='系统配置键值表'
            """
        )
        # 写入微信推送开关默认值（已存在则跳过）
        db.execute_update(
            "INSERT IGNORE INTO `sys_config` (cfg_key, cfg_value, remark) "
            "VALUES (%s, %s, %s)",
            ('wechat_push_enabled', '1', '接口自动化执行告警→企业微信推送总开关：1开 0关'),
        )
        rows = db.execute_query('SELECT COUNT(*) AS c FROM `sys_config`')
        cols = db.execute_query('SHOW COLUMNS FROM `sys_config`')
        print('sys_config 表就绪，列数 =', len(cols))
        print('当前配置项条数 =', rows[0]['c'] if rows else 0)
        kv = db.execute_query(
            "SELECT cfg_key, cfg_value FROM sys_config WHERE cfg_key='wechat_push_enabled'"
        )
        print('wechat_push_enabled =', kv[0]['cfg_value'] if kv else 'MISSING')


if __name__ == '__main__':
    main()
