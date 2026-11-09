# -*- coding: utf-8 -*-
"""研发流程菜单权限落库（幂等）：permissions 插入 menu:devprocess + 给 admin 授权。"""
import os
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'src'))
from comment import MySqlconection, yaml_data

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'src', 'config', 'config.yml')


def dbh():
    d = yaml_data.BaseYamlData(CONFIG_PATH).read_yaml()['mysql']
    return MySqlconection.DatabaseHandler(d['host'], d['user'], d['password'], d['database'])


def main():
    with dbh() as db:
        print('-- permissions 列 --')
        for r in db.execute_query('SHOW FULL COLUMNS FROM permissions'):
            print(' ', r['Field'], r['Type'])
        print('-- menu:task 样例 --')
        for r in db.execute_query("SELECT * FROM permissions WHERE code='menu:task'"):
            print(' ', r)
        print('-- role_permissions 列 --')
        for r in db.execute_query('SHOW FULL COLUMNS FROM role_permissions'):
            print(' ', r['Field'], r['Type'])

        exist = db.execute_query("SELECT id FROM permissions WHERE code='menu:devprocess'")
        if not exist:
            db.execute_update(
                "INSERT INTO permissions (name, code, type, path, sort) VALUES ('研发流程','menu:devprocess',1,'/devprocess',30)"
            )
            pid = db.execute_query("SELECT LAST_INSERT_ID() AS lid")[0]['lid']
            print('[OK] 插入 permissions id=', pid)
        else:
            pid = exist[0]['id']
            print('[SKIP] permissions 已存在 id=', pid)

        admin = db.execute_query("SELECT id FROM roles WHERE code='admin'")
        admin_id = admin[0]['id'] if admin else 1
        linked = db.execute_query(
            "SELECT COUNT(*) c FROM role_permissions WHERE role_id=%s AND permission_id=%s", (admin_id, pid)
        )[0]['c']
        if not linked:
            db.execute_update(
                "INSERT INTO role_permissions (role_id, permission_id) VALUES (%s,%s)", (admin_id, pid)
            )
            print('[OK] 给 admin(role_id=%s) 授权' % admin_id)
        else:
            print('[SKIP] admin 已授权')

    print('== 菜单权限落库完成 ==')


if __name__ == '__main__':
    main()
