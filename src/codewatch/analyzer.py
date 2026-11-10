# -*- coding: utf-8 -*-
"""代码变更影响分析（只读静态分析，不执行用例）。

输入：git 仓库本地缓存目录 + base/head 两个提交 + 项目类型
输出：结构化影响面（接口/模型/表/共享函数）+ 每个接口的具体回归建议

依赖：git 可执行文件、Python 标准库 ast。不依赖数据库；覆盖信息由调用方通过 assets 传入。
多语言解析在 languages.py，按仓库的 project_type 分派（AUTO/PYTHON/PHP/JAVA）。
"""
import ast
import copy
import re
import subprocess
from collections import defaultdict, deque
from dataclasses import dataclass, field

from . import languages

HTTP_METHODS = {'get', 'post', 'put', 'delete', 'patch', 'head', 'options'}
WRITE_METHODS = {'POST', 'PUT', 'DELETE', 'PATCH'}
BUILTIN_CALL_NAMES = {
    'str', 'int', 'float', 'bool', 'list', 'dict', 'set', 'tuple', 'len', 'range',
    'print', 'super', 'getattr', 'setattr', 'hasattr', 'isinstance', 'issubclass',
    'open', 'enumerate', 'zip', 'sorted', 'min', 'max', 'sum', 'any', 'all', 'type',
    'repr', 'format', 'map', 'filter', 'next', 'iter', 'id', 'hash', 'abs', 'round',
}
SQL_TABLE_RE = re.compile(
    r'(?is)\b(?:insert\s+(?:ignore\s+)?into|replace\s+into|update|delete\s+from|from|join)'
    r'\s+[`"\[\]]?([A-Za-z_][A-Za-z0-9_]*)'
)
WRITE_TABLE_RE = re.compile(
    r'(?is)\b(?:insert\s+(?:ignore\s+)?into|replace\s+into|update|delete\s+from)'
    r'\s+[`"\[\]]?([A-Za-z_][A-Za-z0-9_]*)'
)
MODULE_BLACKLIST = {
    'routers', 'src', 'fastapi', 'pydantic', 'typing', 'datetime', 'collections',
    'json', 'os', 'sys', 're', 'time', 'logging', 'pathlib', 'copy', 'math',
    'mysql', 'pymysql', 'yaml', 'httpx', 'openai', 'base', 'request',
}
# 非 Python 语言（尤其 ThinkPHP）里这些方法名在几百个类中重复，
# 按函数名反查调用链会把全仓路由都连上，需要限流 + 降级为「建议」
GENERIC_FUNC_NAMES = {
    'list', 'create', 'update', 'delete', 'detail', 'info', 'index', 'get', 'set',
    'save', 'add', 'edit', 'show', 'count', 'find', 'select', 'insert', 'search',
    'read', 'write', 'handle', 'run', 'execute', 'build', 'format', 'check', 'data',
}
MAX_CALLER_ROUTES = 8

MODULE_KEYWORDS = {
    'testcases': '用例',
    'product-function-points': '功能点',
    'automation': '接口',
    'ai': 'AI',
    'tasks': '任务',
    'devprocess': '流程',
    'travel': '行程',
    'users': '用户',
    'menus': '菜单',
    'upload': '导入',
}


# ---------------------------------------------------------------------------
# git
# ---------------------------------------------------------------------------
def git(repo, *args, timeout=60):
    p = subprocess.run(['git', '-C', repo, '-c', 'core.quotepath=false'] + list(args),
                       capture_output=True, timeout=timeout)
    if p.returncode != 0:
        raise RuntimeError('git %s 失败: %s' % (' '.join(args), p.stderr.decode('utf-8', 'replace')[:500]))
    return p.stdout.decode('utf-8', 'replace')


def git_show(repo, ref, path):
    p = subprocess.run(['git', '-C', repo, 'show', '%s:%s' % (ref, path)], capture_output=True)
    return p.stdout.decode('utf-8', 'replace') if p.returncode == 0 else None


# ---------------------------------------------------------------------------
# AST 工具
# ---------------------------------------------------------------------------
class _StripDoc(ast.NodeTransformer):
    @staticmethod
    def _strip(node):
        body = getattr(node, 'body', None)
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
                and isinstance(body[0].value.value, str):
            node.body = body[1:] or [ast.Pass()]

    def visit_Module(self, node):
        self._strip(node)
        self.generic_visit(node)
        return node

    def visit_FunctionDef(self, node):
        self._strip(node)
        self.generic_visit(node)
        return node

    def visit_AsyncFunctionDef(self, node):
        self._strip(node)
        self.generic_visit(node)
        return node

    def visit_ClassDef(self, node):
        self._strip(node)
        self.generic_visit(node)
        return node


def node_dump(node):
    return ast.dump(_StripDoc().visit(copy.deepcopy(node)),
                    annotate_fields=False, include_attributes=False)


