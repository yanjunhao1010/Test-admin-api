"""通知中心 · 新建 notifications 表（幂等，可重复执行）

用途：为测试管理后台增加站内通知中心，覆盖 4 类派送/完成通知
      —— 任务派送、缺陷派送、接口定时任务完成、AI 生成任务完成。

字段口径（按 2026-09-28 方案拍板）：
- type：1任务派送 2缺陷派送 3定时任务完成 4AI生成完成
- receiver_uid：接收人用户ID（行级权限依据，admin 不豁免，沿用项目铁律）
- sender_uid：触发人用户ID（可选）
- biz_type/biz_id/link：业务对象定位，点击通知跳对应页面

用法：python add_notifications_table.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.comment import MySqlconection, yaml_data

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, 'src', 'config', 'config.yml')

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS `notifications` (
  `id`            int unsigned NOT NULL AUTO_INCREMENT COMMENT '通知ID',
  `type`          tinyint NOT NULL COMMENT '类型：1任务派送 2缺陷派送 3定时任务完成 4AI生成完成',
  `receiver_uid`  int NOT NULL COMMENT '接收人用户ID（行级权限依据，admin 不豁免）',
  `sender_uid`    int DEFAULT NULL COMMENT '触发人用户ID（可选）',
  `title`         varchar(200) NOT NULL COMMENT '通知标题',
  `content`       varchar(500) DEFAULT '' COMMENT '通知正文',
  `biz_type`      varchar(32) DEFAULT '' COMMENT '业务类型：task/defect/schedule/ai',
  `biz_id`        varchar(64) DEFAULT '' COMMENT '业务对象ID（task_id/缺陷所属task_id/schedule_id/ai_report_id/batch_id）',
  `link`          varchar(255) DEFAULT '' COMMENT '前端跳转路由，如 /task/detail/12',
  `is_read`       tinyint NOT NULL DEFAULT 0 COMMENT '是否已读：0未读 1已读',
  `create_time`   datetime NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  PRIMARY KEY (`id`),
  KEY `idx_receiver_read` (`receiver_uid`,`is_read`),
  KEY `idx_create_time` (`create_time`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='站内通知中心'
"""


def main():
    data = yaml_data.BaseYamlData(CONFIG_PATH).read_yaml()
    db = MySqlconection.DatabaseHandler(
        data['mysql']['host'], data['mysql']['user'],
        data['mysql']['password'], data['mysql']['database'],
    )
    db.execute_update(CREATE_SQL)
    print('OK   已确保 notifications 表存在')

    chk = db.execute_query(
        "SELECT TABLE_NAME AS t FROM information_schema.TABLES "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'notifications'")
    cols = db.execute_query(
        "SELECT COLUMN_NAME AS c FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'notifications' "
        "ORDER BY ORDINAL_POSITION")
    print('校验 notifications 表存在 = %s，列数 = %d' % (bool(chk), len(cols)))
    print('DONE  notifications 建表迁移完成')


if __name__ == '__main__':
    main()
