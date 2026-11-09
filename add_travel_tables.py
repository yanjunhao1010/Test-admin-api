# -*- coding: utf-8 -*-
"""智能出行迁移脚本（建表 + 菜单权限三件套落库）。

一次运行，幂等：
- CREATE TABLE IF NOT EXISTS travel_plans
- permissions 插入「智能出行 / 行程规划 / 我的行程」三节点（按 code 判重）
- role_permissions 给全部角色授权（按 (role_id, permission_id) 判重）

运行：在项目根目录执行
    python add_travel_tables.py
依赖：src/config/config.yml 中的 mysql 配置 + pymysql
"""
import pymysql
import yaml

CONFIG_PATH = 'src/config/config.yml'


def main():
    cfg = yaml.safe_load(open(CONFIG_PATH, encoding='utf-8'))['mysql']
    conn = pymysql.connect(
        host=cfg['host'], user=cfg['user'], password=cfg['password'],
        database=cfg['database'], charset='utf8mb4',
        cursorclass=pymysql.cursors.DictCursor,
    )
    cur = conn.cursor()
    try:
        # 1) 行程方案表
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS travel_plans (
                id            BIGINT        PRIMARY KEY AUTO_INCREMENT COMMENT '主键',
                plan_name     VARCHAR(128)  NOT NULL                  COMMENT '行程名称（如：杭州2日亲子游）',
                destination   VARCHAR(64)   NOT NULL                  COMMENT '目的地城市',
                days          INT           NOT NULL DEFAULT 1        COMMENT '行程天数',
                preference    JSON          NULL                      COMMENT '偏好标签（亲子/美食/自然/人文/citywalk/穷游/舒适等）',
                budget_level  VARCHAR(16)   DEFAULT ''                COMMENT '预算档位（经济/舒适/高端）',
                itinerary     JSON          NOT NULL                  COMMENT '行程 JSON（每日编排：时段/地点/类型/说明/推荐语/参考花费）',
                total_cost    DECIMAL(10,2) NULL                      COMMENT '预估总花费（元，参考价）',
                poi_count     INT           NULL                      COMMENT '行程内地点数量',
                create_by     BIGINT        NOT NULL                  COMMENT '创建人 uid',
                create_time   DATETIME      DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
                is_del        TINYINT       NOT NULL DEFAULT 0        COMMENT '软删标记 0正常 1删除'
            ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='智能出行-行程方案表'
            """
        )
        print('travel_plans 表就绪')

        # 2) 菜单权限三节点（按 code 判重，幂等）
        menus = [
            # (parent_id, code, type, name, path, icon, sort, component)
            (0, 'menu:travel', 1, '智能出行', '/travel', 'compass', 8, 'Layout'),
            (None, 'menu:travel:planner', 1, '行程规划', '/travel/planner', '', 1, '@/views/travel/planner/index'),
            (None, 'menu:travel:myplans', 1, '我的行程', '/travel/myplans', '', 2, '@/views/travel/myplans/index'),
        ]
        cur.execute("SELECT id FROM permissions WHERE code = 'menu:travel'")
        row = cur.fetchone()
        if row:
            parent_id = row['id']
            print('menu:travel 已存在 id=%s，跳过父节点' % parent_id)
        else:
            cur.execute(
                'INSERT INTO permissions (parent_id, code, type, name, path, icon, sort, component) '
                'VALUES (0, %s, %s, %s, %s, %s, %s, %s)',
                ('menu:travel', 1, '智能出行', '/travel', 'compass', 8, 'Layout'),
            )
            parent_id = cur.lastrowid
            print('menu:travel 已插入 id=%s' % parent_id)

        for code, name, path, component, sort in [
            ('menu:travel:planner', '行程规划', '/travel/planner', '@/views/travel/planner/index', 1),
            ('menu:travel:myplans', '我的行程', '/travel/myplans', '@/views/travel/myplans/index', 2),
        ]:
            cur.execute('SELECT id FROM permissions WHERE code = %s', (code,))
            existed = cur.fetchone()
            if existed:
                print('%s 已存在 id=%s，跳过' % (code, existed['id']))
                continue
            cur.execute(
                'INSERT INTO permissions (parent_id, code, type, name, path, icon, sort, component) '
                'VALUES (%s, %s, %s, %s, %s, %s, %s, %s)',
                (parent_id, code, 1, name, path, '', sort, component),
            )
            print('%s 已插入 id=%s' % (code, cur.lastrowid))

        # 3) 全部角色授权（按 (role_id, permission_id) 判重，幂等）
        cur.execute("SELECT id FROM permissions WHERE code LIKE 'menu:travel%'")
        perm_ids = [r['id'] for r in cur.fetchall()]
        cur.execute('SELECT id FROM roles')
        role_ids = [r['id'] for r in cur.fetchall()]
        granted = 0
        for rid in role_ids:
            for pid in perm_ids:
                cur.execute(
                    'SELECT id FROM role_permissions WHERE role_id = %s AND permission_id = %s',
                    (rid, pid),
                )
                if cur.fetchone():
                    continue
                cur.execute(
                    'INSERT INTO role_permissions (role_id, permission_id) VALUES (%s, %s)',
                    (rid, pid),
                )
                granted += 1
        print('role_permissions 新增授权 %d 条（角色 %d 个 × 权限 %d 个）' % (granted, len(role_ids), len(perm_ids)))

        conn.commit()
    finally:
        conn.close()
    print('迁移完成 OK')


if __name__ == '__main__':
    main()