def const_str(node):
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def call_names(node):
    out = set()
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        f = sub.func
        if isinstance(f, ast.Name) and f.id not in BUILTIN_CALL_NAMES:
            out.add(f.id)
        elif isinstance(f, ast.Attribute):
            out.add(f.attr)
    return out


def extract_tables(text, known):
    tables = set()
    for m in SQL_TABLE_RE.finditer(text):
        name = m.group(1).lower()
        if known:
            if name in known:
                tables.add(name)
        elif '_' in name and name not in MODULE_BLACKLIST:
            tables.add(name)
    return tables


def sql_strings(node):
    for sub in ast.walk(node):
        if isinstance(sub, ast.Constant) and isinstance(sub.value, str) \
                and re.search(r'(?i)\b(select|insert|update|delete)\b', sub.value):
            yield sub.value


def sql_by_table(node, known):
    out = {}
    for s in sql_strings(node):
        for t in extract_tables(s, known):
            out.setdefault(t, []).append(re.sub(r'\s+', ' ', s).strip())
    for t in out:
        out[t] = sorted(set(out[t]))
    return out


def ops_of_table(text, table):
    ops = set()
    for op, pattern in (
        ('insert', r'(?is)\binsert\s+(?:ignore\s+)?into\s+[`"\[\]]?%s\b'),
        ('update', r'(?is)\bupdate\s+[`"\[\]]?%s\b'),
        ('delete', r'(?is)\bdelete\s+from\s+[`"\[\]]?%s\b'),
        ('select', r'(?is)\b(?:from|join)\s+[`"\[\]]?%s\b'),
    ):
        if re.search(pattern % re.escape(table), text):
            ops.add(op)
    return ops


def parse_params(fn):
    out = []
    args = fn.args
    positional = list(args.posonlyargs) + list(args.args)
    defaults = [None] * (len(positional) - len(args.defaults)) + list(args.defaults)
    for arg, default in zip(positional, defaults):
        out.append({'name': arg.arg,
                    'type': ast.unparse(arg.annotation) if arg.annotation else '',
                    'default': ast.unparse(default) if default is not None else ''})
    for arg, default in zip(args.kwonlyargs, args.kw_defaults):
        out.append({'name': arg.arg,
                    'type': ast.unparse(arg.annotation) if arg.annotation else '',
                    'default': ast.unparse(default) if default is not None else ''})
    return out


def literal_values(ann_node):
    if isinstance(ann_node, ast.Subscript) and isinstance(ann_node.value, ast.Name) \
            and ann_node.value.id == 'Literal' and isinstance(ann_node.slice, ast.Tuple):
        return [const_str(e) for e in ann_node.slice.elts if const_str(e) is not None]
    return []


def parse_field_meta(ann_node, value_node):
    meta = {
        'type': ast.unparse(ann_node),
        'required': value_node is None,
        'default': ast.unparse(value_node) if value_node is not None else '',
        'constraints': {},
        'enum': literal_values(ann_node),
    }
    if isinstance(value_node, ast.Call) and isinstance(value_node.func, ast.Name) \
            and value_node.func.id == 'Field':
        if value_node.args and isinstance(value_node.args[0], ast.Constant) \
                and value_node.args[0].value is Ellipsis:
            meta['required'] = True
        for kw in value_node.keywords:
            if not kw.arg:
                continue
            meta['constraints'][kw.arg] = (
                kw.value.value if isinstance(kw.value, ast.Constant) else ast.unparse(kw.value))
    return meta


# ---------------------------------------------------------------------------
# 模块解析
# ---------------------------------------------------------------------------
@dataclass
class FuncInfo:
    file: str
    qual: str
    name: str
    lineno: int
    end_lineno: int
    args: str
    body_dump: str
    calls: set
    tables: set
    sql_by_table: dict = field(default_factory=dict)
    params: list = field(default_factory=list)
    hints: list = field(default_factory=list)
    is_route: bool = False
    methods: tuple = ()
    path: str = ''
    full_path: str = ''
    router_var: str = ''
    body_model: str = ''  # PHP/Java：关联的验证器/DTO 类名，供回归建议取字段约束


@dataclass
class ClassInfo:
    file: str
    name: str
    fields: dict
    bases: list = field(default_factory=list)
    field_meta: dict = field(default_factory=dict)


@dataclass
class ModuleInfo:
    path: str
    routers: dict = field(default_factory=dict)
    routes: list = field(default_factory=list)
    funcs: dict = field(default_factory=dict)
    classes: dict = field(default_factory=dict)
    tree_dump: str = ''


