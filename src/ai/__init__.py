"""AI 工作台 · 能力底座

对外入口集中在 runner.py（执行编排），其余模块按职责拆分：

    settings.py  全局配置（配额上限、脱敏开关）读取，带进程内缓存
    errors.py    统一异常类型
    crypto.py    api_key 的 Fernet 加解密
    mask.py      外发文本脱敏（手机号/身份证/邮箱/银行卡/密钥/内网IP）
    prompts.py   提示词模板装载（ai_prompts 表优先，内置默认兜底）
    models.py    模型注册表：可见性过滤、默认模型优先级链、参数适配、连通性探测
    client.py    统一 LLM 调用（OpenAI 兼容），超时/代理/用量
    schema.py    结构化输出模型 + 逐条校验与坏条目剔除
    docs.py      需求文档解析与分块（md/txt/docx）
    runner.py    执行编排：配额校验 → 渲染 → 脱敏 → 调用 → 校验 → 重试 → 写日志
"""

__version__ = '1.0.0'
