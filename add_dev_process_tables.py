# -*- coding: utf-8 -*-
"""
研发流程模块：建表 + seed 11 节点（幂等，可重复跑）。
dev/prod 同库，跑一次两边都生效。
运行：.venv/Scripts/python.exe add_dev_process_tables.py
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'src'))
from comment import MySqlconection, yaml_data

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'src', 'config', 'config.yml')


def dbh():
    d = yaml_data.BaseYamlData(CONFIG_PATH).read_yaml()['mysql']
    return MySqlconection.DatabaseHandler(d['host'], d['user'], d['password'], d['database'])


# 11 节点（测试导向交付闭环）；is_required: 用例评审/复盘归档 设为可选(可跳过)，其余必走
SEED_NODES = [
    (1, 'requirement_clarify', '需求澄清', 1),
    (2, 'requirement_review', '需求评审', 1),
    (3, 'tech_design', '技术方案', 1),
    (4, 'development_impl', '开发实现', 1),
    (5, 'case_design', '测试用例设计', 1),
    (6, 'case_review', '用例评审', 0),
    (7, 'case_execute', '测试执行', 1),
    (8, 'defect_fix', '缺陷修复', 1),
    (9, 'release', '发布上线', 1),
    (10, 'verify', '上线验证', 1),
    (11, 'retro_archive', '复盘归档', 0),
]


def main():
    with dbh() as db:
        # 1) 节点定义表
        db.execute_update("""
        CREATE TABLE IF NOT EXISTS dev_process_node_def (
          id            INT UNSIGNED AUTO_INCREMENT PRIMARY KEY COMMENT '节点定义ID',
          node_index    TINYINT      NOT NULL COMMENT '节点序号 1~11',
          node_key      VARCHAR(40)  NOT NULL COMMENT '节点标识',
          node_name     VARCHAR(60)  NOT NULL COMMENT '节点名称',
          default_role  VARCHAR(30)  DEFAULT '' COMMENT '默认负责角色code(预留字段,v1自由分配不强制使用)',
          is_required   TINYINT      DEFAULT 1 COMMENT '是否必走 1必走 0可跳过',
          sort          TINYINT      DEFAULT 0 COMMENT '排序',
          UNIQUE KEY uk_key (node_key),
          UNIQUE KEY uk_index (node_index)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='研发流程节点定义'
        """)
        print('[OK] dev_process_node_def')

        # 2) 流程实例表
        db.execute_update("""
        CREATE TABLE IF NOT EXISTS dev_process (
          id            INT UNSIGNED AUTO_INCREMENT PRIMARY KEY COMMENT '流程实例ID',
          code          VARCHAR(40)  NOT NULL COMMENT '流程编号 如 DP-20261023-001',
          title         VARCHAR(200) NOT NULL COMMENT '需求/功能名称',
          project_id    INT UNSIGNED DEFAULT NULL COMMENT '关联测试项目(test_case_project.id) 可选',
          creator_id    INT UNSIGNED DEFAULT NULL COMMENT '发起人 user.id',
          current_node  TINYINT      DEFAULT 1 COMMENT '当前所处节点序号 1~11',
          progress      TINYINT      DEFAULT 0 COMMENT '整体完成度 0~100',
          status        TINYINT      DEFAULT 0 COMMENT '0进行中 1已完成 2已终止',
          is_del        TINYINT      DEFAULT 0 COMMENT '删除位',
          create_time   DATETIME     DEFAULT CURRENT_TIMESTAMP,
          update_time   DATETIME     DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='研发流程实例'
        """)
        print('[OK] dev_process')

        # 3) 节点执行记录表
        db.execute_update("""
        CREATE TABLE IF NOT EXISTS dev_process_node (
          id            INT UNSIGNED AUTO_INCREMENT PRIMARY KEY COMMENT '节点记录ID',
          process_id    INT UNSIGNED NOT NULL COMMENT '关联 dev_process.id',
          node_index    TINYINT      NOT NULL COMMENT '节点序号 1~11',
          node_key      VARCHAR(40)  NOT NULL COMMENT '节点标识',
          node_name     VARCHAR(60)  NOT NULL COMMENT '节点名称',
          assignee_id   INT UNSIGNED DEFAULT NULL COMMENT '负责人 user.id',
          assignee_role VARCHAR(30)  DEFAULT '' COMMENT '负责人角色(自由分配,记录实际所选用户角色,可空)',
          completer_id  INT UNSIGNED DEFAULT NULL COMMENT '完成人 user.id',
          status        TINYINT      DEFAULT 0 COMMENT '0待开始 1进行中(含重开) 2已完成 3已跳过',
          started_at    DATETIME     DEFAULT NULL COMMENT '开始时间',
          finished_at   DATETIME     DEFAULT NULL COMMENT '完成时间',
          remark        VARCHAR(500) DEFAULT '' COMMENT '备注',
          reopen_count  TINYINT      DEFAULT 0 COMMENT '重开次数(受控回退累计,初始0)',
          reopened_by   INT UNSIGNED DEFAULT NULL COMMENT '最近一次重开人 user.id',
          reopened_at   DATETIME     DEFAULT NULL COMMENT '最近一次重开时间',
          reopen_reason VARCHAR(500) DEFAULT '' COMMENT '重开原因(建议关联 requirement_changes.id)',
          is_del        TINYINT      DEFAULT 0 COMMENT '删除位',
          KEY idx_process (process_id),
          KEY idx_index (node_index)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='研发流程节点执行记录'
        """)
        print('[OK] dev_process_node')

        # 4) seed 11 节点（仅当为空时）
        cnt = db.execute_query("SELECT COUNT(*) AS c FROM dev_process_node_def")[0]['c']
        if cnt == 0:
            for idx, key, name, required in SEED_NODES:
                db.execute_update(
                    "INSERT INTO dev_process_node_def (node_index, node_key, node_name, default_role, is_required, sort) "
                    "VALUES (%s,%s,%s,'',%s,%s)",
                    (idx, key, name, required, idx),
                )
            print('[OK] seed 11 节点')
        else:
            print(f'[SKIP] dev_process_node_def 已有 {cnt} 行，跳过 seed')

    print('== dev_process 建表+seed 完成 ==')


if __name__ == '__main__':
    main()
