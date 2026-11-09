"""AI 生成用例 · 历史记录建表脚本（幂等，可重复执行）

新增两张表：
1. ai_case_gen_history     —— 每次「AI 生成用例」成功落一条（含输入参数快照、模型、耗时、条数）
2. ai_case_gen_history_item —— 每条生成的用例一条（关联 history_id）

用途：在 AI 生成用例页提供「生成历史记录」，点击可在右侧回看当时生成的用例列表，
即使当时未「加入 AI 用例库」也不会丢失。

用法：python add_ai_case_gen_history.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.comment import MySqlconection, yaml_data

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, 'src', 'config', 'config.yml')


TABLES = {}

TABLES['ai_case_gen_history'] = """
CREATE TABLE IF NOT EXISTS ai_case_gen_history (
  id             INT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '生成历史ID',
  batch_id       VARCHAR(40)  NOT NULL COMMENT '生成批次号（一次生成唯一）',
  run_by         INT          NOT NULL COMMENT '生成人用户ID，关联 users.id',
  project_id     INT          NULL COMMENT '目标项目ID(test_case_project三级节点)，可空',
  project_name   VARCHAR(255) NULL COMMENT '目标项目名称(快照)，便于项目被删后仍可读',
  model_id       INT          NULL COMMENT '生成所用模型ID，关联 ai_models.id',
  model_name     VARCHAR(100) NULL COMMENT '模型名(快照)',
  module         VARCHAR(255) NULL COMMENT '模块',
  function_point VARCHAR(512) NULL COMMENT '功能点',
  requirement     TEXT         NULL COMMENT '需求说明(快照)',
  case_count     INT          NOT NULL DEFAULT 0 COMMENT '本次生成用例条数',
  elapsed_ms     INT          NULL COMMENT '生成耗时(毫秒)',
  attempt        TINYINT      NOT NULL DEFAULT 1 COMMENT '第几次尝试(含校验失败重试)',
  create_time    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '生成时间',
  PRIMARY KEY (id),
  KEY idx_user_time (run_by, create_time),
  KEY idx_batch (batch_id)
) COMMENT 'AI生成用例历史记录（每次生成落一条，供历史回看）'
"""

TABLES['ai_case_gen_history_item'] = """
CREATE TABLE IF NOT EXISTS ai_case_gen_history_item (
  id             INT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '历史用例条目ID',
  history_id     INT          NOT NULL COMMENT '关联 ai_case_gen_history.id',
  seq            INT          NOT NULL DEFAULT 0 COMMENT '生成时的顺序号',
  module         VARCHAR(255) NULL COMMENT '模块',
  function_point VARCHAR(512) NULL COMMENT '功能点',
  priority       TINYINT      NOT NULL DEFAULT 2 COMMENT '优先级 1-低 2-中 3-高（与 test_cases 对齐）',
  case_type      VARCHAR(32)  NULL COMMENT '用例类型：正向/异常/边界/权限/兼容',
  design_method  VARCHAR(64)  NULL COMMENT '设计方法',
  pre_condition  TEXT         NULL COMMENT '前置条件',
  case_steps     TEXT         NULL COMMENT '操作步骤',
  expected_result TEXT        NULL COMMENT '预期结果',
  remark         TEXT         NULL COMMENT '备注',
  confidence     DECIMAL(3,2) NULL COMMENT 'AI自评置信度 0~1',
  dup_case_id    INT          NULL COMMENT '与库内疑似重复的用例ID',
  create_time    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  PRIMARY KEY (id),
  KEY idx_history (history_id)
) COMMENT 'AI生成用例历史明细（每条生成用例一条）'
"""


def main():
    data = yaml_data.BaseYamlData(CONFIG_PATH).read_yaml()
    db = MySqlconection.DatabaseHandler(
        data['mysql']['host'], data['mysql']['user'],
        data['mysql']['password'], data['mysql']['database'],
    )
    db.connect()

    for name, ddl in TABLES.items():
        try:
            db.execute_update(ddl)
            print(f'OK   建表 {name}')
        except Exception as e:
            print(f'ERR  建表 {name} 失败: {e}')
            raise

    db.close()
    print('\nAI 生成用例历史表创建完成 ✔')


if __name__ == '__main__':
    main()