def parse_module(src, path, mounts, known_tables):
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return None
    info = ModuleInfo(path=path)
    info.tree_dump = node_dump(tree)
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call) \
                and isinstance(node.value.func, ast.Name) and node.value.func.id == 'APIRouter':
            prefix = ''
            for kw in node.value.keywords:
                if kw.arg == 'prefix' and const_str(kw.value) is not None:
                    prefix = const_str(kw.value)
            for t in node.targets:
                if isinstance(t, ast.Name):
                    info.routers[t.id] = prefix

    module_stem = path.rsplit('/', 1)[-1].replace('.py', '')
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            body_dump = node_dump(node)
            text = ast.unparse(node)
            fi = FuncInfo(
                file=path, qual=node.name, name=node.name,
                lineno=node.lineno, end_lineno=getattr(node, 'end_lineno', node.lineno),
                args=ast.unparse(node.args), body_dump=body_dump,
                calls=call_names(node), tables=extract_tables(text, known_tables),
                sql_by_table=sql_by_table(node, known_tables), params=parse_params(node),
            )
            for dec in node.decorator_list:
                if not isinstance(dec, ast.Call) or not isinstance(dec.func, ast.Attribute):
                    continue
                attr = dec.func.attr
                var = dec.func.value.id if isinstance(dec.func.value, ast.Name) else ''
                path_val = const_str(dec.args[0]) if dec.args else None
                methods = []
                if attr in HTTP_METHODS:
                    methods = [attr.upper()]
                elif attr == 'api_route':
                    for kw in dec.keywords:
                        if kw.arg == 'methods' and isinstance(kw.value, (ast.List, ast.Tuple)):
                            methods = [const_str(x).upper() for x in kw.value.elts if const_str(x)]
                if methods and path_val is not None:
                    prefix = mounts.get((module_stem, var), info.routers.get(var, ''))
                    full = re.sub(r'/+', '/', prefix + info.routers.get(var, '') + path_val) or '/'
                    fi.is_route = True
                    fi.methods = tuple(sorted(set(methods)))
                    fi.path = path_val
                    fi.full_path = full
                    fi.router_var = var
            if fi.is_route:
                info.routes.append(fi)
            else:
                info.funcs[fi.qual] = fi
        elif isinstance(node, ast.ClassDef):
            fields, field_meta = {}, {}
            for stmt in node.body:
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    fields[stmt.target.id] = (
                        ast.unparse(stmt.annotation),
                        ast.unparse(stmt.value) if stmt.value is not None else '')
                    field_meta[stmt.target.id] = parse_field_meta(stmt.annotation, stmt.value)
            info.classes[node.name] = ClassInfo(
                file=path, name=node.name, fields=fields,
                bases=[ast.unparse(b) for b in node.bases], field_meta=field_meta)
    return info


def load_mounts(src):
    mounts = {}
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return mounts
    aliases = {}
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == 'routers':
            for a in node.names:
                aliases[a.asname or a.name] = a.name
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute) \
                or node.func.attr != 'include_router' or not node.args:
            continue
        arg = node.args[0]
        if not isinstance(arg, ast.Attribute) or not isinstance(arg.value, ast.Name):
            continue
        mod = aliases.get(arg.value.id, arg.value.id)
        prefix = ''
        for kw in node.keywords:
            if kw.arg == 'prefix' and const_str(kw.value):
                prefix = const_str(kw.value)
        mounts[(mod, arg.attr)] = prefix
    return mounts


class ProjectIndex:
    def __init__(self, repo, ref, known_tables, project_type='AUTO'):
        self.repo, self.ref, self.known = repo, ref, known_tables
        self.funcs, self.routes, self.classes = {}, [], {}
        self.file_text, self.func_lines = {}, {}
        self.reverse = defaultdict(list)
        self.ctx = languages.build_context(repo, ref, project_type)
        self.lang = self.ctx['lang']
        self._build()
        self.route_map = {}
        for r in self.routes:
            for m in r.methods:
                k = (r.full_path, m)
                old = self.route_map.get(k)
                # PHP 里同一路由可能同时出现在 route/*.php 与 controller 中，
                # 优先保留信息更全的那个（控制器带参数 / 验证器）
                score = len(r.params) + bool(getattr(r, 'body_model', ''))
                if not old or score > len(old.params) + bool(getattr(old, 'body_model', '')):
                    self.route_map[k] = r

    def _build(self):
        # 注意：缓存仓是 --no-checkout，索引为空，不能用 git ls-files；
        # 必须从提交树列文件，并由 languages 批量读 blob（大仓库避免上千次 git show）。
        for path, src in (self.ctx.get('sources') or {}).items():
            if not src:
                continue
            self.file_text[path] = src
            mi = languages.parse_module(src, path, self.ctx, self.known, self.lang)
            if not mi:
                continue
            lines = src.splitlines()
            for fi in list(mi.funcs.values()) + mi.routes:
                key = '%s::%s' % (path, fi.qual)
                self.funcs[key] = fi
                for name in fi.calls:
                    self.reverse[name].append(fi)
                self.func_lines[key] = '\n'.join(lines[fi.lineno - 1:fi.end_lineno])
            self.routes.extend(mi.routes)
            for c in mi.classes.values():
                self.classes[c.name] = c

    def callers(self, name, depth=3):
        seen, out = set(), []
        dq = deque([(name, 0)])
        while dq:
            cur, d = dq.popleft()
            if d >= depth:
                continue
            for fi in self.reverse.get(cur, []):
                key = '%s::%s' % (fi.file, fi.qual)
                if key in seen:
                    continue
                seen.add(key)
                out.append(fi)
                dq.append((fi.name, d + 1))
        return out


