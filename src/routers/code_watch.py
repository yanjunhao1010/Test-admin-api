# -*- coding: utf-8 -*-
"""代码变更影响分析接口（初级版：只分析，不执行用例）。

前端对应 src/api/codewatch.js：
    GET  /api/code-watch/repos                 仓库列表
    POST /api/code-watch/repos                 新增仓库
    PUT  /api/code-watch/repos                 编辑仓库
    POST /api/code-watch/repos/delete          删除仓库
    POST /api/code-watch/repos/test            连通性测试
    POST /api/code-watch/repos/{id}/sync       拉取 dev 最新并分析
    POST /api/code-watch/changes               指定 base..head 分析（补历史）
    GET  /api/code-watch/changes               变更列表
    GET  /api/code-watch/changes/{id}          变更详情
    POST /api/code-watch/changes/{id}/analyze  重新分析
    GET  /api/code-watch/changes/{id}/report   Markdown 报告
"""
import base64
import json
import logging
import os
import re
import subprocess
import threading

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from typing import Optional

from src.comment import MySqlconection, yaml_data
from src.codewatch import analyzer, languages, review
from src.routers.auth import parse_token

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

router = APIRouter(prefix='/code-watch', tags=['code-watch'])

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CONFIG_PATH = os.path.join(PROJECT_ROOT, 'src', 'config', 'config.yml')
REPO_ROOT = os.environ.get('CODEWATCH_REPO_DIR') or os.path.join(
    PROJECT_ROOT, 'uploads', 'codewatch', 'repos')

_LOCKS = {}
_LOCKS_GUARD = threading.Lock()


def get_db_handler():
    data = yaml_data.BaseYamlData(CONFIG_PATH).read_yaml()
    return MySqlconection.DatabaseHandler(
        data['mysql']['host'], data['mysql']['user'],
        data['mysql']['password'], data['mysql']['database'])


def _uid(request: Request) -> int:
    token = request.headers.get('accessToken')
    if not token:
        return 0
    try:
        return parse_token(token) or 0
    except Exception:
        return 0


def _ok(data=None, msg='success', total=None):
    out = {'code': 200, 'msg': msg, 'data': data}
    if total is not None:
        out['totalCount'] = total
    return out


def _repo_lock(repo_id):
    with _LOCKS_GUARD:
        if repo_id not in _LOCKS:
            _LOCKS[repo_id] = threading.Lock()
        return _LOCKS[repo_id]


# ---------------------------------------------------------------------------
# git 操作
# ---------------------------------------------------------------------------
def _decode_token(repo):
    enc = (repo.get('token_enc') or '').strip()
    if not enc:
        return ''
    try:
        from src.ai import crypto
        return crypto.decrypt(enc)
    except Exception as e:
        raise HTTPException(status_code=400, detail='仓库令牌解密失败：%s' % e)


def _git_prefix(token):
    if not token:
        return []
    auth = base64.b64encode(('x-access-token:' + token).encode('utf-8')).decode('ascii')
    return ['-c', 'http.extraheader=AUTHORIZATION: basic ' + auth]


def _git(repo_path, args, token='', timeout=120):
    prefix = _git_prefix(token)
    proxy = (os.environ.get('CODEWATCH_GIT_PROXY')
             or os.environ.get('HTTPS_PROXY') or os.environ.get('https_proxy') or '')
    if proxy:
        prefix += ['-c', 'http.proxy=' + proxy, '-c', 'https.proxy=' + proxy]
    cmd = ['git'] + (['-C', repo_path] if repo_path else []) + prefix + list(args)
    env = os.environ.copy()
    env['GIT_TERMINAL_PROMPT'] = '0'
    # GitHub 的 22 端口常被网络屏蔽：允许走 ssh.github.com:443；首次连接自动接受主机指纹
    env.setdefault('GIT_SSH_COMMAND',
                   'ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=15')
    try:
        p = subprocess.run(cmd, capture_output=True, timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=500, detail='git 操作超时（%ss）：%s' % (timeout, ' '.join(args)))
    if p.returncode != 0:
        err = p.stderr.decode('utf-8', 'replace')[:800]
        if any(k in err for k in ('Could not connect', 'Failed to connect',
                                  'Connection timed out', 'Connection refused')):
            err += ('\n提示：当前网络无法直连 GitHub。可把仓库地址改成 SSH over 443：'
                    'ssh://git@ssh.github.com:443/owner/repo.git（需本机已配置 GitHub SSH key）')
        raise HTTPException(status_code=500, detail='git 失败：%s' % err)
    return p.stdout.decode('utf-8', 'replace')


