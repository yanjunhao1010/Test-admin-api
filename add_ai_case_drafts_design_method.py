"""AI 工作台 · 给 ai_case_drafts 增加 design_method 列（幂等，可重复执行）

背景：生成用例的内置提示词已要求标注每条用例采用的黑盒设计方法
（等价类划分法/边界值分析法/错误推测法/判定表法/正交实验法/场景法/用户经验法），
case-gen/save 也会回传该字段，但 ai_case_drafts 表原先没有该列，导致：
1）设计方法无法持久化；2）AI 用例库列表无法展示「用例设计方法名」。
本脚本补齐该列，便于入库与列表展示。

用法：python add_ai_case_drafts_design_method.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.comment import MySqlconection, yaml_data

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, 'src', 'config', 'config.yml')

# 新增列定义（建表规范：必须带中文 COMMENT）
COLUMNS = [
    ("design_method",
     "VARCHAR(64) NULL COMMENT "
     "'用例采用的设计方法（黑盒设计法之一：等价类划分法/边界值分析法/错误推测法/"
     "判定表法/正交实验法/场景法/用户经验法）'"),
]


def main():
    data = yaml_data.BaseYamlData(CONFIG_PATH).read_yaml()
    db = MySqlconection.DatabaseHandler(
        data['mysql']['host'], data['mysql']['user'],
        data['mysql']['password'], data['mysql']['database'],
    )
    db.connect()

    for col, defn in COLUMNS:
        try:
            db.execute_update(
                'ALTER TABLE ai_case_drafts ADD COLUMN %s %s' % (col, defn))
            print('OK   已加列 ai_case_drafts.%s' % col)
        except Exception as e:
            msg = str(e)
            if 'Duplicate column' in msg or '1060' in msg:
                print('SKIP ai_case_drafts.%s 已存在' % col)
            else:
                print('ERR  ai_case_drafts.%s: %s' % (col, msg))
                raise
    print('DONE  ai_case_drafts 设计方法列迁移完成')


if __name__ == '__main__':
    main()