# ---------------------------------------------------------------------------
# 变更解析
# ---------------------------------------------------------------------------
def changed_files(repo, base, head):
    raw = git(repo, 'diff', '--name-status', '-w', base, head)
    out = []
    for line in raw.splitlines():
        parts = line.split('\t')
        if len(parts) < 2:
            continue
        status = parts[0][:1]
        if status == 'R' and len(parts) >= 3:
            out.append(('D', parts[1]))
            out.append(('A', parts[2]))
        else:
            out.append((status, parts[-1]))
    return out


def diff_added_lines(repo, base, head, path):
    diff = git(repo, 'diff', '-U0', base, head, '--', path, timeout=30)
    out, new_line = [], 0
    for line in diff.splitlines():
        m = re.match(r'^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@', line)
        if m:
            new_line = int(m.group(1))
            continue
        if line.startswith('+') and not line.startswith('+++'):
            out.append((new_line, line[1:]))
            new_line += 1
        elif line.startswith(' '):
            new_line += 1
    return out


def analyze_file_changes(repo, base, head, path, status, ctx, known, lang=None):
    lang = lang or (ctx or {}).get('lang') or 'PYTHON'
    res = {
        'path': path, 'status': status,
        'routes_added': [], 'routes_removed': [], 'routes_changed': [],
        'funcs_added': [], 'funcs_removed': [], 'funcs_changed': [],
        'classes_changed': [], 'tables_changed': {}, 'noise': False,
    }
    old_src = git_show(repo, base, path) if status != 'A' else None
    new_src = git_show(repo, head, path) if status != 'D' else None
    old_mi = languages.parse_module(old_src, path, ctx, known, lang) if old_src is not None else None
    new_mi = languages.parse_module(new_src, path, ctx, known, lang) if new_src is not None else None
    if old_mi and new_mi and old_mi.tree_dump == new_mi.tree_dump:
        res['noise'] = True
        return res

    def key(r):
        return (r.methods, r.full_path)

    old_routes = {key(r): r for r in (old_mi.routes if old_mi else [])}
    new_routes = {key(r): r for r in (new_mi.routes if new_mi else [])}
    for k, r in new_routes.items():
        if k not in old_routes:
            res['routes_added'].append(r)
        elif r.body_dump != old_routes[k].body_dump or r.args != old_routes[k].args:
            res['routes_changed'].append((r, old_routes[k]))
    for k, r in old_routes.items():
        if k not in new_routes:
            res['routes_removed'].append(r)

    old_funcs = old_mi.funcs if old_mi else {}
    new_funcs = new_mi.funcs if new_mi else {}
    for q, f in new_funcs.items():
        if q not in old_funcs:
            res['funcs_added'].append((q, f))
        elif f.body_dump != old_funcs[q].body_dump or f.args != old_funcs[q].args:
            res['funcs_changed'].append((f, old_funcs[q]))
    for q, f in old_funcs.items():
        if q not in new_funcs:
            res['funcs_removed'].append((q, f))

    added = diff_added_lines(repo, base, head, path)
    for r, _ in res['routes_changed']:
        r.hints = [t.strip() for ln, t in added if r.lineno <= ln <= r.end_lineno and t.strip()][:10]
    for f, _ in res['funcs_changed']:
        f.hints = [t.strip() for ln, t in added if f.lineno <= ln <= f.end_lineno and t.strip()][:10]

    for new, old in list(res['funcs_changed']) + list(res['routes_changed']):
        for t in set(old.sql_by_table) | set(new.sql_by_table):
            old_s, new_s = set(old.sql_by_table.get(t, [])), set(new.sql_by_table.get(t, []))
            if old_s == new_s:
                continue
            changed = ' '.join((new_s - old_s) | (old_s - new_s))
            ops = ops_of_table(changed, t)
            # 新增的只读查询（如新查一张权限表）不算「改变了这张表的既有用法」，不计入影响面
            if not old_s and ops and ops <= {'select'}:
                continue
            res['tables_changed'].setdefault(t, set()).update(ops)

    old_cls, new_cls = (old_mi.classes if old_mi else {}), (new_mi.classes if new_mi else {})
    for name, c in new_cls.items():
        o = old_cls.get(name)
        if o and o.fields != c.fields:
            added_f = [k for k in c.fields if k not in o.fields]
            removed_f = [k for k in o.fields if k not in c.fields]
            changed_f = [k for k in c.fields if k in o.fields and c.fields[k] != o.fields[k]]
            res['classes_changed'].append({
                'name': name, 'added': added_f, 'removed': removed_f, 'changed': changed_f,
                'added_required': [k for k in added_f if not c.fields[k][1]],
            })
    return res


