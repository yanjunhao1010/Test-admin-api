"""给 api_interfaces 增加 verify_ssl 列（是否校验SSL证书），幂等可重复运行。
约定：新建/变更表字段必须带中文 COMMENT。
"""
import sys

sys.path.insert(0, r'D:\pyproject\Fast_api_project')

from src.comment import MySqlconection, yaml_data


def main():
    data = yaml_data.BaseYamlData(
        r'D:\pyproject\Fast_api_project\src\config\config.yml'
    ).read_yaml()
    db = MySqlconection.DatabaseHandler(
        data['mysql']['host'], data['mysql']['user'],
        data['mysql']['password'], data['mysql']['database'],
    )
    with db:
        cols = [r['Field'] for r in db.execute_query('SHOW FULL COLUMNS FROM api_interfaces')]
        if 'verify_ssl' in cols:
            print('verify_ssl 已存在，跳过添加')
        else:
            db.execute_update(
                "ALTER TABLE api_interfaces "
                "ADD COLUMN verify_ssl TINYINT NOT NULL DEFAULT 1 "
                "COMMENT '是否校验SSL证书(1校验0跳过,内网/自签证书端点可关)'"
            )
            print('已添加 verify_ssl 列')
        # 校验
        row = db.execute_query(
            "SHOW FULL COLUMNS FROM api_interfaces WHERE Field='verify_ssl'"
        )[0]
        print('验证: Field=%s Type=%s Comment=%s' % (row['Field'], row['Type'], row['Comment']))


if __name__ == '__main__':
    main()
