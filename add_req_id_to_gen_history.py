# -*- coding: utf-8 -*-
"""给 ai_case_gen_history 增加 requirement_id（关联 ai_requirements）。

背景：需求分析页「据此生成用例」产出的记录，此前只能靠 run_by 在
「AI生成用例」页查看，无法按需求维度回看。加这一列后，
需求列表可以新增「用例生成记录」按钮，直接看该需求下所有生成批次。

幂等：列已存在则跳过，可重复执行。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.routers.ai_workbench import get_db_handler  # noqa: E402


def main():
    with get_db_handler() as db:
        exists = db.execute_query(
            "SHOW COLUMNS FROM ai_case_gen_history LIKE 'requirement_id'")
        if exists:
            print('[跳过] ai_case_gen_history.requirement_id 已存在')
        else:
            # 存量记录无来源需求，一律留 NULL（不影响仅按 run_by 的旧查询）
            db.execute_update(
                'ALTER TABLE ai_case_gen_history '
                'ADD COLUMN requirement_id int NULL DEFAULT NULL '
                "COMMENT '关联需求文档ID（ai_requirements.id），非需求分析页生成的记录为 NULL' "
                'AFTER run_by')
            print('[完成] 已新增 ai_case_gen_history.requirement_id')

        # 配套索引：需求列表按 requirement_id 查历史会走这个索引
        idx = db.execute_query("SHOW INDEX FROM ai_case_gen_history "
                               "WHERE Key_name = 'idx_req_id'")
        if not idx:
            db.execute_update(
                'ALTER TABLE ai_case_gen_history ADD INDEX idx_req_id (requirement_id)')
            print('[完成] 已新增索引 idx_req_id')
        else:
            print('[跳过] 索引 idx_req_id 已存在')

        # 回读确认
        cols = db.execute_query('SHOW COLUMNS FROM ai_case_gen_history')
        names = [r['Field'] for r in cols]
        print('\n当前列:', names)
        assert 'requirement_id' in names, 'requirement_id 未生效'
        print('校验通过')


if __name__ == '__main__':
    main()