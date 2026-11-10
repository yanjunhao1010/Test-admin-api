# -*- coding: utf-8 -*-
"""代码变更影响分析（初级版）建表 + 菜单权限迁移。

幂等：可重复执行。
  python add_codewatch_tables.py

新增：
  cw_repos     仓库配置（监听 dev 分支）
  cw_changes   变更集 + 分析快照（report_json / report_md）
  cw_files     文件级 diff（供页面展示）
  cw_impacts   影响项（接口/模型/表/共享函数）
  cw_mappings  路径 -> 模块映射（预留，当前用内置映射）
  菜单权限点：menu:codewatch / :changes / :repos，并授予 admin 角色
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.comment import MySqlconection, yaml_data  # noqa: E402

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), 'src', 'config', 'config.yml'
)

SQL = """
CREATE TABLE IF NOT EXISTS cw_repos (
  id INT AUTO_INCREMENT PRIMARY KEY COMMENT '主键自增ID',
  name VARCHAR(100) NOT NULL DEFAULT '' COMMENT '仓库显示名',
  repo_url VARCHAR(500) NOT NULL DEFAULT '' COMMENT '仓库地址(https)',
  dev_branch VARCHAR(100) NOT NULL DEFAULT 'feature/dev' COMMENT '开发分支(监听变更)',
  base_branch VARCHAR(100) NOT NULL DEFAULT 'master' COMMENT '基线分支(已部署)',
  project_type VARCHAR(16) NOT NULL DEFAULT 'AUTO' COMMENT '项目类型(AUTO自动识别/PYTHON/PHP/JAVA)',
  token_enc VARCHAR(800) NOT NULL DEFAULT '' COMMENT '访问令牌密文(Fernet加密)',
  enabled TINYINT NOT NULL DEFAULT 1 COMMENT '是否启用(1启用0停用)',
  last_sha VARCHAR(64) NOT NULL DEFAULT '' COMMENT '上次分析到的提交sha(基线)',
  last_sync_time DATETIME DEFAULT NULL COMMENT '上次同步时间',
  create_by INT NOT NULL DEFAULT 0 COMMENT '创建人用户ID',
  create_time DATETIME DEFAULT NULL COMMENT '创建时间',
  update_time DATETIME DEFAULT NULL COMMENT '更新时间',
  is_del TINYINT NOT NULL DEFAULT 0 COMMENT '是否删除(1已删除)'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='代码变更监听仓库配置';

CREATE TABLE IF NOT EXISTS cw_changes (
  id INT AUTO_INCREMENT PRIMARY KEY COMMENT '主键自增ID',
  repo_id INT NOT NULL DEFAULT 0 COMMENT '仓库ID(cw_repos.id)',
  branch VARCHAR(100) NOT NULL DEFAULT '' COMMENT '变更所在分支',
  base_sha VARCHAR(64) NOT NULL DEFAULT '' COMMENT '起始提交sha',
  head_sha VARCHAR(64) NOT NULL DEFAULT '' COMMENT '结束提交sha',
  commit_count INT NOT NULL DEFAULT 0 COMMENT '提交数',
  file_count INT NOT NULL DEFAULT 0 COMMENT '变更文件数',
  insertions INT NOT NULL DEFAULT 0 COMMENT '新增行数',
  deletions INT NOT NULL DEFAULT 0 COMMENT '删除行数',
  must_count INT NOT NULL DEFAULT 0 COMMENT '必须回归接口数',
  suggest_count INT NOT NULL DEFAULT 0 COMMENT '建议回归接口数',
  new_count INT NOT NULL DEFAULT 0 COMMENT '新增接口数',
  risk_level VARCHAR(10) NOT NULL DEFAULT '低' COMMENT '风险等级(高/中/低)',
  status TINYINT NOT NULL DEFAULT 1 COMMENT '状态(1成功2失败3基线)',
  error_msg TEXT COMMENT '失败原因',
  report_json MEDIUMTEXT COMMENT '结构化影响分析快照(JSON)',
  report_md MEDIUMTEXT COMMENT 'Markdown回归建议报告',
  create_by INT NOT NULL DEFAULT 0 COMMENT '创建人用户ID',
  create_time DATETIME DEFAULT NULL COMMENT '创建时间',
  analyze_time DATETIME DEFAULT NULL COMMENT '分析完成时间',
  UNIQUE KEY uk_cw_change_head (repo_id, head_sha),
  KEY idx_cw_change_repo (repo_id, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='代码变更集及影响分析结果';

CREATE TABLE IF NOT EXISTS cw_files (
  id INT AUTO_INCREMENT PRIMARY KEY COMMENT '主键自增ID',
  change_id INT NOT NULL DEFAULT 0 COMMENT '变更集ID(cw_changes.id)',
  path VARCHAR(500) NOT NULL DEFAULT '' COMMENT '文件路径',
  status VARCHAR(4) NOT NULL DEFAULT 'M' COMMENT '变更类型(A新增/M修改/D删除/R重命名)',
  additions INT NOT NULL DEFAULT 0 COMMENT '新增行数',
  deletions INT NOT NULL DEFAULT 0 COMMENT '删除行数',
  patch MEDIUMTEXT COMMENT 'unified diff 文本',
  is_noise TINYINT NOT NULL DEFAULT 0 COMMENT '是否仅注释/格式变化(1是)',
  KEY idx_cw_file_change (change_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='代码变更文件明细';

CREATE TABLE IF NOT EXISTS cw_impacts (
  id INT AUTO_INCREMENT PRIMARY KEY COMMENT '主键自增ID',
  change_id INT NOT NULL DEFAULT 0 COMMENT '变更集ID(cw_changes.id)',
  kind VARCHAR(20) NOT NULL DEFAULT 'api' COMMENT '类型(api接口/model模型/table表/symbol函数)',
  target_key VARCHAR(300) NOT NULL DEFAULT '' COMMENT '目标标识(接口路径/模型名/表名)',
  method VARCHAR(10) NOT NULL DEFAULT '' COMMENT 'HTTP方法',
  module VARCHAR(100) NOT NULL DEFAULT '' COMMENT '所属功能模块',
  relation_label VARCHAR(100) NOT NULL DEFAULT '' COMMENT '影响方式(direct/caller/table/model/new/removed)',
  level VARCHAR(10) NOT NULL DEFAULT 'suggest' COMMENT '级别(must必须/suggest建议)',
  file VARCHAR(300) NOT NULL DEFAULT '' COMMENT '来源文件',
  line INT NOT NULL DEFAULT 0 COMMENT '来源行号',
  reasons TEXT COMMENT '判定理由',
  sort INT NOT NULL DEFAULT 0 COMMENT '排序',
  KEY idx_cw_impact_change (change_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='代码变更影响项';

CREATE TABLE IF NOT EXISTS cw_mappings (
  id INT AUTO_INCREMENT PRIMARY KEY COMMENT '主键自增ID',
  name VARCHAR(100) NOT NULL DEFAULT '' COMMENT '映射名称',
  match_type VARCHAR(20) NOT NULL DEFAULT 'path_prefix' COMMENT '匹配方式(path_prefix/route_prefix)',
  pattern VARCHAR(300) NOT NULL DEFAULT '' COMMENT '匹配模式',
  module VARCHAR(100) NOT NULL DEFAULT '' COMMENT '映射到的功能模块',
  priority INT NOT NULL DEFAULT 0 COMMENT '优先级(大者优先)',
  enabled TINYINT NOT NULL DEFAULT 1 COMMENT '是否启用(1启用0停用)',
  create_time DATETIME DEFAULT NULL COMMENT '创建时间',
  UNIQUE KEY uk_cw_mapping (match_type, pattern)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='代码路径到功能模块映射(预留)';
"""

# (code, name, path, component, sort, parent_code)
PERMISSIONS = [
    ('menu:codewatch', '代码变更', '/codewatch', 'Layout', 9, None),
    ('menu:codewatch:changes', '变更影响分析', 'changes',
     '@/views/codewatch/changes/index', 1, 'menu:codewatch'),
    ('menu:codewatch:repos', '仓库配置', 'repos',
     '@/views/codewatch/repos/index', 2, 'menu:codewatch'),
]


def ensure_permission(db, code, name, path, component, sort, parent_code):
    row = db.execute_single_query('SELECT id FROM permissions WHERE code=%s', (code,))
    if row:
        return row['id'], False
    parent_id = 0
    if parent_code:
        p = db.execute_single_query('SELECT id FROM permissions WHERE code=%s', (parent_code,))
        if not p:
            raise RuntimeError('父级权限点不存在: %s' % parent_code)
        parent_id = p['id']
    db.execute_update(
        'INSERT INTO permissions (parent_id, name, code, `type`, path, icon, sort, '
        'component, redirect, hidden, always_show, affix, no_keep_alive, badge) '
        'VALUES (%s,%s,%s,1,%s,%s,%s,%s,%s,0,1,0,0,%s)',
        (parent_id, name, code, path, 'code-branch' if not parent_code else '',
         sort, component, '', 'NEW' if not parent_code else ''),
    )
    row = db.execute_single_query('SELECT id FROM permissions WHERE code=%s', (code,))
    return row['id'], True


def grant_admin(db, perm_ids):
    """与 init_automation_permissions.sync_admin_all_permissions 保持一致：admin 补齐新权限点。"""
    role = db.execute_single_query("SELECT id FROM roles WHERE code='admin'")
    if not role:
        print('[WARN] 未找到 admin 角色，跳过授权')
        return 0
    granted = 0
    for pid in perm_ids:
        exists = db.execute_single_query(
            'SELECT 1 x FROM role_permissions WHERE role_id=%s AND permission_id=%s',
            (role['id'], pid),
        )
        if not exists:
            db.execute_update(
                'INSERT INTO role_permissions (role_id, permission_id) VALUES (%s,%s)',
                (role['id'], pid),
            )
            granted += 1
    return granted


def main():
    data = yaml_data.BaseYamlData(CONFIG_PATH).read_yaml()
    db = MySqlconection.DatabaseHandler(
        data['mysql']['host'], data['mysql']['user'],
        data['mysql']['password'], data['mysql']['database'],
    )
    db.connect()
    try:
        for stmt in SQL.split(';'):
            stmt = stmt.strip()
            if stmt:
                db.execute_update(stmt)
                print('OK:', stmt.splitlines()[0][:70])

        ids = []
        for code, name, path, component, sort, parent in PERMISSIONS:
            pid, created = ensure_permission(db, code, name, path, component, sort, parent)
            ids.append(pid)
            print('%s 权限点 %s (id=%s)' % ('新增' if created else '已存在', code, pid))

        granted = grant_admin(db, ids)
        print('admin 角色新增授权 %d 条' % granted)
        print('代码变更影响分析 初始化完成')
    finally:
        db.close()


if __name__ == '__main__':
    main()
