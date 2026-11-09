"""AI 生成用例 · 历史明细项补 draft_id 列（幂等，可重复执行）

给 ai_case_gen_history_item 增加 draft_id 列，关联 ai_case_drafts.id，
用于「已加入 AI 用例库」状态的可靠判定（直接挂外键，不再靠内容猜测匹配），
并实现「已入库的用例不可重复添加」。

用法：python add_ai_case_gen_history_item_draft.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.comment import MySqlconection, yaml_data

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, 'src', 'config', 'config.yml')


def main():
    data = yaml_data.BaseYamlData(CONFIG_PATH).read_yaml()
    db = MySqlconection.DatabaseHandler(
        data['mysql']['host'], data['mysql']['user'],
        data['mysql']['password'], data['mysql']['database'],
    )
    db.connect()

    # 幂等加列：MySQL 8 不支持「ADD COLUMN IF NOT EXISTS」，用信息_schema 探存在
    try:
        rows = db.execute_query(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = 'ai_case_gen_history_item' "
            "AND column_name = 'draft_id'")
        if rows:
            print('OK   列 draft_id 已存在，跳过')
        else:
            db.execute_update(
                'ALTER TABLE ai_case_gen_history_item '
                'ADD COLUMN draft_id INT NULL COMMENT \'关联 ai_case_drafts.id；非空表示该历史用例已加入 AI 用例库\'')
            db.execute_update(
                'ALTER TABLE ai_case_gen_history_item '
                'ADD KEY idx_draft (draft_id)')
            print('OK   列 draft_id 已添加')
    except Exception as e:
        print('ERR  加列失败: %s' % e)
        raise

    db.close()
    print('\nai_case_gen_history_item.draft_id 就绪 ✔')


if __name__ == '__main__':
    main()
