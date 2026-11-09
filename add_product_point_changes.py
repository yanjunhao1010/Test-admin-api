# -*- coding: utf-8 -*-
"""
迁移：新建 product_point_changes 表（产品功能点 · 测试点修改历史 = 诊断的「改前锚点」）。

背景：
  product_function_points.test_points 里的测试点被「修改」时是**直接覆盖**的
  （product_points.py: update_test_point → old[index]['title'] = text → UPDATE），
  改前的文字**没有任何地方存下来**。
  没有 old_title，「新旧对比」就是假的 —— 只能拿新测试点重新体检一遍旧用例。

  这张表就是那个「改前锚点」：
    · update_test_point 每次修改写一行（old/new 标题 + 各自的完整 tp_json + 操作人）
    · 诊断端点按 (pfp_id, tp_index) 取最近一行作对比基准

⚠️ tp_index 是「修改时刻」的下标 —— 下标会随测试点删除而回缩，
   所以这张表**只作历史留痕**，不作为长期引用。

执行：在 Fast_api_project 根目录跑
  ./.venv/Scripts/python.exe add_product_point_changes.py
幂等：表已存在则跳过创建（只做结构补齐，不动数据）。
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'src'))
os.chdir(os.path.join(ROOT, 'src'))

from src.comment import MySqlconection

CONFIG_PATH = os.path.join(ROOT, 'src', 'config', 'config.yml')

TABLE = 'product_point_changes'

CREATE_SQL = """
CREATE TABLE `product_point_changes` (
  `id`                INT UNSIGNED NOT NULL AUTO_INCREMENT  COMMENT '修改记录ID',
  `pfp_id`            INT UNSIGNED NOT NULL                 COMMENT '产品功能点ID(product_function_points.id)',
  `tp_index`         INT          NOT NULL                 COMMENT '被修改的测试点在功能点内的0基下标(修改时刻,会随删除回缩)',
  `tp_ref`           VARCHAR(32)  DEFAULT NULL             COMMENT '测试点回标编号(手动新增的测试点为空)',
  `old_title`        VARCHAR(512) DEFAULT NULL             COMMENT '修改前的测试点文字',
  `new_title`        VARCHAR(512) DEFAULT NULL             COMMENT '修改后的测试点文字',
  `old_tp_json`      TEXT         DEFAULT NULL             COMMENT '修改前的测试点完整JSON(含dims/batch等)',
  `new_tp_json`      TEXT         DEFAULT NULL             COMMENT '修改后的测试点完整JSON',
  `affected_case_ids` TEXT        DEFAULT NULL             COMMENT '本次修改时关联该测试点的用例ID列表(JSON数组)',
  `diagnosis_json`   LONGTEXT     DEFAULT NULL             COMMENT '诊断结果快照(JSON,含影响判定与新旧对比,供回溯)',
  `operator_id`      INT UNSIGNED DEFAULT NULL             COMMENT '操作人用户ID(users.id)',
  `create_time`      DATETIME     DEFAULT NULL             COMMENT '创建时间',
  `is_del`           TINYINT      DEFAULT 0                COMMENT '逻辑删除:0未删 1已删',
  PRIMARY KEY (`id`),
  KEY `idx_pfp_tp` (`pfp_id`, `tp_index`),
  KEY `idx_create` (`create_time`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='产品功能点测试点修改历史(诊断的改前锚点)'
"""

# 幂等补列：(列名, DDL 片段)
EXTRA_COLUMNS = [
    ('tp_ref', "`tp_ref` VARCHAR(32) DEFAULT NULL COMMENT '测试点回标编号(手动新增的测试点为空)'"),
    ('old_title', "`old_title` VARCHAR(512) DEFAULT NULL COMMENT '修改前的测试点文字'"),
    ('new_title', "`new_title` VARCHAR(512) DEFAULT NULL COMMENT '修改后的测试点文字'"),
    ('old_tp_json', "`old_tp_json` TEXT DEFAULT NULL COMMENT '修改前的测试点完整JSON(含dims/batch等)'"),
    ('new_tp_json', "`new_tp_json` TEXT DEFAULT NULL COMMENT '修改后的测试点完整JSON'"),
    ('affected_case_ids', "`affected_case_ids` TEXT DEFAULT NULL COMMENT '本次修改时关联该测试点的用例ID列表(JSON数组)'"),
    ('diagnosis_json', "`diagnosis_json` LONGTEXT DEFAULT NULL COMMENT '诊断结果快照(JSON,含影响判定与新旧对比,供回溯)'"),
    ('operator_id', "`operator_id` INT UNSIGNED DEFAULT NULL COMMENT '操作人用户ID(users.id)'"),
    ('create_time', "`create_time` DATETIME DEFAULT NULL COMMENT '创建时间'"),
    ('is_del', "`is_del` TINYINT DEFAULT 0 COMMENT '逻辑删除:0未删 1已删'"),
]


def connect(cfg):
    """建连带重试：公网 MySQL 偶发超时，重试即通。"""
    last = None
    for i in range(3):
        try:
            d = MySqlconection.DatabaseHandler(
                cfg['mysql']['host'], cfg['mysql']['user'],
                cfg['mysql']['password'], cfg['mysql']['database'],
            )
            d._ensure_connection()  # noqa: SLF001
            return d
        except Exception as e:  # noqa: BLE001
            last = e
            print('连接失败(第%d次): %s' % (i + 1, e))
            time.sleep(2)
    raise last


def main():
    cfg = MySqlconection.load_config_cached(CONFIG_PATH)
    d = connect(cfg)
    with d:
        exist = d.execute_query(
            "SELECT TABLE_NAME FROM information_schema.TABLES "
            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s",
            (cfg['mysql']['database'], TABLE)
        )
        if exist:
            print('%s 已存在，跳过创建' % TABLE)
        else:
            d.execute_update(CREATE_SQL)
            print('%s 创建成功' % TABLE)

        # 幂等补列（表已存在但缺列时补齐）
        cols = {r['COLUMN_NAME'] for r in d.execute_query(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s",
            (cfg['mysql']['database'], TABLE)
        )}
        added = []
        for name, ddl in EXTRA_COLUMNS:
            if name not in cols:
                d.execute_update('ALTER TABLE `%s` ADD COLUMN %s' % (TABLE, ddl))
                added.append(name)
        if added:
            print('补列: %s' % ', '.join(added))
        else:
            print('列结构完整，无需补列')

        rows = d.execute_query(
            "SELECT COUNT(*) c FROM `%s`" % TABLE)
        print('当前数据行数: %s' % rows[0]['c'])


if __name__ == '__main__':
    main()
