"""AI 工作台 · 放宽 ai_reports.biz_id 列长度（幂等，可重复执行）

背景：ai_reports.biz_id 原为 VARCHAR(64)，但「用例质量体检」的 biz_id 存的是
「本次体检的用例 id 列表」（`','.join(str(id))`），单次最多 50 条用例：
- Excel 导入体检：行号 1..50 拼接 = 140 字符
- 用例库体检：真实用例 id 50 个（3 位以上）拼接可超 200 字符
→ 触发 MySQL 1406 `Data too long for column 'biz_id' at row 1`，体检报告入库失败。

两个入口（/ai/case-review 与 /ai/case-review/upload）写的是同一套拼接逻辑，
故直接放宽列长度（VARCHAR(500) 足以容纳 50 条 6 位 id），语义不变、历史数据不动。

用法：python add_ai_reports_biz_id_len.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.comment import MySqlconection, yaml_data

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, 'src', 'config', 'config.yml')

# 目标列定义（保留原注释：建表规范要求中文 COMMENT）
NEW_DEF = ("VARCHAR(500) NULL COMMENT "
           "'业务对象ID（批次号/接口id/需求id等）'")


def main():
    data = yaml_data.BaseYamlData(CONFIG_PATH).read_yaml()
    db = MySqlconection.DatabaseHandler(
        data['mysql']['host'], data['mysql']['user'],
        data['mysql']['password'], data['mysql']['database'],
    )
    db.connect()

    cur = db.execute_query(
        "SELECT CHARACTER_MAXIMUM_LENGTH AS L FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'ai_reports' "
        "AND COLUMN_NAME = 'biz_id'")
    cur_len = cur[0]['L'] if cur else None
    print('当前 ai_reports.biz_id 长度 = %s' % cur_len)

    if cur_len is not None and cur_len >= 500:
        print('SKIP ai_reports.biz_id 已是 VARCHAR(%s)，无需变更' % cur_len)
        print('DONE  ai_reports biz_id 迁移完成')
        return

    db.execute_update('ALTER TABLE ai_reports MODIFY COLUMN biz_id %s' % NEW_DEF)
    print('OK   已放宽 ai_reports.biz_id -> VARCHAR(500)')

    chk = db.execute_query(
        "SELECT CHARACTER_MAXIMUM_LENGTH AS L FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'ai_reports' "
        "AND COLUMN_NAME = 'biz_id'")
    print('校验 ai_reports.biz_id 长度 = %s' % (chk[0]['L'] if chk else None))
    print('DONE  ai_reports biz_id 迁移完成')


if __name__ == '__main__':
    main()