def _repo_dir(repo_id):
    return os.path.join(REPO_ROOT, str(repo_id))


def _partial_clone(path):
    """判断缓存仓是否为早期用 --filter=blob:none 创建的部分克隆。"""
    try:
        return bool(_git(path, ['config', '--get',
                                'remote.origin.partialclonefilter']).strip())
    except HTTPException:
        return False


def _fill_blobs(path, token):
    """把部分克隆补全成完整仓：去掉 filter 标记后重新拉取全部对象。

    分析阶段需要 base/head 两版文件内容，部分克隆会在 git diff 时由 git 内部
    子进程按需向 promisor remote 拉 blob；该请求拿不到本模块注入的认证头，
    在需要认证的 HTTP 仓库上会失败（analyzer.git 抛 RuntimeError → 500）。
    补全后分析阶段完全离线，不再依赖网络与令牌传递。
    """
    for key in ('remote.origin.partialclonefilter', 'remote.origin.promisor',
                'extensions.partialclone'):
        try:
            _git(path, ['config', '--unset-all', key], timeout=30)
        except HTTPException:
            pass  # 配置本来就不存在
    _git(path, ['fetch', '--refetch', '--prune', 'origin'], token=token, timeout=600)


def _ensure_repo(repo):
    path = _repo_dir(repo['id'])
    token = _decode_token(repo)
    url = (repo.get('repo_url') or '').strip()
    if not url:
        raise HTTPException(status_code=400, detail='仓库地址为空')
    if not os.path.isdir(os.path.join(path, '.git')):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.isdir(path):
            import shutil
            shutil.rmtree(path)
        # 完整克隆（--no-checkout 仍不产生工作区文件），避免分析时按需拉取 blob
        _git(None, ['clone', '--no-checkout', url, path],
             token=token, timeout=600)
    elif _partial_clone(path):
        # 兼容早期创建的缓存仓：一次性补全，之后分析完全离线
        _fill_blobs(path, token)
    branches = [b for b in (repo.get('dev_branch'), repo.get('base_branch')) if b]
    try:
        _git(path, ['fetch', '--prune', 'origin'] + branches, token=token, timeout=180)
    except HTTPException:
        # 分支名在远端不存在时退回全量 fetch，至少保证 dev 分支可用
        _git(path, ['fetch', '--prune', 'origin'], token=token, timeout=180)
    return path


def _rev(path, ref):
    try:
        return _git(path, ['rev-parse', '--verify', ref], timeout=20).strip()
    except HTTPException:
        return ''


def _is_ancestor(path, base, head):
    try:
        _git(path, ['merge-base', '--is-ancestor', base, head], timeout=20)
        return True
    except HTTPException:
        return False


def _remote_branches(path):
    try:
        out = _git(path, ['for-each-ref', '--format=%(refname:short)',
                          'refs/remotes/origin'], timeout=20)
    except HTTPException:
        return []
    branches = []
    for line in out.splitlines():
        b = line.strip()
        if not b or b.endswith('/HEAD'):
            continue
        branches.append(b[len('origin/'):] if b.startswith('origin/') else b)
    return branches


def _fallback_base(path, head):
    for ref in ('%s~20' % head, '%s~1' % head):
        sha = _rev(path, ref)
        if sha:
            return sha
    return head


def _check_range(path, base, head):
    if not _rev(path, base):
        raise HTTPException(status_code=400, detail='base 提交不存在：%s' % base)
    if not _rev(path, head):
        raise HTTPException(status_code=400, detail='head 提交不存在：%s' % head)
    if base == head:
        raise HTTPException(status_code=400, detail='base 与 head 相同，没有可分析的变更')