# ---------------------------------------------------------------------------
# 影响汇总
# ---------------------------------------------------------------------------
RELATION_ORDER = ['direct', 'new', 'removed', 'caller', 'table', 'model']


def merge_impacts(items):
    api, others = {}, []
    for i in items:
        if i['kind'] != 'api':
            others.append(i)
            continue
        k = (i['target_key'], i['method'])
        if k not in api:
            api[k] = dict(i, relations=[i['relation']])
        else:
            cur = api[k]
            if i['level'] == 'must':
                cur['level'] = 'must'
            cur['relations'].append(i['relation'])
            for r in i['reasons']:
                if r not in cur['reasons']:
                    cur['reasons'].append(r)
            if not cur.get('file') and i.get('file'):
                cur['file'], cur['line'] = i['file'], i['line']
    out = []
    for v in api.values():
        v['relations'] = list(dict.fromkeys(v['relations']))
        v['relation'] = sorted(v['relations'],
                               key=lambda r: RELATION_ORDER.index(r) if r in RELATION_ORDER else 99)[0]
        v['relation_label'] = '+'.join(v['relations'])
        out.append(v)
    return out + others


def build_impacts(index, changes):
    impacts, changed_tables, models = {}, {}, []

    def add(kind, key, relation, level, reason, method='', file='', line=0):
        k = (kind, key, relation, method)
        if k in impacts:
            if level == 'must':
                impacts[k]['level'] = 'must'
            impacts[k]['reasons'].append(reason)
            return
        impacts[k] = {'kind': kind, 'target_key': key, 'relation': relation, 'level': level,
                      'method': method, 'file': file, 'line': line, 'reasons': [reason]}

    for ch in changes:
        for r, o in ch['routes_changed']:
            write = bool(set(r.methods) & WRITE_METHODS)
            why = []
            if write:
                why.append('%s 写接口' % '/'.join(r.methods))
            if r.args != o.args:
                why.append('函数签名变化')
            why.append('路由函数体修改')
            add('api', r.full_path, 'direct', 'must' if (write or r.args != o.args) else 'suggest',
                '；'.join(why), '/'.join(r.methods), r.file, r.lineno)
        for r in ch['routes_added']:
            add('api', r.full_path, 'new', 'must',
                '新增接口（%s），接口库无对应用例' % '/'.join(r.methods),
                '/'.join(r.methods), r.file, r.lineno)
        for r in ch['routes_removed']:
            add('api', r.full_path, 'removed', 'must',
                '删除接口（%s），破坏性变更' % '/'.join(r.methods),
                '/'.join(r.methods), r.file, r.lineno)

        for f, o in ch['funcs_changed']:
            lang = getattr(index, 'lang', 'PYTHON')
            generic = lang != 'PYTHON' and f.name.lower() in GENERIC_FUNC_NAMES
            shared = f.file.startswith('src/ai/') or f.file.startswith('src/comment/')
            callers = index.callers(f.name)
            route_callers = [c for c in callers if c.is_route]
            if len(route_callers) >= 5 and not generic:
                shared = True
            # 非 Python 大仓里通用方法名会命中几百个同名调用方，截断 + 降级
            limit = 3 if generic else MAX_CALLER_ROUTES
            shown, hidden = route_callers[:limit], len(route_callers) - limit
            for c in shown:
                level = 'must' if (shared and not generic) else 'suggest'
                why = '调用链（近似匹配）：%s() 被修改' % f.name
                if shared:
                    why += '（共享模块/高扇出）'
                if generic:
                    why += '（通用方法名，同名函数多，仅供参考）'
                add('api', c.full_path, 'caller', level, why,
                    '/'.join(c.methods), c.file, c.lineno)
            if hidden > 0 and not generic:
                add('symbol', f.name, 'caller_overflow', 'suggest',
                    '另有 %d 个疑似调用路由未展开（按函数名近似匹配）' % hidden,
                    file=f.file, line=f.lineno)
            if shared:
                add('symbol', f.name, 'shared', 'must',
                    '共享函数被修改，间接影响 %d 个路由' % len(route_callers),
                    file=f.file, line=f.lineno)
        for q, f in ch['funcs_removed']:
            add('symbol', f.name, 'removed', 'must', '删除函数，检查调用方', file=f.file, line=f.lineno)

        for c in ch['classes_changed']:
            models.append(c)
            level = 'must' if (c['added_required'] or c['removed'] or c['changed']) else 'suggest'
            for key, fi in index.funcs.items():
                if not fi.is_route:
                    continue
                if re.search(r'\b%s\b' % re.escape(c['name']), index.func_lines.get(key, '')):
                    add('api', fi.full_path, 'model', level,
                        '模型 %s 字段变化（+%s / -%s / ~%s）' % (
                            c['name'], ','.join(c['added']) or '无',
                            ','.join(c['removed']) or '无', ','.join(c['changed']) or '无'),
                        '/'.join(fi.methods), fi.file, fi.lineno)
            add('model', c['name'], 'changed', level,
                '模型字段变化：新增 %s / 删除 %s / 修改 %s' % (
                    c['added'], c['removed'], c['changed']), file=ch['path'])

        for t, ops in ch.get('tables_changed', {}).items():
            changed_tables.setdefault(t, set()).update(ops)

    for table in sorted(changed_tables):
        ops = changed_tables[table]
        add('table', table, 'changed', 'must',
            '表的 SQL 用法变化（%s）' % ('/'.join(sorted(ops)) or '未知'))
        if not (ops & {'update', 'delete'}):
            continue
        for r in index.routes:
            if table not in r.tables:
                continue
            write = any(m.group(1).lower() == table for m in WRITE_TABLE_RE.finditer(
                index.func_lines.get('%s::%s' % (r.file, r.qual), '')))
            add('api', r.full_path, 'table', 'must' if write else 'suggest',
                '同表%s影响：%s（变更操作：%s）' % (
                    '写' if write else '读', table, '/'.join(sorted(ops))),
                '/'.join(r.methods), r.file, r.lineno)

    return merge_impacts(list(impacts.values())), changed_tables, models


