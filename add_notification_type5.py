# -*- coding: utf-8 -*-
"""notifications 表 type 字段注释扩展：新增 5=流程节点派送。

幂等：可重复执行。执行前先读当前 COMMENT，已包含目标文案则跳过。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.comment import MySqlconection, yaml_data  # noqa: E402

NEW_COMMENT = '类型：1任务派送 2缺陷派送 3定时任务完成 4AI生成完成 5流程节点派送'


def main():
    cfg_path = os.path.join('src', 'config', 'config.yml')
    cfg = yaml_data.BaseYamlData(cfg_path).read_yaml()
    db = MySqlconection.DatabaseHandler(
        cfg['mysql']['host'], cfg['mysql']['user'],
        cfg['mysql']['password'], cfg['mysql']['database'],
    )
    cols = db.execute_query("SHOW FULL COLUMNS FROM `notifications` WHERE Field='type'")
    if not cols:
        print('ERR: notifications 表不存在或无 type 字段')
        return
    old = cols[0]['Comment']
    print('当前 COMMENT:', old)
    if old == NEW_COMMENT:
        print('SKIP: 已是目标注释，无需修改')
        return
    db.execute_update(
        "ALTER TABLE `notifications` MODIFY COLUMN `type` tinyint NOT NULL COMMENT %s",
        (NEW_COMMENT,),
    )
    now = db.execute_query("SHOW FULL COLUMNS FROM `notifications` WHERE Field='type'")[0]['Comment']
    print('修改后 COMMENT:', now)
    assert now == NEW_COMMENT, 'COMMENT 修改未生效'
    print('OK')


if __name__ == '__main__':
    main()