# ---------------------------------------------------------------------------
# 分析 / 落库
# ---------------------------------------------------------------------------
def _load_assets(db):
    known, interfaces, exec_map, manual = set(), [], {}, []
    try:
        known = {r['TABLE_NAME'].lower() for r in db.execute_query(
            'SELECT TABLE_NAME FROM information_schema.TABLES WHERE TABLE_SCHEMA=DATABASE()')}
        interfaces = db.execute_query(
            'SELECT id, name, method, url, `group`, enabled FROM api_interfaces')
        rows = db.execute_query(
            'SELECT interface_id, COUNT(*) n, SUM(success) s FROM api_executions '
            'WHERE run_time >= DATE_SUB(NOW(), INTERVAL 30 DAY) GROUP BY interface_id')
        exec_map = {r['interface_id']: r for r in rows}
        manual = db.execute_query(
            'SELECT COUNT(*) n, module FROM test_cases WHERE is_del=0 GROUP BY module')
    except Exception as e:
        logger.warning('加载接口库覆盖信息失败（降级为无覆盖信息）：%s', e)
    return known, {'interfaces': interfaces, 'exec': exec_map, 'manual': manual}


def _analyze(repo, path, base, head, db, uid=0):
    known, assets = _load_assets(db)
    result = analyzer.analyze_range(
        path, base, head, known, assets,
        project_type=repo.get('project_type') or 'AUTO')
    result['manual'] = assets.get('manual') or []
    # AI 复核层（A 关联判定 + B 场景补充）：仓库开关默认关闭，不开启时零模型调用。
    # analyzer.py 保持纯函数，db / uid 只在这一层使用。
    review.review(result, db, uid,
                  enabled=int(repo.get('ai_review_enabled') or 0) == 1)
    return result


