"""幂等脚本：为自动化模块三张表补齐每个字段的中文 COMMENT。

- 先 SHOW FULL COLUMNS 读取字段真实 Type/Null/Default/Extra，据此生成
  `MODIFY COLUMN ... COMMENT '...'`，避免手抄 DDL 出错或破坏现有定义。
- 已存在注释的字段（如 extracts）自动跳过，可重复运行。
运行：python add_column_comments.py
"""
import os
import sys
import pymysql

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from src.comment import yaml_data

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'src', 'config', 'config.yml'
)

# 每个字段的中文注释映射（覆盖三张表全部字段）
COMMENTS = {
    'api_interfaces': {
        'id': '主键自增ID',
        'name': '接口名称',
        'method': '请求方法(GET/POST/PUT/DELETE)',
        'url': '请求地址',
        'headers': '请求头(JSON字符串)',
        'params': '查询参数(JSON字符串)',
        'body': '请求体(JSON字符串)',
        'assert_type': '断言类型(status_code/json_path等)',
        'expected_value': '断言期望值',
        'enabled': '是否启用(1启用0停用)',
        'create_by': '创建人用户ID',
        'create_time': '创建时间',
        'update_time': '更新时间',
        'extracts': '响应变量提取规则(JSON数组，用于接口间上下文传递)',
    },
    'api_schedules': {
        'id': '主键自增ID',
        'name': '计划名称',
        'interface_ids': '关联接口ID列表(JSON数组)',
        'cron_expr': 'cron执行周期表达式(标准5段)',
        'enabled': '是否启用(1启用0停用)',
        'last_run_time': '上次执行时间',
        'next_run_time': '下次执行时间',
        'create_by': '创建人用户ID',
        'create_time': '创建时间',
        'update_time': '更新时间',
    },
    'api_executions': {
        'id': '主键自增ID',
        'schedule_id': '关联执行计划ID',
        'interface_id': '关联接口ID',
        'run_time': '执行时间',
        'http_status': 'HTTP响应状态码',
        'elapsed_ms': '执行耗时(毫秒)',
        'success': '是否成功(1成功0失败)',
        'response_snippet': '响应内容摘要',
        'error_msg': '错误信息',
        'assertion_result': '断言结果',
    },
}


def esc(v):
    return str(v).replace("'", "\\'")


def main():
    data = yaml_data.BaseYamlData(CONFIG_PATH).read_yaml()
    mysql = data['mysql']
    conn = pymysql.connect(
        host=mysql['host'],
        port=int(mysql.get('port', 3306)),
        user=mysql['user'],
        password=mysql['password'],
        database=mysql['database'],
        charset=mysql.get('charset', 'utf8mb4'),
    )
    try:
        with conn.cursor() as cur:
            for tbl, cmap in COMMENTS.items():
                cur.execute(f"SHOW FULL COLUMNS FROM {tbl}")
                rows = cur.fetchall()
                # 列顺序: Field, Type, Collation, Null, Key, Default, Extra, Privileges, Comment
                cols = {}
                for r in rows:
                    cols[r[0]] = (r[1], r[3], r[5], r[6], r[8])

                for field, comment in cmap.items():
                    if field not in cols:
                        print(f'  SKIP {tbl}.{field}: 字段不存在')
                        continue
                    ctype, null, default, extra, old_comment = cols[field]
                    if old_comment:
                        print(f'  SKIP {tbl}.{field}: 已有注释 [{old_comment}]')
                        continue
                    parts = [f"MODIFY COLUMN {field} {ctype}"]
                    parts.append("NOT NULL" if null == 'NO' else "NULL")
                    if default is not None:
                        parts.append(f"DEFAULT '{esc(default)}'")
                    if extra:
                        parts.append(extra)
                    parts.append(f"COMMENT '{comment}'")
                    sql = f"ALTER TABLE {tbl} " + " ".join(parts)
                    print('  EXEC:', sql)
                    cur.execute(sql)
                    conn.commit()
                    print(f'    OK {tbl}.{field}')

            print('--- 验证（SHOW FULL COLUMNS）---')
            for tbl in COMMENTS:
                cur.execute(f"SHOW FULL COLUMNS FROM {tbl}")
                for r in cur.fetchall():
                    status = r[8] if r[8] else '(仍为空!)'
                    print(f'  {tbl}.{r[0]}: {status}')
    finally:
        conn.close()


if __name__ == '__main__':
    main()
