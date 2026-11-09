import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.comment import MySqlconection, yaml_data

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'src', 'config', 'config.yml'
)

# 每个字段必须带中文 COMMENT（建表规范）。
# 用 try/except 容忍「列已存在」，可重复执行。
ALTERS = [
    # ---- api_interfaces ----
    ("api_interfaces", "asserts",
     "ALTER TABLE api_interfaces ADD COLUMN asserts TEXT "
     "COMMENT '断言规则数组(JSON)：每条含 assert_type/expected_value/enabled，全部通过才判通过'"),
    ("api_interfaces", "timeout",
     "ALTER TABLE api_interfaces ADD COLUMN timeout INT NOT NULL DEFAULT 10 "
     "COMMENT '请求超时秒数(0或空则用默认10秒)'"),
    ("api_interfaces", "retry",
     "ALTER TABLE api_interfaces ADD COLUMN retry TINYINT NOT NULL DEFAULT 0 "
     "COMMENT '失败重试次数(仅网络/超时异常重试，断言失败不重试)'"),
    ("api_interfaces", "dataset",
     "ALTER TABLE api_interfaces ADD COLUMN dataset TEXT "
     "COMMENT '数据驱动参数(JSON数组)：每行一个数据对象，执行时按行替换{{key}}多遍运行'"),
    ("api_interfaces", "group",
     "ALTER TABLE api_interfaces ADD COLUMN `group` VARCHAR(100) NOT NULL DEFAULT '' "
     "COMMENT '接口分组(按模块/业务线归类，便于列表筛选与聚合)'"),
    # ---- api_schedules ----
    ("api_schedules", "on_failure",
     "ALTER TABLE api_schedules ADD COLUMN on_failure TINYINT NOT NULL DEFAULT 1 "
     "COMMENT '前序接口失败策略(1=继续后续/2=中止后续)'"),
    # ---- api_executions ----
    ("api_executions", "case_tag",
     "ALTER TABLE api_executions ADD COLUMN case_tag VARCHAR(100) DEFAULT '' "
     "COMMENT '数据驱动用例标识(数据集行名/序号)'"),
    ("api_executions", "run_batch",
     "ALTER TABLE api_executions ADD COLUMN run_batch BIGINT DEFAULT NULL "
     "COMMENT '同一次计划执行的批次号(epoch毫秒)，同一批次的接口执行记录归属同一运行'"),
    ("api_executions", "trigger",
     "ALTER TABLE api_executions ADD COLUMN `trigger` VARCHAR(16) NOT NULL DEFAULT 'manual' "
     "COMMENT '触发方式(manual=手动执行/schedule=定时调度)'"),
]


def main():
    data = yaml_data.BaseYamlData(CONFIG_PATH).read_yaml()
    db = MySqlconection.DatabaseHandler(
        data['mysql']['host'], data['mysql']['user'],
        data['mysql']['password'], data['mysql']['database'],
    )
    db.connect()
    for table, col, sql in ALTERS:
        try:
            db.execute_update(sql)
            print(f'OK: {table}.{col} 已添加')
        except Exception as e:
            msg = str(e)
            if 'Duplicate column name' in msg or '1054' in msg:
                print(f'SKIP: {table}.{col} 已存在')
            else:
                print(f'ERR: {table}.{col} -> {msg}')
    db.close()
    print('extend_automation_tables done')


if __name__ == '__main__':
    main()