# ---------------------------------------------------------------------------
# 覆盖匹配 + 建议
# ---------------------------------------------------------------------------
def norm_path(p):
    p = re.sub(r'\{[^}]+\}', '*', p or '')
    p = re.sub(r':[A-Za-z_][A-Za-z0-9_]*', '*', p)
    p = re.sub(r'/+$', '', p.strip())
    if p.lower().startswith('/api/'):
        p = p[4:]
    return p.lower()


def match_interfaces(full_path, method, interfaces):
    target = norm_path(full_path)
    exact = [i for i in interfaces
             if norm_path(i.get('url')) == target and (i.get('method') or 'GET').upper() == method]
    if exact:
        return exact
    tail = '/'.join(target.split('/')[-2:])
    return [i for i in interfaces
            if '/'.join(norm_path(i.get('url')).split('/')[-2:]) == tail]


def module_of(path):
    seg = norm_path(path).strip('/').split('/')[0]
    return MODULE_KEYWORDS.get(seg, seg or '未分类')


def base_type_name(t):
    t = (t or '').strip()
    while True:
        m = re.match(r'^(?:Optional|List|list|Dict|dict|Sequence|Set|set)\[(.+)\]$', t)
        if not m:
            return t
        t = m.group(1).strip()


def is_model(index, name, seen=None):
    seen = seen or set()
    if not name or name in seen:
        return False
    seen.add(name)
    cls = index.classes.get(name)
    if not cls:
        return False
    if any('BaseModel' in b for b in cls.bases):
        return True
    return any(is_model(index, base_type_name(b), seen) for b in cls.bases)


def sample_value(type_str, meta=None):
    if meta and meta.get('enum'):
        return '"%s"' % meta['enum'][0]
    t = base_type_name(type_str).lower()
    if 'bool' in t:
        return 'true'
    if 'int' in t:
        return '1'
    if 'float' in t or 'decimal' in t:
        return '1.0'
    if 'list' in t:
        return '[]'
    if 'dict' in t:
        return '{}'
    if 'datetime' in t or 'date' in t:
        return '"2026-01-01 00:00:00"'
    return '"测试"'


def hint_message(hints):
    for h in hints:
        for m in re.findall(r"['\"]([^'\"]*[\u4e00-\u9fff][^'\"]*)['\"]", h):
            if len(m) <= 30:
                return m
    return ''