def _build_md(result, repo):
    lines = ['# 代码变更影响分析报告', '']
    lines.append('- 仓库：`%s`（%s）' % (repo.get('name') or '', repo.get('repo_url') or ''))
    lines.append('- 项目类型：`%s`' % (result.get('project_type_label') or 'PYTHON'))
    lines.append('- 区间：`%s..%s`（%d 个提交，%d 个文件，+%d / -%d 行）' % (
        result['base_sha'][:8], result['head_sha'][:8], result['commit_count'],
        len(result['files']), result['counts']['insertions'], result['counts']['deletions']))
    lines.append('')
    if result.get('commits'):
        lines.append('## 提交')
        lines.append('')
        for c in result['commits'][:20]:
            lines.append('- `%s` %s — %s' % (c['sha'], c['author'], c['subject']))
        lines.append('')

    apis = [i for i in result['impacts'] if i['kind'] == 'api']
    logic = [i for i in result['impacts'] if i.get('kind') == 'logic']
    must = [i for i in apis if i['level'] == 'must']
    suggest = [i for i in apis if i['level'] == 'suggest']
    lines.append('## 一、结论')
    lines.append('')
    lines.append('影响接口 **%d** 个：必须回归 **%d** / 建议回归 **%d**；逻辑层变更 **%d** 个；'
                 '涉及表 %d 张、模型 %d 个。%s' % (
                     len(apis), len(must), len(suggest), len(logic),
                     len(result['tables']), len(result['models']),
                     '' if result['db_available'] else '（数据库覆盖信息不可用，标为「未知」）'))
    lines.append('')

    ar = result.get('ai_review') or {}
    if ar.get('enabled'):
        lines.append('> AI 复核（A 关联判定 + B 场景补充）：状态 `%s`；近似关联 %d 条，已复核 %d 条'
                     '（成立 %d / 误连降级 %d / 结论不可信 %d）；补充业务场景 %d 条'
                     '（逻辑层候选 %d 个）。' % (
                         ar.get('status') or '', ar.get('candidates') or 0,
                         ar.get('reviewed') or 0, ar.get('relevant') or 0,
                         ar.get('irrelevant') or 0, ar.get('unknown') or 0,
                         ar.get('scenes') or 0, ar.get('scene_logics') or 0))
        if ar.get('unknown'):
            lines.append('> 注意：%d 条结论不可信，已保留原级别并标记「请重试」，未按不受影响处理。'
                         % ar['unknown'])
        for e in (ar.get('errors') or [])[:3]:
            lines.append('> - 复核异常：%s' % e)
        lines.append('')

    modules = {}
    for i in apis:
        d = modules.setdefault(i.get('module') or '未分类', {'must': 0, 'suggest': 0})
        d['must' if i['level'] == 'must' else 'suggest'] += 1
    if modules:
        lines += ['| 模块 | 必须回归 | 建议回归 |', '|---|---|---|']
        for m, d in sorted(modules.items(), key=lambda kv: -kv[1]['must']):
            lines.append('| %s | %d | %d |' % (m, d['must'], d['suggest']))
        lines.append('')

    def table(items, title):
        if not items:
            return
        lines.append('### %s' % title)
        lines.append('')
        lines.append('| 方法 | 路径 | 影响方式 | 模块 | 已有用例 | 说明 |')
        lines.append('|---|---|---|---|---|---|')
        for i in items:
            cases = i.get('cases') or []
            cov = ('%d 条' % len(cases)) if cases else ('未知' if not result['db_available'] else '无')
            lines.append('| %s | `%s` | %s | %s | %s | %s |' % (
                i.get('method') or '', i['target_key'], i.get('relation_label') or '',
                i.get('module') or '', cov, '；'.join(i.get('reasons') or [])[:120]))
        lines.append('')

    table([i for i in must if 'new' not in i.get('relations', [])], '必须回归')
    table([i for i in must if 'new' in i.get('relations', [])], '新增接口（需新增用例）')
    table([i for i in apis if 'removed' in i.get('relations', [])], '删除接口（破坏性）')
    table(suggest, '建议回归')

    lines.append('## 二、涉及的表 / 模型 / 共享函数')
    lines.append('')
    if result['tables']:
        lines.append('- 表：' + '、'.join('`%s`' % t for t in result['tables']))
    for m in result['models']:
        lines.append('- 模型：`%s`（新增 %s / 删除 %s / 修改 %s）' % (
            m['name'], m['added'] or '无', m['removed'] or '无', m['changed'] or '无'))
    for i in result['impacts']:
        if i['kind'] == 'symbol':
            lines.append('- 函数：`%s()` — %s' % (i['target_key'], '；'.join(i.get('reasons') or [])))
    lines.append('')

    if result['noise']:
        lines.append('## 三、被忽略的文件')
        lines.append('')
        for n in result['noise']:
            lines.append('- `%s`：仅注释/格式变化（AST 等价）' % n)
        lines.append('')

    lines.append('## 四、回归建议')
    lines.append('')
    for i in must:
        lines.append('### `%s %s`（%s）' % (i.get('method'), i['target_key'], i.get('module')))
        lines.append('')
        for s in i.get('suggestions') or []:
            lines.append('- %s' % s)
        lines.append('')
    if logic:
        lines.append('## 五、逻辑层变更与验证建议')
        lines.append('')
        lines.append('> 函数体真改但可能回溯不到入口接口的逻辑单元；验证建议为静态推断，'
                     '需人工映射到实际 URL / 用例后执行。')
        lines.append('')
        for i in logic:
            lines.append('### `%s()` — `%s:%s`' % (
                i['target_key'], i.get('file') or '', i.get('line') or 0))
            lines.append('')
            for r in i.get('reasons') or []:
                lines.append('- 判定：%s' % r)
            hints = i.get('hints') or []
            if hints:
                lines.append('- 本次改动代码：%s' % '；'.join(str(h)[:100] for h in hints[:5]))
            for s in i.get('suggestions') or []:
                lines.append('- %s' % s)
            lines.append('')
    lines.append('## 六、口径与局限')
    lines.append('')
    for note in (result.get('lang_notes') or languages.lang_notes('PYTHON')):
        lines.append('- ' + note)
    lines.append('- 调用链：按函数名近似匹配，深度上限 3；同名函数可能误连。')
    lines.append('- 表影响：只认同一已有函数里该表 SQL 的变化；仅 INSERT 变化不外溢，UPDATE/DELETE 才向同表接口扩散。')
    lines.append('- 逻辑层变更：验证建议基于函数体差异与新增代码行推导，不代表真实调用关系；'
                 '未识别到入口接口时需人工映射。')
    if (result.get('ai_review') or {}).get('enabled'):
        lines.append('- AI 复核只产字段级判定，报告与用例建议仍由本地拼装；'
                     '标为「结论不可信」的条目保留静态规则原级别，需人工确认或重试。')
    lines.append('- 本报告为静态分析，不代表运行时一定出问题；分级用于排优先级。')
    return '\n'.join(lines)


