# -*- coding: utf-8 -*-
"""代码变更影响分析：多语言适配层。

把 PHP / Java 源码解析成 analyzer 里统一的 ModuleInfo / FuncInfo / ClassInfo，
这样 build_impacts / build_suggestions / 报告生成完全不感知语言差异。

约定
----
* parse_module(src, path, ctx, known, lang) -> analyzer.ModuleInfo | None
* tree_dump / body_dump 用「去掉注释、压缩空白」的规范化文本，
  用于判定「仅注释格式变化」以及函数体是否被真改
* 非 Python 语言**不使用**调用方传入的 known_tables —— 那是 test_admin 自己的
  information_schema，与被分析仓库不是同一个库；改为从源码启发式提表名
* 路由映射（PHP 的 route/*.php -> controller）按 head 提交树构建，
  base 侧新增/删除的路由由路由文件自身的 diff 检出

支持：AUTO（按文件后缀自动识别）/ PYTHON / PHP / JAVA
"""
import re
import subprocess
from collections import defaultdict

AUTO, PYTHON, PHP, JAVA = 'AUTO', 'PYTHON', 'PHP', 'JAVA'
SUPPORTED = (AUTO, PYTHON, PHP, JAVA)
EXTENSIONS = {PYTHON: ('.py',), PHP: ('.php',), JAVA: ('.java',)}
ALL_EXT = ('.py', '.php', '.java')
SKIP_DIR_RE = re.compile(
    r'(?i)(^|/)(vendor|node_modules|target|build|dist|\.git|__pycache__)/')
MAX_INDEX_FILES = 2200

LABELS = {
    AUTO: '自动识别', PYTHON: 'PYTHON', PHP: 'PHP', JAVA: 'JAVA',
}


def normalize(value):
    v = str(value or '').strip().upper()
    return v if v in SUPPORTED else AUTO


def label(value):
    return LABELS.get(normalize(value), '自动识别')


def is_source(path, lang):
    """该路径是否属于指定语言、且不是依赖/测试目录。"""
    if not path or SKIP_DIR_RE.search(path):
        return False
    low = path.lower()
    exts = EXTENSIONS.get(lang)
    if exts:
        return low.endswith(exts)
    return low.endswith(ALL_EXT)


