import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.comment import MySqlconection, yaml_data

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'src', 'config', 'config.yml'
)

SQL = """
CREATE TABLE IF NOT EXISTS api_interfaces (
  id INT AUTO_INCREMENT PRIMARY KEY COMMENT '主键自增ID',
  name VARCHAR(100) NOT NULL DEFAULT '' COMMENT '接口名称',
  method VARCHAR(10) NOT NULL DEFAULT 'GET' COMMENT '请求方法(GET/POST/PUT/DELETE)',
  url VARCHAR(500) NOT NULL DEFAULT '' COMMENT '接口路径(只存path,执行时按所选环境base_url拼接)',
  headers TEXT COMMENT '请求头(JSON字符串)',
  params TEXT COMMENT '查询参数(JSON字符串)',
  body TEXT COMMENT '请求体(JSON字符串)',
  assert_type VARCHAR(20) NOT NULL DEFAULT 'status_code' COMMENT '断言类型(status_code/contains/body_status/json_path/regex/header/response_time)',
  expected_value VARCHAR(500) NOT NULL DEFAULT '' COMMENT '断言期望值',
  enabled TINYINT NOT NULL DEFAULT 1 COMMENT '是否启用(1启用0停用)',
  extracts TEXT COMMENT '响应变量提取规则(JSON数组),用于接口间上下文传递',
  verify_ssl TINYINT NOT NULL DEFAULT 1 COMMENT '是否校验SSL证书(1校验0跳过,内网/自签证书端点可关)',
  create_by INT NOT NULL DEFAULT 0 COMMENT '创建人用户ID',
  create_time DATETIME DEFAULT NULL COMMENT '创建时间',
  update_time DATETIME DEFAULT NULL COMMENT '更新时间'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS api_schedules (
  id INT AUTO_INCREMENT PRIMARY KEY COMMENT '主键自增ID',
  name VARCHAR(100) NOT NULL DEFAULT '' COMMENT '计划名称',
  interface_ids TEXT COMMENT '关联接口ID列表(JSON数组)',
  cron_expr VARCHAR(100) NOT NULL DEFAULT '' COMMENT 'cron执行周期表达式(标准5段)',
  enabled TINYINT NOT NULL DEFAULT 1 COMMENT '是否启用(1启用0停用)',
  env_id INT DEFAULT NULL COMMENT '绑定的环境ID,执行时该计划全部接口使用此环境的base_url/headers',
  last_run_time DATETIME DEFAULT NULL COMMENT '上次执行时间',
  next_run_time DATETIME DEFAULT NULL COMMENT '下次执行时间',
  create_by INT NOT NULL DEFAULT 0 COMMENT '创建人用户ID',
  create_time DATETIME DEFAULT NULL COMMENT '创建时间',
  update_time DATETIME DEFAULT NULL COMMENT '更新时间'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS api_executions (
  id INT AUTO_INCREMENT PRIMARY KEY COMMENT '主键自增ID',
  schedule_id INT DEFAULT NULL COMMENT '关联执行计划ID',
  interface_id INT NOT NULL COMMENT '关联接口ID',
  run_time DATETIME DEFAULT NULL COMMENT '执行时间',
  http_status INT DEFAULT NULL COMMENT 'HTTP响应状态码',
  elapsed_ms INT DEFAULT NULL COMMENT '执行耗时(毫秒)',
  success TINYINT NOT NULL DEFAULT 0 COMMENT '是否成功(1成功0失败)',
  response_snippet TEXT COMMENT '响应内容摘要',
  error_msg TEXT COMMENT '错误信息',
  assertion_result VARCHAR(50) NOT NULL DEFAULT '' COMMENT '断言结果',
  request_dump TEXT COMMENT '实际发出的请求快照(JSON:method/url/headers/params/body)',
  response_dump TEXT COMMENT '完整响应快照(JSON:status/headers/body)',
  extracted_vars TEXT COMMENT '本次执行提取的上下文变量(JSON)',
  assert_detail TEXT COMMENT '断言明细(JSON:passed/type/expected/actual/detail)',
  run_batch BIGINT DEFAULT NULL COMMENT '同一次计划执行的批次号(epoch毫秒)，同一批次的接口执行记录归属同一运行，用于按运行聚合测试报告',
  `trigger` VARCHAR(16) NOT NULL DEFAULT 'manual' COMMENT '触发方式(manual=手动执行/schedule=定时调度)'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS api_environments (
  id INT AUTO_INCREMENT PRIMARY KEY COMMENT '主键自增ID',
  name VARCHAR(100) NOT NULL DEFAULT '' COMMENT '环境名称',
  base_url VARCHAR(500) NOT NULL DEFAULT '' COMMENT '环境基础地址,接口只存路径,执行时按所选环境拼接',
  headers TEXT COMMENT '全局请求头(JSON字符串,该环境下所有接口均自动携带)',
  variables TEXT COMMENT '全局变量(JSON字符串,键值对,供接口{{key}}引用)',
  create_by INT NOT NULL DEFAULT 0 COMMENT '创建人用户ID',
  create_time DATETIME DEFAULT NULL COMMENT '创建时间',
  update_time DATETIME DEFAULT NULL COMMENT '更新时间'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""


def main():
    data = yaml_data.BaseYamlData(CONFIG_PATH).read_yaml()
    db = MySqlconection.DatabaseHandler(
        data['mysql']['host'], data['mysql']['user'],
        data['mysql']['password'], data['mysql']['database'],
    )
    db.connect()
    for stmt in SQL.split(';'):
        stmt = stmt.strip()
        if not stmt:
            continue
        db.execute_update(stmt)
        print('OK:', stmt.splitlines()[0][:60])
    print('automation tables created OK')
    db.close()


if __name__ == '__main__':
    main()
