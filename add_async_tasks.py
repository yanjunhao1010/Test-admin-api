# -*- coding: utf-8 -*-
"""AI 需求分析异步任务表（幂等，可重复执行）

背景：需求分析三阶段串行，实测 9 个模块 14 批耗时 249 秒，最坏单批 300 秒，
同步请求必然让前端「一直卡在分析中」。方案第三节明确「不做 Celery/Redis」，
用轻量任务表 + 后台线程 + 前端轮询实现「立即返回 + 进度可见」。
"""
import os
import sys

import pymysql
import yaml

BASE = os.path.dirname(os.path.abspath(__file__))
CFG = os.path.join(BASE, 'src', 'config', 'config.yml')


def get_conn():
    cfg = yaml.safe_load(open(CFG, encoding='utf-8'))['mysql']
    return pymysql.connect(
        host=cfg['host'], user=cfg['user'], password=cfg['password'],
        database=cfg['database'], charset='utf8mb4', autocommit=False)


DDL = [
    # 异步任务主表：一个需求的一次分析 = 一个 task
    ("""CREATE TABLE IF NOT EXISTS ai_async_tasks (
  task_id       varchar(64)  NOT NULL COMMENT '任务ID(uuid hex)',
  requirement_id int unsigned NOT NULL COMMENT '需求文档ID',
  stages        varchar(64)  DEFAULT 'decompose,extract,audit' COMMENT '本次要跑的阶段，逗号分隔',
  status        tinyint      NOT NULL DEFAULT 0 COMMENT '0排队中 1运行中 2已完成 3失败',
  cur_stage     varchar(32)  DEFAULT '' COMMENT '当前阶段：decompose/extract/audit',
  progress      tinyint      NOT NULL DEFAULT 0 COMMENT '总进度百分比 0-100',
  cur_batch     varchar(255) DEFAULT '' COMMENT '当前批次描述，如「3/14 · 首页-创作者个人主页」',
  done_batch    int          NOT NULL DEFAULT 0 COMMENT '已完成批次数',
  total_batch   int          NOT NULL DEFAULT 0 COMMENT '总批次数',
  done_stage    int          NOT NULL DEFAULT 0 COMMENT '已完成阶段数',
  total_stage   int          NOT NULL DEFAULT 0 COMMENT '总阶段数',
  error_msg     text         COMMENT '失败原因（status=3 时可读）',
  result        mediumtext   COMMENT '完成后的产物摘要(JSON)：各阶段 summary + coverage',
  model_id      int          DEFAULT NULL COMMENT '本次使用的模型ID',
  run_by        int          NOT NULL DEFAULT 0 COMMENT '发起人uid',
  elapsed_ms    int          NOT NULL DEFAULT 0 COMMENT '总耗时(毫秒)',
  create_time   datetime     NOT NULL COMMENT '创建时间',
  update_time   datetime     DEFAULT NULL COMMENT '更新时间',
  finish_time   datetime     DEFAULT NULL COMMENT '完成时间',
  PRIMARY KEY (task_id),
  KEY idx_req (requirement_id),
  KEY idx_status (status),
  KEY idx_runby (run_by)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='AI需求分析异步任务(阶段三R8)'"""),

    # 进度事件流：便于排查「卡在哪一步」，也供前端展示时间线
    ("""CREATE TABLE IF NOT EXISTS ai_async_task_logs (
  id          bigint unsigned NOT NULL AUTO_INCREMENT COMMENT '自增主键',
  task_id     varchar(64) NOT NULL COMMENT '任务ID',
  level       varchar(8)  NOT NULL DEFAULT 'info' COMMENT 'info/warn/error',
  stage       varchar(32) DEFAULT '' COMMENT '所属阶段',
  message     varchar(500) DEFAULT '' COMMENT '可读进度描述',
  create_time datetime NOT NULL COMMENT '创建时间',
  PRIMARY KEY (id),
  KEY idx_task (task_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='AI需求分析异步任务进度流水'"""),
]


def main():
    conn = get_conn()
    cur = conn.cursor()
    for ddl in DDL:
        cur.execute(ddl)
        name = ddl.split('`')[1] if '`ai_' in ddl else ddl.split('(')[1].strip().split()[0]
        print('  [OK] %s' % name)
    conn.commit()

    print('\n=== 校验 ===')
    for t in ('ai_async_tasks', 'ai_async_task_logs'):
        cur.execute('SHOW COLUMNS FROM `%s`' % t)
        cols = [c[0] for c in cur.fetchall()]
        cur.execute('SHOW INDEX FROM `%s`' % t)
        idx = sorted({(x[2], x[4]) for x in cur.fetchall()})
        cur.execute('SELECT COUNT(*) FROM `%s`' % t)
        cnt = cur.fetchone()[0]
        print('  %s: %d 列, %d 行' % (t, len(cols), cnt))
        print('     列 = %s' % cols)
        print('     索引 = %s' % idx)

    # COMMENT 抽查（项目铁律：建表必须写中文 COMMENT）
    cur.execute("SELECT TABLE_NAME, TABLE_COMMENT FROM information_schema.TABLES "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME LIKE 'ai_async%'")
    for r in cur.fetchall():
        print('  表注释 %s -> %s' % r)
    cur.execute("SELECT COLUMN_NAME, COLUMN_COMMENT FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'ai_async_tasks' "
                "AND (COLUMN_COMMENT IS NULL OR COLUMN_COMMENT = '')")
    missing = cur.fetchall()
    print('  无注释列 = %s' % (missing if missing else '无（全部已写中文注释）'))
    conn.close()
    print('\n完成。')


if __name__ == '__main__':
    main()