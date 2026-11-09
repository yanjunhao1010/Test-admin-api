"""给 api_interfaces 增加接口变更感知字段（F7），幂等可重复运行。
约定：新建/变更表字段必须带中文 COMMENT。

- dataset_sig：接口签名(method+url+params+body 的 MD5)，用于检测接口定义改动。
- dataset_stale：接口签名变化时标记(1=已有场景可能过期)，重新生成/编辑场景后清零。
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
        alters = [
            ("dataset_sig",
             "ADD COLUMN dataset_sig VARCHAR(64) NOT NULL DEFAULT '' "
             "COMMENT '接口签名(method+url+params+body的MD5)，用于检测接口改动是否使已有场景过期'"),
            ("dataset_stale",
             "ADD COLUMN dataset_stale TINYINT NOT NULL DEFAULT 0 "
             "COMMENT '接口签名变化时标记(1=场景可能过期)，由重新生成/编辑场景后清零'"),
        ]
        for name, ddl in alters:
            if name in cols:
                print(f'{name} 已存在，跳过添加')
                continue
            db.execute_update(f'ALTER TABLE api_interfaces {ddl}')
            print(f'已添加 {name} 列')
        # 校验
        for name in ('dataset_sig', 'dataset_stale'):
            row = db.execute_query(
                f"SHOW FULL COLUMNS FROM api_interfaces WHERE Field='{name}'"
            )[0]
            print('验证: Field=%s Type=%s Comment=%s' % (row['Field'], row['Type'], row['Comment']))


if __name__ == '__main__':
    main()
