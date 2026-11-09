"""统一 LLM 调用（OpenAI 兼容协议）。

按 model_type 做参数适配 —— 这是最容易翻车的地方：
    deepseek-reasoner 不支持 response_format=json_object / function calling，temperature 也不生效，
    所以 reasoner 类型只发 model/messages/max_tokens，结构化输出改由「prompt 强约束 + 服务端校验重试」兜。
"""
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from .errors import AIUpstreamError

logger = logging.getLogger(__name__)

# 网络瞬断重试：只针对「连接建立失败」这类与请求内容无关的错误。
# 此前这类错误一次就冒泡，把整条备用链在几秒内耗光（实测三模型 4 秒内全灭）。
NETWORK_RETRY = 2
RETRY_BACKOFF_BASE = 1.0   # 秒
RETRY_BACKOFF_CAP = 5.0


def _is_local_url(base_url: str) -> bool:
    """本地模型（Ollama / vLLM）常无鉴权，识别为本地地址即允许空 key"""
    if not base_url:
        return False
    return any(seg in base_url for seg in ('localhost', '127.0.0.1', '0.0.0.0', '::1'))


def _build_client(api_key: str, base_url: str, timeout: int):
    """构造 OpenAI 客户端；有代理环境变量时走代理（兼容企业网）"""
    try:
        import httpx
        from openai import OpenAI
    except Exception as e:  # noqa: BLE001
        raise AIUpstreamError('缺少 openai / httpx 依赖：%s' % e)

    kwargs: Dict[str, Any] = {
        'api_key': api_key,
        'base_url': base_url,
        'timeout': timeout,
        # 关掉 SDK 自带重试：容错由我们的「备用模型链」统一处理，避免一次请求被悄悄放大 3 倍耗时
        'max_retries': 0,
    }
    proxy = (os.getenv('HTTPS_PROXY') or os.getenv('HTTP_PROXY')
             or os.getenv('https_proxy') or os.getenv('http_proxy'))
    if proxy:
        # verify=False 兼容本地抓包/企业代理的 TLS 中间人证书。
        # limits 要放开：分批生成会连续建连，默认 100 连接 / 20 keepalive 在
        # 高频新建-关闭场景下容易触发 httpx 的池等待，进而表现为 Connection error。
        kwargs['http_client'] = httpx.Client(
            proxy=proxy, verify=False, timeout=timeout,
            limits=httpx.Limits(max_connections=50, max_keepalive_connections=20,
                                keepalive_expiry=30.0))
    return OpenAI(**kwargs)


def close_client(client: Any) -> None:
    """关闭 OpenAI 客户端及其 httpx 连接池。

    必须调用：httpx 的连接池不 close 会一直占着 TCP/TLS 连接，
    而本文件每次 chat_completion 都会新建一个客户端 —— 分批场景下
    （一次请求建 N 个）不关就是 N 份连接池泄漏，最终表现为随机的 Connection error。
    """
    if client is None:
        return
    try:
        client.close()
    except Exception as e:  # noqa: BLE001  关闭失败不该影响业务结果
        logger.debug('关闭 LLM 客户端失败（忽略）：%s', e)


def _is_transient_network_error(exc: Exception) -> bool:
    """判断是否「网络瞬断」——这类错误重试一次就能过，不该立刻烧掉整条备用链。

    覆盖 httpx 的连接类错误：代理重启、连接被对端 RST、TLS 握手中断、
    连接池等待超时等。区分依据是异常类名 + 是否带 http 状态码：
    带明确 4xx/5xx 的一律不算瞬断（那是真请求有问题，重试无意义）。
    """
    if getattr(exc, 'status_code', None):
        return False
    name = type(exc).__name__
    # openai SDK 会把 httpx 异常包一层，类名可能带 APIConnectionError / APITimeoutError
    if any(k in name for k in ('Connection', 'Connect', 'Timeout', 'TimeoutError',
                               'ReadError', 'WriteError', 'RemoteProtocol', 'Proxy')):
        return True
    msg = str(exc).lower()
    return any(k in msg for k in ('connection error', 'connection reset', 'connection aborted',
                                  'server disconnected', 'timed out', 'timeout',
                                  'eof occurred', 'broken pipe', 'proxy'))


def _filter_secret(text: str, api_key: Optional[str]) -> str:
    """上游报错常回显 key，落日志/返回前端前统一打掉"""
    if not text:
        return ''
    out = str(text)
    if api_key and len(api_key) > 6 and api_key in out:
        out = out.replace(api_key, '****')
    return out[:800]


