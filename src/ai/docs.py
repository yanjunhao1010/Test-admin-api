"""需求文档解析与分块。

支持：.md / .markdown / .txt（直读文本）、.docx（python-docx 抽段落 + 表格）、
图片（png/jpg/jpeg/webp/gif/bmp，转 data URL 供多模态模型分析）。

分块是必须的：长文档不能整篇塞给模型。
按标题切块并保留「标题路径」作为上下文前缀，让分块后的功能点仍有归属感。
图片各自作为一个独立块，分析时作为多模态输入直接送模型。
"""
import base64
import io
import os
import re
from typing import Any, Dict, List

MAX_BLOCK_CHARS = 6000

SUPPORTED_EXT = ('.md', '.markdown', '.txt', '.docx')
TEXT_EXT = ('.md', '.markdown', '.txt')
IMAGE_EXT = ('.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp')

# 图片 magic number 校验（不依赖第三方库）：(扩展名, 头部字节)
_IMAGE_MAGIC = {
    '.png': b'\x89PNG\r\n\x1a\n',
    '.jpg': b'\xff\xd8\xff',
    '.jpeg': b'\xff\xd8\xff',
    '.webp': b'RIFF',
    '.gif': b'GIF8',
    '.bmp': b'BM',
}
_IMAGE_MIME = {
    '.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
    '.webp': 'image/webp', '.gif': 'image/gif', '.bmp': 'image/bmp',
}

HEADING_RE = re.compile(r'^(#{1,6})\s+(.*)$')
DOCX_HEADING_RE = re.compile(r'(?:Heading|标题)\s*(\d)')


def _decode(data: bytes) -> str:
    for enc in ('utf-8-sig', 'utf-8', 'gb18030', 'gbk', 'latin-1'):
        try:
            return data.decode(enc)
        except Exception:
            continue
    return data.decode('utf-8', errors='ignore')


def _extract_docx(data: bytes) -> str:
    try:
        from docx import Document
    except ImportError:
        raise ValueError('服务端未安装 python-docx，暂不支持 .docx；请改用 .md / .txt，或让管理员执行 pip install python-docx')

    doc = Document(io.BytesIO(data))
    lines: List[str] = []

    for p in doc.paragraphs:
        text = (p.text or '').strip()
        if not text:
            continue
        style = ''
        try:
            style = p.style.name or ''
        except Exception:
            style = ''
        m = DOCX_HEADING_RE.search(style)
        if m:
            level = max(1, min(6, int(m.group(1))))
            lines.append('#' * level + ' ' + text)
        elif style.lower().startswith('title'):
            lines.append('# ' + text)
        else:
            lines.append(text)

    # 用例表格常写在 Word 表里，必须抽；转成 markdown 表格便于模型理解
    for idx, table in enumerate(doc.tables, 1):
        rows = []
        for row in table.rows:
            cells = []
            for cell in row.cells:
                cells.append((cell.text or '').strip().replace('\n', ' '))
            if any(cells):
                rows.append('| ' + ' | '.join(cells) + ' |')
        if rows:
            lines.append('')
            lines.append('### 表格 %d' % idx)
            lines.extend(rows)

    return '\n'.join(lines)


def extract_text(filename: str, data: bytes) -> str:
    """按扩展名解析出纯文本；不支持的类型直接报错（错误信息给用户看）"""
    ext = os.path.splitext(filename or '')[1].lower()
    if ext not in SUPPORTED_EXT:
        raise ValueError('不支持的文件类型 %s，仅支持：md / txt / docx' % (ext or '未知'))
    if ext in TEXT_EXT:
        return _decode(data)
    return _extract_docx(data)


def is_image_ext(filename: str) -> bool:
    ext = os.path.splitext(filename or '')[1].lower()
    return ext in IMAGE_EXT


def prep_image(filename: str, data: bytes) -> Dict[str, Any]:
    """校验图片 magic 并转成 data URL（供多模态模型直接消费）。

    返回 {filename, mime, data_url}；非法图片抛 ValueError（错误信息给用户看）。
    """
    ext = os.path.splitext(filename or '')[1].lower()
    if ext not in IMAGE_EXT:
        raise ValueError('不支持的图片类型 %s，仅支持：png / jpg / jpeg / webp / gif / bmp' % (ext or '未知'))
    magic = _IMAGE_MAGIC.get(ext)
    if magic and not data.startswith(magic):
        raise ValueError('文件 %s 不是合法的 %s 图片（文件头校验失败）' % (filename, ext))
    mime = _IMAGE_MIME.get(ext, 'application/octet-stream')
    b64 = base64.b64encode(data).decode('ascii')
    return {'filename': filename, 'mime': mime, 'data_url': 'data:%s;base64,%s' % (mime, b64)}


def split_blocks(text: str, max_chars: int = MAX_BLOCK_CHARS) -> List[Dict[str, Any]]:
    """按标题切块，返回 [{'section_path': '登录 > 忘记密码', 'text': '...'}]"""
    if not text or not text.strip():
        return []

    lines = text.split('\n')
    stack: List[Any] = []          # [(level, title)]
    blocks: List[Dict[str, Any]] = []
    buf: List[str] = []
    buf_len = 0

    def flush():
        nonlocal buf, buf_len
        body = '\n'.join(buf).strip()
        if body:
            blocks.append({
                'section_path': ' > '.join(t for _, t in stack),
                'text': body,
            })
        buf = []
        buf_len = 0

    for line in lines:
        m = HEADING_RE.match(line.strip())
        if m:
            flush()
            level = len(m.group(1))
            title = m.group(2).strip()
            stack = [(lv, tt) for lv, tt in stack if lv < level]
            stack.append((level, title))
            buf.append(line)
            buf_len += len(line)
            continue
        buf.append(line)
        buf_len += len(line)
        if buf_len >= max_chars:
            flush()
    flush()

    # 单块仍超长（超长段落无标题）时二次硬切，避免超出模型上下文
    out: List[Dict[str, Any]] = []
    for b in blocks:
        body = b['text']
        if len(body) <= int(max_chars * 1.5):
            out.append(b)
            continue
        for i in range(0, len(body), max_chars):
            out.append({'section_path': b['section_path'], 'text': body[i:i + max_chars]})
    return out
