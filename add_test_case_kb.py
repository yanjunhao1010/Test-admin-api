# -*- coding: utf-8 -*-
"""测试用例知识库建表迁移脚本（幂等）。

运行：在 Fast_api_project 目录下执行
    python add_test_case_kb.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.comment import MySqlconection, yaml_data

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'src', 'config', 'config.yml'
)


def get_db_handler():
    data = yaml_data.BaseYamlData(CONFIG_PATH).read_yaml()
    return MySqlconection.DatabaseHandler(
        data['mysql']['host'], data['mysql']['user'], data['mysql']['password'], data['mysql']['database']
    )


SQL = """
CREATE TABLE IF NOT EXISTS test_case_kb (
  id              INT UNSIGNED AUTO_INCREMENT PRIMARY KEY COMMENT '知识条目ID',
  title           VARCHAR(255)  NOT NULL COMMENT '知识条目标题（如“登录-验证码失效场景”）',
  project_id      INT UNSIGNED  NOT NULL COMMENT '所属三级项目ID，关联 test_case_project.id',
  module          VARCHAR(255)  DEFAULT '' COMMENT '模块',
  function_point  VARCHAR(512)  DEFAULT '' COMMENT '功能点',
  priority        TINYINT       DEFAULT 2 COMMENT '优先级 1低 2中 3高',
  pre_condition   TEXT          COMMENT '前置条件',
  steps           TEXT          COMMENT '测试步骤',
  expected_result TEXT          COMMENT '预期结果',
  kb_type         TINYINT       DEFAULT 1 COMMENT '类型 1通用用例 2测试要点 3典型坑点/边界',
  tags            VARCHAR(512)  DEFAULT '' COMMENT '标签，逗号分隔，用于检索',
  scene_desc      TEXT          COMMENT '适用场景/补充说明（对应原 remark 弱项，鼓励填写）',
  source_type     TINYINT       DEFAULT 0 COMMENT '来源 0人工 1来自test_cases 2来自ai_case_drafts',
  source_ref_id   INT           DEFAULT NULL COMMENT '来源记录ID（test_cases.id / ai_case_drafts.id）',
  scope           VARCHAR(8)    DEFAULT 'global' COMMENT 'global=团队共享 personal=个人',
  owner_id        INT           DEFAULT NULL COMMENT '作者/归属用户ID',
  ref_count       INT           DEFAULT 0 COMMENT '被引用次数（复用统计）',
  version         INT           DEFAULT 1 COMMENT '版本号',
  status          TINYINT       DEFAULT 1 COMMENT '状态 1启用 0禁用',
  is_del          TINYINT       DEFAULT 0 COMMENT '逻辑删除 0未删 1已删',
  creator_id      INT           DEFAULT NULL COMMENT '创建者用户ID',
  create_time     DATETIME      DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  update_time     DATETIME      DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
  KEY idx_project (project_id),
  KEY idx_kb_type (kb_type),
  KEY idx_scope (scope),
  KEY idx_source (source_type, source_ref_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='测试用例知识库';
"""


if __name__ == '__main__':
    with get_db_handler() as db:
        db.execute_update(SQL)
    print('test_case_kb 表已确保存在（如已存在则跳过）')