def _snapshot(result):
    """report_json 里不带 patch（patch 单独存 cw_files，避免 JSON 过大）。"""
    snap = dict(result)
    snap['files'] = [{k: v for k, v in f.items() if k != 'patch'} for f in result['files']]
    return json.dumps(snap, ensure_ascii=False)


def _insert_id(db, sql, params):
    db.execute_update(sql, params)
    row = db.execute_single_query('SELECT LAST_INSERT_ID() AS id')
    return int(row['id']) if row else 0


def _save_change(db, repo, base, head, result, uid, branch=''):
    md = _build_md(result, repo)
    cid = _insert_id(
        db,
        'INSERT INTO cw_changes (repo_id, branch, base_sha, head_sha, commit_count, file_count, '
        'insertions, deletions, must_count, suggest_count, new_count, risk_level, status, '
        'report_json, report_md, create_by, create_time, analyze_time) '
        'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,1,%s,%s,%s,NOW(),NOW())',
        (repo['id'], branch or repo.get('dev_branch') or '', base, head,
         result['commit_count'], len(result['files']), result['counts']['insertions'],
         result['counts']['deletions'], result['counts']['must'], result['counts']['suggest'],
         result['counts']['new'], result['risk_level'], _snapshot(result), md, uid))
    try:
        for f in result['files']:
            db.execute_update(
                'INSERT INTO cw_files (change_id, path, status, additions, deletions, patch, is_noise) '
                'VALUES (%s,%s,%s,%s,%s,%s,%s)',
                (cid, f['path'], f['status'], f['additions'], f['deletions'],
                 f.get('patch') or '', f['is_noise']))
        for idx, i in enumerate(result['impacts']):
            db.execute_update(
                'INSERT INTO cw_impacts (change_id, kind, target_key, method, module, '
                'relation_label, level, file, line, reasons, sort) '
                'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                (cid, i['kind'], i['target_key'][:300], i.get('method') or '',
                 (i.get('module') or '')[:100], i.get('relation_label') or '',
                 i['level'], (i.get('file') or '')[:300], i.get('line') or 0,
                 '；'.join(i.get('reasons') or []), idx))
    except Exception:
        # 子表写入失败时清掉主表，避免留下半截记录
        db.execute_update('DELETE FROM cw_files WHERE change_id=%s', (cid,))
        db.execute_update('DELETE FROM cw_impacts WHERE change_id=%s', (cid,))
        db.execute_update('DELETE FROM cw_changes WHERE id=%s', (cid,))
        raise
    return cid


# ---------------------------------------------------------------------------
# 仓库 CRUD
# ---------------------------------------------------------------------------
class RepoIn(BaseModel):
    id: Optional[int] = None
    name: str = ''
    repo_url: str = ''
    dev_branch: str = 'feature/dev'
    base_branch: str = 'master'
    project_type: str = 'AUTO'
    ai_review_enabled: int = 0
    token: str = ''
    enabled: int = 1


class RepoTestIn(BaseModel):
    id: Optional[int] = None
    repo_url: str = ''
    token: str = ''


class RangeIn(BaseModel):
    repo_id: int
    base: str = ''
    head: str = ''


def _repo_row(db, repo_id):
    row = db.execute_single_query(
        'SELECT * FROM cw_repos WHERE id=%s AND is_del=0', (repo_id,))
    if not row:
        raise HTTPException(status_code=404, detail='仓库不存在')
    return row


def _repo_out(row):
    return {
        'id': row['id'], 'name': row['name'], 'repo_url': row['repo_url'],
        'dev_branch': row['dev_branch'], 'base_branch': row['base_branch'],
        'project_type': languages.normalize(row.get('project_type')),
        'project_type_label': languages.label(row.get('project_type')),
        'ai_review_enabled': int(row.get('ai_review_enabled') or 0),
        'enabled': row['enabled'], 'last_sha': row['last_sha'],
        'last_sync_time': str(row['last_sync_time']) if row.get('last_sync_time') else '',
        'has_token': bool(row.get('token_enc')),
        'create_time': str(row['create_time']) if row.get('create_time') else '',
    }