def build_suggestions(api, index, models):
    route = index.route_map.get((api['target_key'], api['method']))
    if not route:
        return ['未能定位路由函数：按 正常 / 参数缺失 / 类型错误 / 无 token 回归']
    body, body_param = None, ''
    for p in route.params:
        bt = base_type_name(p['type'])
        if bt in index.classes and is_model(index, bt):
            body, body_param = bt, p['name']
            break
    # PHP/Java：请求体模型由解析器按验证器 / DTO 标注，不走函数参数类型推断
    if not body:
        candidate = getattr(route, 'body_model', '') or ''
        if candidate in index.classes:
            body = candidate
    meta = index.classes[body].field_meta if body else {}
    required = [k for k, v in meta.items() if v['required']]
    optional = [k for k, v in meta.items() if not v['required']]
    path_params = re.findall(r'\{(\w+)\}', api['target_key'])
    query = [p for p in route.params
             if p['name'] != body_param and base_type_name(p['type']) not in index.classes
             and base_type_name(p['type']) not in ('Request', '')
             and p['name'] not in path_params]

    out = []
    if body:
        out.append('请求体 `%s`：必填 %d（%s）/ 可选 %d%s' % (
            body, len(required), '、'.join(required[:8]) or '无', len(optional),
            '（%s）' % '、'.join(optional[:5]) if optional else ''))
    if path_params:
        out.append('路径参数：%s' % '、'.join('`%s`' % p for p in path_params))
    if query:
        out.append('Query 参数：%s' % '、'.join(
            '`%s=%s`' % (p['name'], p['default'] or '必填') for p in query[:8]))
    hints = route.hints or []
    if hints:
        out.append('本次改动：%s' % '；'.join(
            '`%s`' % ((h[:60] + '…') if len(h) > 60 else h) for h in hints[:2]))

    cases = []
    if body:
        sample = ', '.join('%s: %s' % (k, sample_value(meta[k]['type'], meta[k]))
                           for k in required[:5])
        cases.append('正常路径：`{%s}%s` → 200 且 code ∈ {0,200}' % (
            sample, '，…' if len(required) > 5 else ''))
    else:
        cases.append('正常路径：合法参数 → 200 且 code ∈ {0,200}')
    for f in required[:3]:
        cases.append('缺 `%s` → 422（校验失败），提示需指向该字段' % f)
    int_field = next((k for k, v in meta.items() if 'int' in v['type'].lower()), None)
    if int_field:
        cases.append('`%s` 传字符串 `"abc"` → 422（类型转换失败）' % int_field)
    if 'id' in meta or 'id' in path_params:
        cases.append('`id=999999999`（不存在）→ 明确业务错误/空结果，不能 500')
    for f in [k for k in meta if k.endswith('_id') and k != 'id'][:2]:
        cases.append('`%s=999999999`（外键不存在）→ 业务错误或空结果，不能 500' % f)
    for f, m in meta.items():
        if 'list' in m['type'].lower():
            cases.append('`%s` 取 `[]` / 重复 id / 不存在的 id → 幂等或明确报错' % f)
            break
    for f, m in meta.items():
        if 'bool' in m['type'].lower():
            cases.append('`%s` 取 true / false → 两分支都覆盖（false 不调外部模型）' % f)
    empty_target = next((f for f in required
                         if 'str' in meta[f]['type'].lower() and any(f in h for h in hints)), None)
    if empty_target:
        msg = hint_message(hints)
        cases.append('`%s` 传 `""` 或全空格 → 400%s（本次新增校验）' % (
            empty_target, '「%s」' % msg if msg else ''))
    blob = ' '.join(hints)
    if 'create_by' in blob:
        cases.append('数据权限：admin 角色应返回全部数据（含他人创建）')
        cases.append('数据权限：普通角色只返回 create_by=当前用户 的数据')
        cases.append('越权校验：A 用户 token 查不到 B 用户创建的数据')
        if 'accessToken' in blob or 'if token' in blob:
            cases.append('无 token / token 解析失败：确认是否应返回全部'
                         '（当前实现不追加过滤条件，需与设计确认）')
    for f, m in meta.items():
        c = m['constraints']
        if 'max_length' in c:
            cases.append('`%s` 长度取 %s+1 → 422 或明确截断策略' % (f, c['max_length']))
        if 'ge' in c or 'le' in c:
            cases.append('`%s` 取边界值与越界值（ge=%s / le=%s）' % (
                f, c.get('ge', '-'), c.get('le', '-')))
        if m['enum']:
            cases.append('`%s` 枚举：%s 逐个通过，非法值 → 422' % (
                f, '/'.join('`%s`' % v for v in m['enum'][:4])))
    for q in query:
        name = q['name']
        if name.lower() in ('pageno', 'pagesize'):
            cases.append('`%s` 取 0 / 1 / 100000 → 分页边界不能 500' % name)
        elif 'int' in q['type'].lower():
            cases.append('`%s` 传 0 / 负数 / 字符串 → 类型与边界校验' % name)
        elif 'str' in q['type'].lower():
            cases.append('`%s` 传空串 / 不存在值 / 超长 → 空列表而非 500' % name)
    changed = next((m for m in models if m['name'] == body), None)
    if changed:
        if changed['added']:
            cases.append('模型新增字段 `%s`：传值 / 不传，验证默认值与兼容性' % '、'.join(changed['added']))
        if changed['removed']:
            cases.append('模型删除字段 `%s`：旧调用方继续传 → 忽略或明确报错' % '、'.join(changed['removed']))
        if changed['changed']:
            cases.append('模型字段 `%s` 类型/默认值变化：用旧类型入参验证' % '、'.join(changed['changed']))
    if 'new' in api.get('relations', []):
        cases.append('新增接口：先补最小合法入参的冒烟用例')
    if 'removed' in api.get('relations', []):
        cases.append('删除接口：确认调用方已下线，旧用例从接口库清理')
    seen, uniq = set(), []
    for c in cases:
        if c not in seen:
            seen.add(c)
            uniq.append(c)
    out.append('建议用例：')
    for i, c in enumerate(uniq[:8], 1):
        out.append('%d. %s' % (i, c))
    if len(uniq) > 8:
        out.append('… 其余 %d 条同类边界' % (len(uniq) - 8))
    out.append('鉴权：无 token / 错误 token → 401/403')
    if api['method'] in WRITE_METHODS:
        out.append('执行后清理：删除或复原本次写入的数据')
    out.append('断言：HTTP 200 且 body.code ∈ {0,200}；写接口另断读回一致')
    return out


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def analyze_range(repo_path, base, head, known_tables, assets=None, project_type='AUTO'):
    assets = assets or {}
    commits = []
    for line in git(repo_path, 'log', '--format=%H%x1f%an%x1f%s', '%s..%s' % (base, head)).splitlines():
        parts = line.split('\x1f')
        if len(parts) == 3:
            commits.append({'sha': parts[0][:8], 'author': parts[1], 'subject': parts[2]})

    stats = {}
    for line in git(repo_path, 'diff', '--numstat', '-w', base, head).splitlines():
        parts = line.split('\t')
        if len(parts) >= 3:
            stats[parts[2]] = (int(parts[0]) if parts[0].isdigit() else 0,
                               int(parts[1]) if parts[1].isdigit() else 0)

    index = ProjectIndex(repo_path, head, known_tables, project_type)
    lang, ctx = index.lang, index.ctx
    files, changes = [], []
    for status, path in changed_files(repo_path, base, head):
        if not languages.is_source(path, lang):
            files.append({'path': path, 'status': status, 'additions': 0, 'deletions': 0,
                          'patch': '', 'is_noise': 0})
            continue
        ch = analyze_file_changes(repo_path, base, head, path, status, ctx,
                                  known_tables, lang)
        changes.append(ch)
        patch = git(repo_path, 'diff', '-U3', base, head, '--', path, timeout=30)
        if len(patch) > 150000:
            patch = patch[:150000] + '\n...[patch 已截断]'
        add, dele = stats.get(path, (0, 0))
        files.append({'path': path, 'status': status, 'additions': add, 'deletions': dele,
                      'patch': patch, 'is_noise': 1 if ch['noise'] else 0})

    for ch in changes:
        for r, _ in ch['routes_changed']:
            for ir in index.routes:
                if ir.file == r.file and ir.full_path == r.full_path:
                    ir.hints = r.hints

    impacts, tables, models = build_impacts(index, changes)

    interfaces = assets.get('interfaces') or []
    exec_map = assets.get('exec') or {}
    for i in impacts:
        if i['kind'] != 'api':
            continue
        hit = match_interfaces(i['target_key'], i['method'], interfaces)
        cases = []
        for h in hit:
            ex = exec_map.get(h['id']) or {}
            n, s = ex.get('n') or 0, ex.get('s') or 0
            cases.append({'id': h['id'], 'name': h.get('name') or ('#%s' % h['id']),
                          'group': h.get('group'),
                          'pass_rate': round(s * 100.0 / n, 1) if n else None})
        i['cases'] = cases
        i['module'] = module_of(i['target_key'])
        i['suggestions'] = build_suggestions(i, index, models)

    must = [i for i in impacts if i['kind'] == 'api' and i['level'] == 'must']
    suggest = [i for i in impacts if i['kind'] == 'api' and i['level'] == 'suggest']
    new = [i for i in must if 'new' in i.get('relations', [])]
    risk = '高' if len(must) >= 5 else ('中' if must else '低')
    return {
        'base_sha': base,
        'head_sha': head,
        'commit_count': len(commits),
        'commits': commits,
        'files': files,
        'impacts': impacts,
        'tables': sorted(tables),
        'models': models,
        'noise': [f['path'] for f in files if f['is_noise']],
        'db_available': bool(interfaces),
        'counts': {'must': len(must), 'suggest': len(suggest), 'new': len(new),
                   'insertions': sum(v[0] for v in stats.values()),
                   'deletions': sum(v[1] for v in stats.values())},
        'risk_level': risk,
        'project_type': lang,
        'project_type_label': languages.label(lang),
        'detect_counts': ctx.get('detect_counts') or {},
        'lang_notes': languages.lang_notes(lang),
    }
