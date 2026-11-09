# -*- coding: utf-8 -*-
"""
研发流程「关联引用」扩展迁移：节点附件表 + 引用类型扩展（幂等，可重复跑）。
dev/prod 同库，跑一次两边都生效。
运行：.venv/Scripts/python.exe add_dev_process_attachments.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'src'))
from comment import MySqlconection, yaml_data

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'src', 'config', 'config.yml')


def dbh():
    d = yaml_data.BaseYamlData(CONFIG_PATH).read_yaml()['mysql']
    return MySqlconection.DatabaseHandler(d['host'], d['user'], d['password'], d['database'])


def main():
    with dbh() as db:
        # 1) 节点附件表（关联引用里的「文件上传」）
        db.execute_update("""
        CREATE TABLE IF NOT EXISTS dev_process_attachments (
          id           INT UNSIGNED AUTO_INCREMENT PRIMARY KEY COMMENT '附件ID',
          process_id   INT UNSIGNED NOT NULL COMMENT '关联 dev_process.id',
          node_index   TINYINT      NOT NULL COMMENT '关联节点序号',
          file_name    VARCHAR(255) NOT NULL COMMENT '原始文件名',
          file_path    VARCHAR(500) NOT NULL COMMENT '服务器存储相对路径(uploads/devprocess/xxx)',
          file_size    INT UNSIGNED DEFAULT 0 COMMENT '文件大小(字节)',
          file_ext     VARCHAR(20)  DEFAULT '' COMMENT '扩展名(不含点)',
          content_type VARCHAR(150) DEFAULT '' COMMENT 'MIME类型',
          note         VARCHAR(300) DEFAULT '' COMMENT '附件说明',
          create_by    INT UNSIGNED DEFAULT NULL COMMENT '上传人 user.id',
          create_time  DATETIME     DEFAULT CURRENT_TIMESTAMP COMMENT '上传时间',
          is_del       TINYINT      DEFAULT 0 COMMENT '删除位',
          KEY idx_proc_node (process_id, node_index)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='研发流程节点附件'
        """)
        print('[OK] dev_process_attachments')

        # 2) 引用类型扩展：新增 4=需求分析(ai_requirements)
        #    用 INFORMATION_SCHEMA 判断，避免重复跑时重复 ALTER
        cur = db.execute_single_query(
            "SELECT COLUMN_COMMENT FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='dev_process_node_links' AND COLUMN_NAME='ref_type'"
        )
        comment = (cur or {}).get('COLUMN_COMMENT') or ''
        if '需求分析' not in comment:
            db.execute_update(
                "ALTER TABLE dev_process_node_links MODIFY COLUMN ref_type TINYINT DEFAULT 1 "
                "COMMENT '1测试用例 2测试任务/缺陷 3需求变更 4需求分析(ai_requirements)'"
            )
            print('[OK] dev_process_node_links.ref_type COMMENT 已扩展')
        else:
            print('[SKIP] dev_process_node_links.ref_type COMMENT 已是最新')

    print('== 附件表迁移完成 ==')


if __name__ == '__main__':
    main()