def _encrypt_token(db, plain):
    from src.ai import crypto
    return crypto.encrypt(plain)


@router.get('/repos')
def list_repos():
    with get_db_handler() as db:
        rows = db.execute_query(
            'SELECT * FROM cw_repos WHERE is_del=0 ORDER BY id DESC')
    return _ok([_repo_out(r) for r in rows], total=len(rows))


@router.post('/repos')
def save_repo(data: RepoIn, request: Request):
    uid = _uid(request)
    url = (data.repo_url or '').strip()
    if not (url.startswith(('http://', 'https://', 'ssh://', 'file://'))
            or url.startswith('git@')):
        raise HTTPException(status_code=400,
                            detail='仓库地址需以 http(s)://、ssh://、file:// 或 git@ 开头')
    if not (data.dev_branch or '').strip():
        raise HTTPException(status_code=400, detail='开发分支不能为空')
    ptype = languages.normalize(data.project_type)
    with get_db_handler() as db:
        if data.id:
            row = _repo_row(db, data.id)
            token_enc = row.get('token_enc') or ''
            if data.token and not data.token.startswith('****'):
                token_enc = _encrypt_token(db, data.token)
            db.execute_update(
                'UPDATE cw_repos SET name=%s, repo_url=%s, dev_branch=%s, base_branch=%s, '
                'project_type=%s, ai_review_enabled=%s, token_enc=%s, enabled=%s, '
                'update_time=NOW() WHERE id=%s',
                (data.name.strip(), url, data.dev_branch.strip(), data.base_branch.strip(),
                 ptype, int(bool(data.ai_review_enabled)), token_enc, data.enabled, data.id))
            return _ok({'id': data.id}, '已保存')
        token_enc = _encrypt_token(db, data.token) if data.token else ''
        rid = _insert_id(
            db,
            'INSERT INTO cw_repos (name, repo_url, dev_branch, base_branch, project_type, '
            'ai_review_enabled, token_enc, enabled, create_by, create_time, update_time) '
            'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(),NOW())',
            (data.name.strip() or url.rsplit('/', 1)[-1], url, data.dev_branch.strip(),
             data.base_branch.strip(), ptype, int(bool(data.ai_review_enabled)),
             token_enc, data.enabled, uid))
    return _ok({'id': rid}, '已新增')


@router.post('/repos/delete')
def delete_repo(data: dict):
    rid = data.get('id')
    if not rid:
        raise HTTPException(status_code=400, detail='缺少 id')
    with get_db_handler() as db:
        db.execute_update('UPDATE cw_repos SET is_del=1, update_time=NOW() WHERE id=%s', (rid,))
    return _ok(None, '已删除')


@router.post('/repos/test')
def test_repo(data: RepoTestIn):
    url = (data.repo_url or '').strip()
    token = data.token or ''
    if data.id and not token:
        with get_db_handler() as db:
            row = _repo_row(db, data.id)
            url = url or row['repo_url']
            token = _decode_token(row)
    if not url:
        raise HTTPException(status_code=400, detail='仓库地址为空')
    out = _git(None, ['ls-remote', '--heads', url], token=token, timeout=60)
    heads = [line.split('refs/heads/')[-1] for line in out.splitlines() if 'refs/heads/' in line]
    return _ok({'branches': heads[:50], 'count': len(heads)}, '连接成功')