def chat_completion(
    runtime: Dict[str, Any],
    messages: List[Dict[str, Any]],
    json_mode: bool = False,
    max_tokens: Optional[int] = None,
    temperature: Optional[float] = None,
) -> Tuple[str, Dict[str, Any], Optional[int], int]:
    """调用一次模型。

    返回 (content, usage, http_status, elapsed_ms, finish_reason)；失败抛 AIUpstreamError。

    内置「网络瞬断重试」：连接建立失败（Connection error / 连接被 RST / 代理重启）
    这类错误与请求内容无关，通常重试一次即可通过。此前这类错误直接冒泡，
    会把整条备用模型链在几秒内耗光（实测：三个模型 4 秒内全部 Connection error），
    分批生成时一批报废就等于整单少一批内容。
    """
    api_key = (runtime.get('api_key') or '').strip()
    base_url = (runtime.get('base_url') or '').strip().rstrip('/')
    model_name = (runtime.get('model_name') or '').strip()
    model_type = (runtime.get('model_type') or 'chat').lower()
    # 默认 300s 而不是 120s：reasoner 类模型的输出是「先思考再回答」，单次可达60~170s，
    # 120s 会把正常请求误杀成超时（表现为上游 502/连接中断）。
    timeout = int(runtime.get('timeout') or 300)

    if not base_url:
        raise AIUpstreamError('该模型未配置接口地址')
    if not model_name:
        raise AIUpstreamError('该模型未配置模型名')
    # 本地模型（Ollama / vLLM）通常不需要密钥，允许留空
    if not api_key and not _is_local_url(base_url):
        raise AIUpstreamError('该模型未配置 API Key（本地模型可留空）')
    key_for_sdk = api_key or 'ollama'

    kwargs: Dict[str, Any] = {
        'model': model_name,
        'messages': messages,
        'stream': False,
    }

    if model_type != 'reasoner':
        # reasoner 不支持 temperature，别的模型正常下发
        temp = temperature if temperature is not None else runtime.get('temperature')
        if temp is not None:
            kwargs['temperature'] = float(temp)
        caps = runtime.get('capabilities') or {}
        if json_mode and caps.get('json_object') is not False:
            # 探测过明确不支持时不发，避免吃一个 400
            kwargs['response_format'] = {'type': 'json_object'}

    if max_tokens:
        kwargs['max_tokens'] = int(max_tokens)

    started = time.time()
    last_exc: Optional[Exception] = None
    for attempt in range(NETWORK_RETRY + 1):
        # 每次尝试都新建客户端并在 finally 关闭：连接池用完即弃，
        # 避免分批场景下累积 N 份未释放的连接池。
        client = _build_client(key_for_sdk, base_url, timeout)
        try:
            resp = client.chat.completions.create(**kwargs)
            break
        except Exception as e:  # noqa: BLE001
            last_exc = e
            transient = _is_transient_network_error(e)
            logger.warning(
                '模型 %s 调用%s（第 %d/%d 次）：%s', model_name,
                '遇到网络瞬断，重试中' if transient else '失败', attempt + 1,
                NETWORK_RETRY + 1, _filter_secret(getattr(e, 'message', None) or str(e), api_key))
            # 非瞬断错误（401/429/5xx 等）重试无意义，直接抛出
            if not transient or attempt >= NETWORK_RETRY:
                status = getattr(e, 'status_code', None)
                detail = _filter_secret(getattr(e, 'message', None) or str(e), api_key)
                raise AIUpstreamError('模型调用失败（%s）：%s' % (status or '网络/超时', detail))
            # 指数退避 + 抖动：避免多个并发批次在同一毫秒重试形成尖峰
            time.sleep(min(RETRY_BACKOFF_CAP, RETRY_BACKOFF_BASE * (2 ** attempt))
                       * (0.7 + 0.6 * ((time.time() % 1))))
        finally:
            close_client(client)
    else:
        # 理论上不可达（break 成功或有最后一次 raise），兜底避免 UnboundLocalError
        status = getattr(last_exc, 'status_code', None) if last_exc else None
        detail = _filter_secret(getattr(last_exc, 'message', None) or str(last_exc or ''), api_key)
        raise AIUpstreamError('模型调用失败（%s）：%s' % (status or '网络/超时', detail))
    elapsed_ms = int((time.time() - started) * 1000)

    try:
        content = resp.choices[0].message.content or ''
    except Exception:  # noqa: BLE001
        content = ''
    # finish_reason='length' 表示输出被 max_tokens 撞顶截断 —— 这是「输出不是合法 JSON」
    # 最常见的真实原因（JSON 尾部被砍掉），必须单独识别，否则只能看到误导性的重试报错。
    finish_reason = None
    try:
        finish_reason = getattr(resp.choices[0], 'finish_reason', None)
    except Exception:  # noqa: BLE001
        pass
    usage = {}
    try:
        if resp.usage:
            usage = {
                'prompt_tokens': getattr(resp.usage, 'prompt_tokens', None),
                'completion_tokens': getattr(resp.usage, 'completion_tokens', None),
                'total_tokens': getattr(resp.usage, 'total_tokens', None),
            }
    except Exception:  # noqa: BLE001
        pass
    http_status = getattr(resp, 'response', None)
    http_status = getattr(http_status, 'status_code', 200) if http_status is not None else 200
    return content, usage, http_status, elapsed_ms, finish_reason
