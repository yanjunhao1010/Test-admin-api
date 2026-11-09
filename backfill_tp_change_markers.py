# -*- coding: utf-8 -*-
"""
迁移：给「已经被修改过」的测试点补写 chg 留档标记（test_points JSON 项内）。

背景：
  「诊断」按钮的可用状态（「这个测试点被修改过没有」）原先是**每次从
  product_point_changes 反推**出来的。反推会因下列任一因素而**翻转**：
    · 测试点被删除 → 后续测试点下标回缩 → 留痕里的 tp_index 对不上
    · 测试点再次改名 → 留痕的 new_title 与当前标题不一致
    · 功能点内出现同名测试点 → 标题兜底认领主动放弃
    · 留痕行被软删 → 直接查不到
  表现为「刚改完按钮能点，过一会儿又变灰」。

改造后（product_points.py，11/04 晚最终口径）：
  「诊断」按钮只看 `chg.pending` —— 一次真改 = 一次诊断资格，
  「应用到用例」/「替换为新用例」成功后 pending 置 false → 按钮熄灭，
  必须再次真正修改内容才重新点亮。
  ⚠️ 本脚本回填的标记**不带 pending** → 按新口径一律视为「已处理」→ 按钮置灰。
     也就是说它现在只用于补全历史留档（改前锚点可追溯），**不再影响按钮显隐**；
     新语义下无需再执行本脚本（保留仅为兼容旧部署）。

回填口径：
  与运行时兜底完全一致（复用 product_points._match_change_to_tp），
  所以「回填后判定为改过」与「改造前的判定结果」不会打架。

⚠️ **真改语义**（11/04 晚补充）：
  「有留痕」≠「真正修改过」。**空改留痕**（打开弹窗一字未改就点保存 →
  old_title == new_title）不构成一次真实修改 → **不回填标记**，
  否则「诊断」按钮会被无意义地点亮。
  判定复用 product_points._tp_change_is_real。

⚠️ **升级老标记**：
  老版本写出的标记缺 `new_title` 字段 → 「是否真改」只能靠查留痕核实。
  本脚本会**补上** `new_title`（让标记可自证，留痕被软删也不影响判定），
  而不是简单跳过。

幂等：可重复执行 —— 已是最新（有 new_title）的标记不动；老标记升级；空改留痕跳过。

执行：在 Fast_api_project 根目录跑
  ./.venv/Scripts/python.exe backfill_tp_change_markers.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, 'src'))
os.chdir(os.path.join(ROOT, 'src'))

from src.comment import MySqlconection
from src.routers.product_points import (
    _match_change_to_tp,
    _norm_tp_list,
    _dump_tp_list,
    _load_pfp_changes,
    _tp_change_is_real,
)

CONFIG_PATH = os.path.join(ROOT, 'src', 'config', 'config.yml')


def connect(cfg):
    """建连带重试：公网 MySQL 偶发超时，重试即通。"""
    last = None
    for i in range(3):
        try:
            d = MySqlconection.DatabaseHandler(
                cfg['mysql']['host'], cfg['mysql']['user'],
                cfg['mysql']['password'], cfg['mysql']['database'],
            )
            d._ensure_connection()  # noqa: SLF001
            return d
        except Exception as e:  # noqa: BLE001
            last = e
            print('连接失败(第%d次): %s' % (i + 1, e))
            import time
            time.sleep(2)
    raise last


def main():
    cfg = MySqlconection.load_config_cached(CONFIG_PATH)
    d = connect(cfg)
    with d:
        rows = d.execute_query(
            "SELECT id, title, test_points FROM product_function_points WHERE is_del=0"
        ) or []
        print('待检查功能点: %d 个' % len(rows))

        filled = 0
        touched_pfp = 0
        skipped = 0
        upgraded = 0
        for r in rows:
            pfp_id = int(r['id'])
            items = _norm_tp_list(r.get('test_points'))
            if not items:
                continue
            changes = _load_pfp_changes(d, [pfp_id]).get(pfp_id, []) or []
            changed_any = False
            for i, it in enumerate(items):
                existing = it.get('chg') or {}
                # ① 已有 chg 标记 → **升级**（补 new_title），而不是直接跳过。
                #    老标记缺 new_title → 「是否真改」只能靠查留痕核实；
                #    补上后即可**自证**，留痕被软删也不影响判定。
                if existing:
                    if str(existing.get('new_title') or '').strip():
                        skipped += 1
                        continue
                    hit = None
                    cid = existing.get('id')
                    if cid:
                        for c in changes:
                            if int(c.get('id') or 0) == int(cid):
                                hit = c
                                break
                    if not hit:
                        skipped += 1
                        continue
                    if not _tp_change_is_real(hit):
                        # 标记对应的留痕是空改 → 撤销标记（不该点亮按钮）
                        it.pop('chg', None)
                        changed_any = True
                        filled += 1
                        print('  撤销 pfp=%s idx=%s → change_id=%s 是空改（old==new）'
                              % (pfp_id, i, hit['id']))
                        continue
                    it['chg'] = dict(existing)
                    it['chg']['new_title'] = (hit.get('new_title') or '')[:512]
                    changed_any = True
                    upgraded += 1
                    print('  升级 pfp=%s idx=%s → change_id=%s 补 new_title=%r'
                          % (pfp_id, i, hit['id'], hit.get('new_title')))
                    continue
                # ② 没有标记 → 按留痕反推回填
                hit = _match_change_to_tp(changes, items, i)
                if not hit:
                    continue
                # ⚠️ 空改留痕（改前 == 改后）**不算一次真正的修改** → 不回填标记。
                #    否则「打开弹窗一字未改就点保存」也会点亮「诊断」按钮。
                if not _tp_change_is_real(hit):
                    print('  跳过 pfp=%s idx=%s → change_id=%s 是空改（old==new）'
                          % (pfp_id, i, hit['id']))
                    continue
                items[i]['chg'] = {
                    'id': int(hit['id']),
                    'time': str(hit.get('create_time') or ''),
                    'old_title': (hit.get('old_title') or '')[:512],
                    # ⚠️ new_title：让「这次是否真改」可自证（old_title != new_title）
                    'new_title': (hit.get('new_title') or '')[:512],
                }
                changed_any = True
                filled += 1
                print('  回填 pfp=%s idx=%s → change_id=%s old_title=%r'
                      % (pfp_id, i, hit['id'], hit.get('old_title')))
            if changed_any:
                d.execute_update(
                    "UPDATE product_function_points SET test_points=%s, update_time=NOW() WHERE id=%s",
                    (_dump_tp_list(items), pfp_id),
                )
                touched_pfp += 1

        print('回填完成：标记 %d 条（其中升级 %d 条），涉及功能点 %d 个，已带标记跳过 %d 个'
              % (filled, upgraded, touched_pfp, skipped))


if __name__ == '__main__':
    main()
