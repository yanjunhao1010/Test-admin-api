"""AI 底座统一异常类型。

路由层据此映射 HTTP 状态码，避免把「没配模型」这种配置问题也报成 500。
"""


class AIError(Exception):
    """AI 底座基础异常"""


class AIConfigError(AIError):
    """配置类问题：没有可用模型、密钥缺失或解密失败、base_url 非法等。映射 400。"""


class AIQuotaError(AIError):
    """超出配额：单次 token 上限或当日调用次数上限。映射 429。"""


class AIUpstreamError(AIError):
    """上游模型调用失败：超时、401、429、5xx、网络不通。映射 502。"""


class AISchemaError(AIError):
    """结构化输出校验失败（已按配置重试仍失败）。映射 422。"""