# ---------------------------------------------------------------------------
# 同步 / 分析
# ---------------------------------------------------------------------------
@router.post('/repos/{repo_id}/sync')
def sync_repo(repo_id: int, request: Request):
    uid = _uid(request)
    with get_db_handler() as db:
        repo = _repo_row(db, repo_id)
    lock = _repo_lock(repo_id)
    if not lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail='该仓库正在同步，请稍候')
    try:
        path = _ensure_repo(repo)
        head = _rev(path, 'origin/' + repo['dev_branch']) or _rev(path, repo['dev_branch'])
        if not head:
            branches = _remote_branches(path)
            raise HTTPException(
                status_code=400,
                detail='开发分支不存在：%s；远端可用分支：%s。'
                       '如果这是本地新建的 dev 分支，请先在开发仓库执行 '
                       'git push -u origin %s' % (
                           repo['dev_branch'], '、'.join(branches) or '（读取失败）',
                           repo['dev_branch']))
        if repo['last_sha'] and repo['last_sha'] == head:
            return _ok({'unchanged': True, 'head_sha': head}, '代码无变化')

        if not repo['last_sha']:
            with get_db_handler() as db:
                db.execute_update(
                    'UPDATE cw_repos SET last_sha=%s, last_sync_time=NOW(), update_time=NOW() '
                    'WHERE id=%s', (head, repo_id))
            return _ok({'baseline': True, 'head_sha': head},
                       '已建立基线（%s）；下一次提交起开始分析' % head[:8])

        base = repo['last_sha']
        if not _is_ancestor(path, base, head):
            base = _fallback_base(path, head)
        _check_range(path, base, head)
        with get_db_handler() as db:
            result = _analyze(repo, path, base, head, db, uid)
            cid = _save_change(db, repo, base, head, result, uid)
            db.execute_update(
                'UPDATE cw_repos SET last_sha=%s, last_sync_time=NOW(), update_time=NOW() '
                'WHERE id=%s', (head, repo_id))
        return _ok({'change_id': cid, 'base_sha': base, 'head_sha': head,
                    'counts': result['counts'], 'risk_level': result['risk_level']},
                   '分析完成')
    finally:
        lock.release()


@router.post('/changes')
def create_change(data: RangeIn, request: Request):
    uid = _uid(request)
    if not data.base or not data.head:
        raise HTTPException(status_code=400, detail='请填写 base 和 head 提交')
    with get_db_handler() as db:
        repo = _repo_row(db, data.repo_id)
        exists = db.execute_single_query(
            'SELECT id FROM cw_changes WHERE repo_id=%s AND head_sha=%s',
            (data.repo_id, data.head))
        if exists:
            return _ok({'change_id': exists['id'], 'exists': True}, '该提交已分析过')
    path = _ensure_repo(repo)
    _check_range(path, data.base, data.head)
    with get_db_handler() as db:
        result = _analyze(repo, path, data.base, data.head, db, uid)
        cid = _save_change(db, repo, data.base, data.head, result, uid)
    return _ok({'change_id': cid, 'counts': result['counts'],
                'risk_level': result['risk_level']}, '分析完成')


@router.get('/changes')
def list_changes(pageNo: int = 1, pageSize: int = 20, repo_id: int = 0):
    pageNo = max(1, pageNo)
    pageSize = min(100, max(1, pageSize))
    where, params = 'c.status IN (1,3)', []
    if repo_id:
        where += ' AND c.repo_id=%s'
        params.append(repo_id)
    with get_db_handler() as db:
        total = db.execute_single_query(
            'SELECT COUNT(*) n FROM cw_changes c WHERE ' + where, tuple(params))['n']
        rows = db.execute_query(
            'SELECT c.*, r.name repo_name, r.repo_url FROM cw_changes c '
            'LEFT JOIN cw_repos r ON r.id=c.repo_id WHERE ' + where +
            ' ORDER BY c.id DESC LIMIT %s OFFSET %s',
            tuple(params) + (pageSize, (pageNo - 1) * pageSize))
    data = []
    for r in rows:
        data.append({
            'id': r['id'], 'repo_id': r['repo_id'], 'repo_name': r.get('repo_name') or '',
            'branch': r['branch'], 'base_sha': r['base_sha'], 'head_sha': r['head_sha'],
            'commit_count': r['commit_count'], 'file_count': r['file_count'],
            'insertions': r['insertions'], 'deletions': r['deletions'],
            'must_count': r['must_count'], 'suggest_count': r['suggest_count'],
            'new_count': r['new_count'], 'risk_level': r['risk_level'],
            'status': r['status'],
            'create_time': str(r['create_time']) if r.get('create_time') else '',
        })
    return _ok(data, total=total)


