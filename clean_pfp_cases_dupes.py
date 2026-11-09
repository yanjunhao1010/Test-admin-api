# -*- coding: utf-8 -*-
"""
一次性清理 product_point_cases 的重复墓碑行 / 多余物理行。

背景：历史「关联→解绑→再关联」曾直接 INSERT 新行（而非复活墓碑），
导致 uk_pfp_case(is_del, pfp_id, case_id) 出现两条 is_del=1 同键行，
再次解绑 UPDATE 撞唯一键报 1062。

清理规则（语义安全）：
- 对每个 (pfp_id, case_id) 分组：
  - 若存在生效行(is_del=0)：保留它，删除该组全部墓碑行(is_del=1)。
  - 若无生效行：保留最新一条墓碑行，删除其余墓碑行。
这样清理后任意分组最多只剩 1 行，解绑 UPDATE 不再冲突。
"""
import os
import sys
from collections import defaultdict

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'src'))
os.chdir(os.path.join(ROOT, 'src'))

from src.comment import MySqlconection

CONFIG_PATH = os.path.join(ROOT, 'src', 'config', 'config.yml')


def main():
    cfg = MySqlconection.load_config_cached(CONFIG_PATH)
    d = MySqlconection.DatabaseHandler(
        cfg['mysql']['host'], cfg['mysql']['user'],
        cfg['mysql']['password'], cfg['mysql']['database'])
    with d:
        rows = d.execute_query(
            "SELECT id, pfp_id, case_id, is_del FROM product_point_cases")
        state = defaultdict(lambda: {'active': [], 'tomb': []})
        for r in rows:
            key = (r['pfp_id'], r['case_id'])
            if r['is_del'] == 0:
                state[key]['active'].append(r['id'])
            else:
                state[key]['tomb'].append(r['id'])

        to_delete = []
        for key, v in state.items():
            if v['active']:
                # 生效行唯一键保证至多一条；若有异常多条也只保留最新
                acts = sorted(v['active'])
                to_delete += acts[:-1]
                to_delete += v['tomb']  # 有生效行时全部墓碑删除
            else:
                tombs = sorted(v['tomb'])
                to_delete += tombs[:-1]  # 无生效行：保留一条最新墓碑

        print('总行数:', len(rows))
        print('涉及分组数:', len(state))
        print('待删除多余行数:', len(to_delete))

        if to_delete:
            for i in range(0, len(to_delete), 500):
                batch = to_delete[i:i + 500]
                placeholders = ','.join('%s' for _ in batch)
                d.execute_update(
                    "DELETE FROM product_point_cases WHERE id IN (%s)" % placeholders,
                    tuple(batch))
            print('已删除:', len(to_delete))

        # 复检是否还有任意 (is_del, pfp_id, case_id) 重复组
        dup = d.execute_query(
            "SELECT is_del, pfp_id, case_id, COUNT(*) c "
            "FROM product_point_cases GROUP BY is_del, pfp_id, case_id HAVING COUNT(*) > 1")
        print('复检重复组数:', len(dup))
        for x in dup:
            print('  DUP', x)


if __name__ == '__main__':
    main()