# ---------------------------------------------------------------------------
# git 批量读取（大仓库避免上千次 git show 子进程）
# ---------------------------------------------------------------------------
def _git(repo, *args, timeout=120):
    p = subprocess.run(['git', '-C', repo, '-c', 'core.quotepath=false'] + list(args),
                       capture_output=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError('git %s 失败: %s' % (
            ' '.join(args), p.stderr.decode('utf-8', 'replace')[:300]))
    return p.stdout.decode('utf-8', 'replace')


def _tree(repo, ref):
    """返回 [(blob_oid, path)]，一次 ls-tree。"""
    out = []
    for line in _git(repo, 'ls-tree', '-r', ref, timeout=120).splitlines():
        if '\t' not in line:
            continue
        meta, path = line.split('\t', 1)
        parts = meta.split()
        if len(parts) == 3 and parts[1] == 'blob':
            out.append((parts[2], path))
    return out


def read_sources(repo, ref, entries, wanted):
    """用 git cat-file --batch 一次读取多个 blob，返回 {path: 文本}。"""
    wanted = [p for p in wanted if p]
    wanted_set = set(wanted)
    oid_by_path = {p: o for o, p in entries if p in wanted_set}
    ordered = [p for p in wanted if p in oid_by_path]
    if not ordered:
        return {}
    payload = ''.join(oid_by_path[p] + '\n' for p in ordered).encode('ascii')
    proc = subprocess.Popen(['git', '-C', repo, 'cat-file', '--batch'],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE)
    try:
        stdout, _ = proc.communicate(payload, timeout=300)
    except subprocess.TimeoutExpired:
        proc.kill()
        raise RuntimeError('git cat-file --batch 超时')
    res, pos = {}, 0
    for path in ordered:
        nl = stdout.find(b'\n', pos)
        if nl < 0:
            break
        header = stdout[pos:nl].decode('ascii', 'replace').split()
        pos = nl + 1
        if len(header) < 3 or header[1] != 'blob':
            continue
        size = int(header[2])
        res[path] = stdout[pos:pos + size].decode('utf-8', 'replace')
        pos += size + 1  # 每个对象后有一个分隔换行
    return res


def _show(repo, ref, path):
    p = subprocess.run(['git', '-C', repo, 'show', '%s:%s' % (ref, path)],
                       capture_output=True, timeout=30)
    return p.stdout.decode('utf-8', 'replace') if p.returncode == 0 else ''


# ---------------------------------------------------------------------------
# 语言识别 / 文件扫描 / 上下文
# ---------------------------------------------------------------------------
def detect(paths):
    counts = {PYTHON: 0, PHP: 0, JAVA: 0}
    for p in paths:
        if SKIP_DIR_RE.search(p):
            continue
        low = p.lower()
        for lang, exts in EXTENSIONS.items():
            if low.endswith(exts):
                counts[lang] += 1
                break
    if not any(counts.values()):
        return PYTHON, counts  # 保持旧行为
    return max(counts, key=lambda k: counts[k]), counts


def _priority(path, lang):
    low = '/' + path.lower()
    if lang == PHP:
        keys = ('/route/', '/controller/', '/validate/', '/services/', '/dao/', '/model/')
    elif lang == JAVA:
        keys = ('/controller/', '/service/', '/mapper/', '/dto/', '/entity/', '/model/')
    else:
        keys = ('/routers/', '/ai/', '/')
    for i, k in enumerate(keys):
        if k in low:
            return (i, path)
    return (len(keys), path)


def scan_paths(entries, lang):
    paths = [p for _, p in entries if is_source(p, lang)]
    if lang == PYTHON:
        src_only = [p for p in paths if p.startswith('src/')]
        if src_only:
            paths = src_only  # 与旧实现一致：FastAPI 只索引 src/
    paths = sorted(paths, key=lambda p: _priority(p, lang))
    return paths[:MAX_INDEX_FILES]


def build_context(repo, ref, project_type=AUTO):
    """一次扫描提交树，产出索引所需的全部上下文。"""
    requested = normalize(project_type)
    entries = _tree(repo, ref)
    all_paths = [p for _, p in entries]
    if requested == AUTO:
        lang, counts = detect(all_paths)
    else:
        lang, counts = requested, {}

    paths = scan_paths(entries, lang)
    sources = read_sources(repo, ref, entries, paths)
    ctx = {
        'lang': lang, 'requested': requested, 'detect_counts': counts,
        'paths': paths, 'sources': sources, 'entries': entries,
        'mounts': {}, 'php_routes': {}, 'controller_root': '',
    }

    if lang == PYTHON:
        from . import analyzer
        main_src = sources.get('src/main.py') or _show(repo, ref, 'src/main.py')
        ctx['mounts'] = analyzer.load_mounts(main_src or '')
    elif lang == PHP:
        route_paths = [p for p in all_paths if is_source(p, PHP) and _is_route_file(p)]
        route_sources = read_sources(repo, ref, entries, route_paths)
        ctx['controller_root'] = _controller_root(all_paths)
        ctx['php_routes'] = parse_php_routes(route_sources, ctx['controller_root'])
        names = set()
        for text in sources.values():
            for nm in re.finditer(r'(?im)^\s*class\s+([A-Za-z_]\w*Validate)\b', text):
                names.add(nm.group(1))
        ctx['validate_names'] = names
    return ctx


# ---------------------------------------------------------------------------
# 词法：注释剥离 / 掩码（用于括号配平）
# ---------------------------------------------------------------------------
def _lex(src, php, mask_strings):
    """返回与 src 等长的文本。注释恒被抹成空格；字符串内容按需抹掉。

    引号本身保留，换行保留（保证行号与括号配平不受影响）。
    """
    out = list(src)
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c in ("'", '"'):
            quote, i = c, i + 1
            while i < n:
                if src[i] == '\\' and i + 1 < n:
                    if mask_strings:
                        out[i] = out[i + 1] = ' '
                    i += 2
                    continue
                if src[i] == quote:
                    i += 1
                    break
                if mask_strings and src[i] != '\n':
                    out[i] = ' '
                i += 1
            continue
        if src.startswith('//', i) or (php and c == '#'):
            j = src.find('\n', i)
            j = n if j < 0 else j
            for k in range(i, j):
                if out[k] != '\n':
                    out[k] = ' '
            i = j
            continue
        if src.startswith('/*', i):
            j = src.find('*/', i + 2)
            j = n if j < 0 else j + 2
            for k in range(i, j):
                if out[k] != '\n':
                    out[k] = ' '
            i = j
            continue
        i += 1
    return ''.join(out)


def norm_text(src, php=True):
    """去注释 + 压缩空白，保留字符串内容（字符串改动仍然算「真改」）。"""
    return re.sub(r'\s+', ' ', _lex(src, php, False)).strip()


def _blocks(src, php, head_re):
    """按 head_re 找函数/方法头，用掩码括号配平切出函数体。

    返回 [{name, args, lineno, end_lineno, body, head}]
    """
    masked = _lex(src, php, True)
    out = []
    for m in head_re.finditer(masked):
        brace = masked.find('{', m.end())
        if brace < 0 or brace - m.end() > 300:
            continue
        depth, end = 0, None
        for i in range(brace, len(masked)):
            if masked[i] == '{':
                depth += 1
            elif masked[i] == '}':
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end is None:
            continue
        out.append({
            'name': m.group(1), 'args': (m.group(2) or '').strip(),
            'lineno': src.count('\n', 0, m.start()) + 1,
            'end_lineno': src.count('\n', 0, end) + 1,
            'body': src[m.start():end + 1], 'head': m.group(0),
        })
    return out


def _calls(text, php=True):
    """近似提取调用名，供 index.callers 反查调用链。"""
    out = set()
    for m in re.finditer(r'->\s*([A-Za-z_]\w*)\s*\(', text):
        out.add(m.group(1))
    for m in re.finditer(r'([A-Za-z_]\w*)::\s*([A-Za-z_]\w*)\s*\(', text):
        out.add(m.group(2))
    for m in re.finditer(r'(?<![\w$>:])([A-Za-z_]\w{2,})\s*\(', text):
        name = m.group(1)
        noise = {'if', 'for', 'foreach', 'while', 'switch', 'catch', 'isset', 'empty',
                 'array', 'count', 'strlen', 'intval', 'implode', 'explode', 'is_array',
                 'return', 'echo', 'print', 'function', 'new', 'class', 'public',
                 'private', 'protected', 'static', 'try', 'list', 'unset', 'sprintf',
                 'json_encode', 'json_decode', 'trim', 'in_array', 'strtolower'}
        if name not in noise:
            out.add(name)
    return out


# ---------------------------------------------------------------------------
# PHP（ThinkPHP 6 风格）
# ---------------------------------------------------------------------------
PHP_VERBS = {
    'get': 'GET', 'post': 'POST', 'put': 'PUT', 'delete': 'DELETE',
    'patch': 'PATCH', 'options': 'OPTIONS', 'head': 'HEAD', 'any': 'ANY',
    'rule': 'ANY',
}
PHP_FUNC_HEAD = re.compile(
    r'(?im)^[ \t]*(?:(?:public|protected|private|static|final|abstract)\s+)*'
    r'function\s+([A-Za-z_]\w*)\s*\(([^)]*)\)')
PHP_TABLE_CALL = re.compile(
    r"(?i)\b(?:Db::(?:name|table)|->(?:name|table)|db)\s*\(\s*['\"]([A-Za-z_]\w*)['\"]\s*\)")
PHP_DB_CHAIN = re.compile(
    r"(?i)Db::(?:name|table)\s*\(\s*['\"]([A-Za-z_]\w*)['\"]\s*\)([^;]{0,400})")
PHP_SQL_LITERAL = re.compile(
    r"(?is)['\"]((?:select|insert\s+into|replace\s+into|update|delete\s+from)\b[^'\"]{6,})['\"]")
SQL_TABLE_FROM = re.compile(
    r'(?is)\b(?:insert\s+(?:ignore\s+)?into|replace\s+into|update|delete\s+from|from|join)'
    r'\s+[`"\[\]]?([A-Za-z_][A-Za-z0-9_]*)')


def _is_route_file(path):
    low = '/' + path.lower()
    if '/config/' in low:  # config/route.php 是框架配置，不是路由定义
        return False
    return '/route/' in low or low.endswith('/route.php')


def _controller_root(paths):
    roots = set()
    for p in paths:
        m = re.search(r'(?i)^(.*?/app/controller/)', p)
        if m:
            roots.add(m.group(1))
    return sorted(roots)[0] if roots else ''


def _parse_route_file(src, path):
    """解析 Route::group(...) 嵌套 + Route::get/post(...) 定义。"""
    stack, routes = [], []
    group_open = re.compile(r"(?i)Route::group\s*\(\s*(?:['\"]([^'\"]*)['\"])?")
    verb_re = re.compile(
        r"(?i)Route::(get|post|put|delete|patch|any|rule|options|head)\s*\(\s*"
        r"['\"]([^'\"]+)['\"]\s*,\s*([^,)]*)")
    close_re = re.compile(r'^\s*\}\s*\)')
    prefix_re = re.compile(r"(?i)->prefix\s*\(\s*['\"]([^'\"]+)['\"]")
    for lineno, line in enumerate(src.splitlines(), 1):
        if 'Route::group' in line and 'function' in line:
            m = group_open.search(line)
            gp = (m.group(1) if m and m.group(1) else '').strip('/')
            stack.append({'path': gp, 'routes': []})
            continue
        vm = verb_re.search(line)
        if vm:
            parts = tuple(g['path'] for g in stack if g['path']) + (vm.group(2).strip('/'),)
            url = '/' + '/'.join(p for p in parts if p)
            url = re.sub(r':([A-Za-z_]\w*)', r'{\1}', url)
            rec = {
                'url': url, 'method': PHP_VERBS.get(vm.group(1).lower(), 'ANY'),
                'target': vm.group(3).strip().strip("'\""),
                'line': lineno, 'file': path, 'prefix': None, 'text': line.strip(),
            }
            (stack[-1]['routes'] if stack else routes).append(rec)
            continue
        if close_re.match(line):
            if not stack:
                continue
            group = stack.pop()
            pm = prefix_re.search(line)
            if pm:
                for r in group['routes']:
                    if not r.get('prefix'):
                        r['prefix'] = pm.group(1)
            (stack[-1]['routes'] if stack else routes).extend(group['routes'])
    return routes


def _resolve_target(target, prefix, root):
    """把 Route::get('list', '/list') + prefix 解析成 (控制器文件, 方法名)。"""
    t = (target or '').strip().strip("'\"")
    cls, action = prefix or '', 'index'
    if t.startswith('/'):
        action = t.lstrip('/').split('/')[-1] or 'index'
    elif '@' in t:
        cls, action = t.split('@', 1)
    elif '/' in t:
        left, action = t.rsplit('/', 1)
        if left and ('.' in left or not cls):
            cls = left
    elif t:
        action = t
    segs = [s for s in cls.replace('.', '/').replace('\\', '/').split('/') if s]
    if not segs or not root:
        return '', action
    return root + '/'.join(segs) + '.php', action


def parse_php_routes(route_sources, controller_root):
    """返回 {'by_controller': {(file, action): [route]}, 'all': [route]}。"""
    all_routes = []
    for path, src in (route_sources or {}).items():
        all_routes.extend(_parse_route_file(src, path))
    by_controller = defaultdict(list)
    for r in all_routes:
        cfile, action = _resolve_target(r['target'], r.get('prefix'), controller_root)
        r['controller_file'], r['action'] = cfile, action
        if cfile:
            by_controller[(cfile, action)].append(r)
    return {'by_controller': dict(by_controller), 'all': all_routes}


def _php_sql_by_table(text):
    out = {}
    for m in PHP_DB_CHAIN.finditer(text):
        table, chain = m.group(1).lower(), m.group(2)
        verb = 'select'
        for op, pat in (('insert', r'->\s*insert'), ('update', r'->\s*update'),
                        ('delete', r'->\s*delete')):
            if re.search(pat, chain, re.I):
                verb = op
                break
        stmt = {
            'insert': 'insert into %s', 'update': 'update %s',
            'delete': 'delete from %s', 'select': 'select * from %s',
        }[verb] % table
        out.setdefault(table, []).append(stmt)
    for s in PHP_SQL_LITERAL.findall(text):
        flat = re.sub(r'\s+', ' ', s).strip()
        for tm in SQL_TABLE_FROM.finditer(flat):
            out.setdefault(tm.group(1).lower(), []).append(flat[:300])
    for tm in PHP_TABLE_CALL.finditer(text):
        out.setdefault(tm.group(1).lower(), ['select * from %s' % tm.group(1).lower()])
    return {t: sorted(set(v)) for t, v in out.items()}


def _php_params(args_text, body):
    """PHP 控制器方法没有类型化入参，主要从 request->param([...]) 取 Query 名。"""
    out, seen = [], set()

    def add(name, type_str=''):
        name = (name or '').strip()
        if not name or name in seen or not re.match(r'^\w+$', name):
            return
        seen.add(name)
        out.append({'name': name, 'type': type_str, 'default': ''})

    for m in re.finditer(r'\$\{?(\w+)', args_text or ''):
        name = m.group(1)
        if re.search(r'(?i)(Services?|Dao|Model|App|Request|Controller|Validate)$', name):
            continue
        add(name)
    for m in re.finditer(r"(?i)(?:request|input)\s*(?:->|\.)?\s*param\s*\(\s*\[([^\]]*)\]",
                         body or ''):
        for q in re.findall(r"['\"]([A-Za-z_]\w*)['\"]", m.group(1)):
            add(q)
    for m in re.finditer(r"(?i)param\s*\(\s*['\"]([A-Za-z_]\w*)['\"]\s*\)", body or ''):
        add(m.group(1))
    return out[:12]


def _validate_meta(rule_text):
    rules = [x.strip() for x in (rule_text or '').split('|')]
    meta = {'type': 'str', 'required': False, 'default': '',
            'constraints': {}, 'enum': []}
    for r in rules:
        low = r.lower()
        if low in ('require', 'required', 'notblank', 'notnull'):
            meta['required'] = True
        elif low.startswith('max:'):
            meta['constraints']['max_length'] = r.split(':', 1)[1]
        elif low.startswith('min:') and re.match(r'^\d+$', r.split(':', 1)[1]):
            meta['constraints']['ge'] = r.split(':', 1)[1]
        elif low.startswith('in:'):
            meta['enum'] = [v for v in r.split(':', 1)[1].split(',') if v]
        elif low in ('integer', 'number', 'float', 'int'):
            meta['type'] = 'int'
        elif low in ('array', 'list'):
            meta['type'] = 'list'
        elif low in ('boolean', 'bool'):
            meta['type'] = 'bool'
    return meta


def _php_class_rules(src, class_name):
    m = re.search(r'(?is)protected\s+\$rule\s*=\s*\[(.*?)\]\s*;', src)
    fields, field_meta = {}, {}
    if not m:
        return fields, field_meta
    for fm in re.finditer(r"['\"]([^'\"]+)['\"]\s*=>\s*['\"]([^'\"]*)['\"]", m.group(1)):
        key, rule = fm.group(1), fm.group(2)
        name = key.split('|')[0].strip()
        if not re.match(r'^\w+$', name):
            continue
        fields[name] = (rule, '')
        field_meta[name] = _validate_meta(rule)
    return fields, field_meta


def _guess_validate(body, class_name, ctx):
    m = re.search(r'\b([A-Za-z_]\w*Validate)::class', body or '')
    if m:
        return m.group(1)
    m = re.search(r"(?i)validate\s*\([^)]*['\"]([A-Za-z_]\w*Validate)['\"]", body or '')
    if m:
        return m.group(1)
    if class_name:
        guess = class_name + 'Validate'
        if guess in (ctx.get('validate_names') or set()):
            return guess
    return ''


def parse_php_module(src, path, ctx, known=None):
    from . import analyzer as A
    info = A.ModuleInfo(path=path)
    info.tree_dump = norm_text(src, php=True)
    routes_ctx = ctx.get('php_routes') or {}
    by_ctrl = routes_ctx.get('by_controller') or {}

    if _is_route_file(path):
        # 必须解析「当前这一版」源码：若复用 ctx 里按 head 构建的路由表，
        # base/head 两版会解析出相同结果，新增/删除的路由就检不出来了。
        for r in _parse_route_file(src, path):
            fi = A.FuncInfo(
                file=path, qual='route::' + r['url'], name=r.get('action') or r['url'],
                lineno=r['line'], end_lineno=r['line'], args='',
                body_dump=norm_text(r.get('text') or '', php=True),
                calls=set(), tables=set(), sql_by_table={}, params=[],
                is_route=True, methods=(r['method'],),
                path=r['url'], full_path=r['url'], router_var='')
            info.routes.append(fi)
        # 路由文件里没解析出 Route::* 时，按普通函数记录，仍能被 diff 检出
        if not info.routes:
            for fi in _php_route_fallback(src, path):
                info.funcs[fi.qual] = fi
        return info

    cm = re.search(r'(?im)^\s*(?:final\s+|abstract\s+)?class\s+([A-Za-z_]\w*)', src)
    class_name = cm.group(1) if cm else ''
    for b in _blocks(src, True, PHP_FUNC_HEAD):
        body = b['body']
        sql_map = _php_sql_by_table(body)
        fi = A.FuncInfo(
            file=path, qual=b['name'], name=b['name'],
            lineno=b['lineno'], end_lineno=b['end_lineno'],
            args=b['args'], body_dump=norm_text(body, php=True),
            calls=_calls(body), tables=set(sql_map), sql_by_table=sql_map,
            params=[])
        hit = by_ctrl.get((path, b['name']))
        if hit:
            r = hit[0]
            fi.is_route = True
            fi.methods = (r['method'],)
            fi.path = r['url']
            fi.full_path = r['url']
            fi.params = _php_params(b['args'], body)
            fi.body_model = _guess_validate(body, class_name, ctx)
        if fi.is_route:
            info.routes.append(fi)
        else:
            info.funcs[fi.qual] = fi

    if class_name:
        fields, meta = _php_class_rules(src, class_name)
        if fields:
            info.classes[class_name] = A.ClassInfo(
                file=path, name=class_name, fields=fields,
                bases=['Validate'], field_meta=meta)
    return info


def _php_route_fallback(src, path):
    """路由文件里没解析出 Route::* 时的兜底：整行作为函数体，避免完全漏检。"""
    from . import analyzer as A
    out = []
    for i, line in enumerate(src.splitlines(), 1):
        if not re.search(r'(?i)\bRoute\b', line):
            continue
        out.append(A.FuncInfo(
            file=path, qual='route::L%d' % i, name='L%d' % i,
            lineno=i, end_lineno=i, args='', body_dump=norm_text(line, php=True),
            calls=set(), tables=set(), sql_by_table={}, params=[]))
    return out


# ---------------------------------------------------------------------------
# JAVA（Spring Boot 注解风格）
# ---------------------------------------------------------------------------
JAVA_MAPPING = {
    'getmapping': 'GET', 'postmapping': 'POST', 'putmapping': 'PUT',
    'deletemapping': 'DELETE', 'patchmapping': 'PATCH', 'requestmapping': 'ANY',
}
# 注意：正则不能消耗方法体的 `{`，否则 _blocks 会从下一个 `{` 开始配平，串到后面的方法
JAVA_FUNC_HEAD = re.compile(
    r'(?m)^[ \t]*(?:public|protected)[ \t]+[^\n;{}()]+?[ \t]+'
    r'([A-Za-z_]\w*)[ \t]*\(([^)]*)\)[ \t]*(?:throws[^\n{]+)?')
JAVA_ANNOTATION = re.compile(
    r'@(Get|Post|Put|Delete|Patch|Request)Mapping\s*\(([^)]*)\)', re.I)
JAVA_PARAM = re.compile(
    r'(?:@(?:PathVariable|RequestParam|RequestBody|ModelAttribute)\s*(?:\([^)]*\))?\s*)?'
    r'([\w<>\[\],.?]+)\s+(\w+)')
JAVA_FIELD = re.compile(
    r'(?m)^\s*(?:@[\w.]+(?:\([^)]*\))?\s*)*(?:private|protected|public)\s+'
    r'([\w<>\[\],.?]+)\s+(\w+)\s*(?:=[^;]*)?;')


def _java_field_meta(src, field_line):
    anns = ' '.join(m.group(0) for m in
                    re.finditer(r'@[\w.]+(?:\([^)]*\))?', field_line))
    meta = {'type': 'str', 'required': False, 'default': '',
            'constraints': {}, 'enum': []}
    if re.search(r'(?i)@(NotNull|NotBlank|NotEmpty)', anns):
        meta['required'] = True
    mm = re.search(r'(?i)@Size\s*\([^)]*max\s*=\s*(\d+)', anns)
    if mm:
        meta['constraints']['max_length'] = mm.group(1)
    mi = re.search(r'(?i)@Min\s*\(\s*(\d+)', anns)
    if mi:
        meta['constraints']['ge'] = mi.group(1)
    ma = re.search(r'(?i)@Max\s*\(\s*(\d+)', anns)
    if ma:
        meta['constraints']['le'] = ma.group(1)
    if re.search(r'(?i)\b(int|long|Integer|Long|double|float)\b', field_line):
        meta['type'] = 'int'
    if re.search(r'(?i)\b(boolean|Boolean)\b', field_line):
        meta['type'] = 'bool'
    if re.search(r'(?i)\b(List|Set|Collection)\b', field_line):
        meta['type'] = 'list'
    return meta


def _java_sql_by_table(text):
    out = {}
    for m in re.finditer(r'(?i)@(?:TableName|Table)\s*\(\s*(?:name\s*=\s*)?["\']([\w.]+)["\']', text):
        out.setdefault(m.group(1).lower(), []).append('select * from %s' % m.group(1).lower())
    for s in re.findall(r'"((?:select|insert\s+into|update|delete\s+from)\b[^"]{6,})"',
                        text, re.I | re.S):
        flat = re.sub(r'\s+', ' ', s).strip()
        for tm in SQL_TABLE_FROM.finditer(flat):
            out.setdefault(tm.group(1).lower(), []).append(flat[:300])
    return {t: sorted(set(v)) for t, v in out.items()}


def _java_params(args_text):
    out = []
    for part in (args_text or '').split(','):
        part = part.strip()
        if not part:
            continue
        m = JAVA_PARAM.search(part)
        if not m:
            continue
        type_str, name = m.group(1), m.group(2)
        if re.search(r'(?i)(Request|Response|Session|Principal|Model|BindingResult)$', type_str):
            continue
        out.append({'name': name, 'type': type_str, 'default': ''})
    return out[:12]


def parse_java_module(src, path, ctx, known=None):
    from . import analyzer as A
    info = A.ModuleInfo(path=path)
    info.tree_dump = norm_text(src, php=False)
    cm = re.search(r'(?m)^\s*(?:public\s+)?(?:final\s+)?class\s+([A-Za-z_]\w*)', src)
    class_name = cm.group(1) if cm else ''
    class_mapping = ''
    cmm = re.search(r'@RequestMapping\s*\(\s*["\']([^"\']+)["\']', src)
    if cmm and re.search(r'@(?:RestController|Controller|RequestMapping)', src[:cmm.start()]):
        class_mapping = cmm.group(1)
    is_controller = bool(re.search(r'@(?:RestController|Controller)\b', src))

    if not is_controller:
        fields, meta = {}, {}
        for fm in JAVA_FIELD.finditer(src):
            type_str, name = fm.group(1), fm.group(2)
            fields[name] = (type_str, '')
            meta[name] = _java_field_meta(src, fm.group(0))
        if class_name and fields:
            info.classes[class_name] = A.ClassInfo(
                file=path, name=class_name, fields=fields,
                bases=['BaseModel'], field_meta=meta)
        return info

    lines = src.splitlines()
    for b in _blocks(src, False, JAVA_FUNC_HEAD):
        before = '\n'.join(lines[max(0, b['lineno'] - 12):b['lineno']])
        am = None
        for am in JAVA_ANNOTATION.finditer(before):
            pass  # 取方法上方最近的一个映射注解
        if not am:
            continue
        sub = re.search(r'["\']([^"\']*)["\']', am.group(2))
        sub = sub.group(1) if sub else ''
        method = JAVA_MAPPING.get(am.group(1).lower() + 'mapping', 'ANY') \
            if am.group(1).lower() + 'mapping' in JAVA_MAPPING else 'ANY'
        full = re.sub(r'/+', '/', (class_mapping + '/' + sub).strip()) or '/'
        if not full.startswith('/'):
            full = '/' + full
        full = re.sub(r'\{(\w+):[^}]+\}', r'{\1}', full)
        sql_map = _java_sql_by_table(b['body'])
        fi = A.FuncInfo(
            file=path, qual=b['name'], name=b['name'],
            lineno=b['lineno'], end_lineno=b['end_lineno'],
            args=b['args'], body_dump=norm_text(b['body'], php=False),
            calls=_calls(b['body'], php=False), tables=set(sql_map),
            sql_by_table=sql_map, params=_java_params(b['args']),
            is_route=True, methods=(method,), path=sub, full_path=full,
            router_var=class_name)
        info.routes.append(fi)
    return info


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------
def parse_module(src, path, ctx, known, lang=None):
    if src is None:
        return None
    lang = normalize(lang or (ctx or {}).get('lang') or AUTO)
    if lang == AUTO:
        raise ValueError('parse_module 需要已解析出的具体语言')
    if lang == PHP:
        return parse_php_module(src, path, ctx or {}, known)
    if lang == JAVA:
        return parse_java_module(src, path, ctx or {}, known)
    from . import analyzer
    return analyzer.parse_module(src, path, (ctx or {}).get('mounts') or {}, known)


def lang_notes(lang):
    """写进 Markdown 报告的「口径与局限」。"""
    common = [
        '调用链：按函数名近似匹配（深度上限 3）；非 PYTHON 语言对 `list/detail/create` 等'
        '通用方法名限流为最多 3 个路由并降级为「建议」，避免同名误连淹没报告。',
    ]
    if lang == PHP:
        return [
            '路由解析：ThinkPHP `Route::group(...)->prefix(...)` + `Route::get/post/...`；'
            '单行 group / 动态拼接路由不识别。',
            '控制器定位：按 `prefix` + 路由第二参数反查 `app/controller/` 下的类方法；'
            '闭包路由、资源路由(`Route::resource`)不识别。',
            '数据表：识别 `Db::name/table(...)` 链式 insert/update/delete/select 与原生 SQL；'
            '不使用本系统数据库的表名白名单（与被分析仓库不是同一个库）。',
            '字段约束：来自 `*Validate` 类的 `protected $rule`（require/max/min/in 等）。',
        ] + common
    if lang == JAVA:
        return [
            '路由解析：Spring `@RestController` + 类/方法级 `@RequestMapping` 与 `@GetMapping` 等；'
            'XML 配置路由、动态注册不识别。',
            '字段约束：来自 DTO/Entity 字段上的 `@NotNull/@Size/@Min/@Max`。',
            '数据表：识别 `@TableName/@Table` 与原生 SQL 字符串；MyBatis XML 未解析。',
        ] + common
    return [
        '路由解析：`@router.*` + `APIRouter(prefix=...)` + `main.py include_router(prefix=...)`；'
        '动态拼接路径不识别。',
    ]