@router.get('/changes/{change_id}')
def get_change(change_id: int):
    with get_db_handler() as db:
        row = db.execute_single_query(
            'SELECT c.*, r.name repo_name, r.repo_url FROM cw_changes c '
            'LEFT JOIN cw_repos r ON r.id=c.repo_id WHERE c.id=%s', (change_id,))
        if not row:
            raise HTTPException(status_code=404, detail='变更记录不存在')
        files = db.execute_query(
            'SELECT path, status, additions, deletions, is_noise, patch FROM cw_files '
            'WHERE change_id=%s ORDER BY id', (change_id,))
    try:
        report = json.loads(row.get('report_json') or '{}')
    except Exception:
        report = {}
    return _ok({
        'change': {
            'id': row['id'], 'repo_id': row['repo_id'],
            'repo_name': row.get('repo_name') or '', 'repo_url': row.get('repo_url') or '',
            'branch': row['branch'], 'base_sha': row['base_sha'], 'head_sha': row['head_sha'],
            'commit_count': row['commit_count'], 'file_count': row['file_count'],
            'insertions': row['insertions'], 'deletions': row['deletions'],
            'must_count': row['must_count'], 'suggest_count': row['suggest_count'],
            'new_count': row['new_count'], 'risk_level': row['risk_level'],
            'status': row['status'], 'error_msg': row.get('error_msg') or '',
            'create_time': str(row['create_time']) if row.get('create_time') else '',
            'analyze_time': str(row['analyze_time']) if row.get('analyze_time') else '',
        },
        'files': files,
        'report': report,
    })


@router.post('/changes/{change_id}/analyze')
def reanalyze(change_id: int, request: Request):
    uid = _uid(request)
    with get_db_handler() as db:
        row = db.execute_single_query('SELECT * FROM cw_changes WHERE id=%s', (change_id,))
        if not row:
            raise HTTPException(status_code=404, detail='变更记录不存在')
        repo = _repo_row(db, row['repo_id'])
    path = _ensure_repo(repo)
    _check_range(path, row['base_sha'], row['head_sha'])
    with get_db_handler() as db:
        result = _analyze(repo, path, row['base_sha'], row['head_sha'], db, uid)
        db.execute_update('DELETE FROM cw_files WHERE change_id=%s', (change_id,))
        db.execute_update('DELETE FROM cw_impacts WHERE change_id=%s', (change_id,))
        db.execute_update(
            'UPDATE cw_changes SET commit_count=%s, file_count=%s, insertions=%s, deletions=%s, '
            'must_count=%s, suggest_count=%s, new_count=%s, risk_level=%s, status=1, error_msg=NULL, '
            'report_json=%s, report_md=%s, analyze_time=NOW() WHERE id=%s',
            (result['commit_count'], len(result['files']), result['counts']['insertions'],
             result['counts']['deletions'], result['counts']['must'], result['counts']['suggest'],
             result['counts']['new'], result['risk_level'], _snapshot(result),
             _build_md(result, repo), change_id))
        for f in result['files']:
            db.execute_update(
                'INSERT INTO cw_files (change_id, path, status, additions, deletions, patch, is_noise) '
                'VALUES (%s,%s,%s,%s,%s,%s,%s)',
                (change_id, f['path'], f['status'], f['additions'], f['deletions'],
                 f.get('patch') or '', f['is_noise']))
        for idx, i in enumerate(result['impacts']):
            db.execute_update(
                'INSERT INTO cw_impacts (change_id, kind, target_key, method, module, '
                'relation_label, level, file, line, reasons, sort) '
                'VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)',
                (change_id, i['kind'], i['target_key'][:300], i.get('method') or '',
                 (i.get('module') or '')[:100], i.get('relation_label') or '', i['level'],
                 (i.get('file') or '')[:300], i.get('line') or 0,
                 '；'.join(i.get('reasons') or []), idx))
    return _ok({'counts': result['counts'], 'risk_level': result['risk_level']}, '重新分析完成')


@router.get('/changes/{change_id}/report')
def get_report(change_id: int):
    with get_db_handler() as db:
        row = db.execute_single_query(
            'SELECT report_md FROM cw_changes WHERE id=%s', (change_id,))
    if not row or not row.get('report_md'):
        raise HTTPException(status_code=404, detail='报告不存在')
    return PlainTextResponse(row['report_md'], media_type='text/markdown; charset=utf-8')
