# -*- coding: utf-8 -*-
"""用例管理-「AI补充测试点」按钮权限节点落库（幂等）。

一次运行：
- permissions 插入 testcase:ai-augment 按钮权限（父 menu:testcase，type=2），按 code 判重；
- role_permissions 给全部角色授权（按 (role_id, permission_id) 判重）。

运行：在项目根目录执行
    python add_testcase_ai_augment_perm.py
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
        # 父节点 menu:testcase（应已由 init_rbac.py 建立）
        cur.execute("SELECT id FROM permissions WHERE code = 'menu:testcase'")
        row = cur.fetchone()
        if not row:
            print('menu:testcase 不存在，请先初始化 RBAC')
            return
        parent_id = row['id']

        # 按钮权限 testcase:ai-augment
        code = 'testcase:ai-augment'
        cur.execute('SELECT id FROM permissions WHERE code = %s', (code,))
        existed = cur.fetchone()
        if existed:
            perm_id = existed['id']
            print('%s 已存在 id=%s，跳过插入' % (code, perm_id))
        else:
            cur.execute(
                'INSERT INTO permissions (parent_id, code, type, name, path, icon, sort, component) '
                'VALUES (%s, %s, %s, %s, %s, %s, %s, %s)',
                (parent_id, code, 2, 'AI补充测试点', '', '', 6, ''),
            )
            perm_id = cur.lastrowid
            print('%s 已插入 id=%s' % (code, perm_id))

        # 全部角色授权（按 (role_id, permission_id) 判重，幂等）
        cur.execute('SELECT id FROM roles')
        role_ids = [r['id'] for r in cur.fetchall()]
        granted = 0
        for rid in role_ids:
            cur.execute(
                'SELECT id FROM role_permissions WHERE role_id = %s AND permission_id = %s',
                (rid, perm_id),
            )
            if cur.fetchone():
                continue
            cur.execute(
                'INSERT INTO role_permissions (role_id, permission_id) VALUES (%s, %s)',
                (rid, perm_id),
            )
            granted += 1
        print('role_permissions 新增授权 %d 条（角色 %d 个）' % (granted, len(role_ids)))
        conn.commit()
    finally:
        conn.close()
    print('OK')


if __name__ == '__main__':
    main()
