from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from openai import OpenAI
import logging
from typing import Optional, Dict, Any, List
import json
import re
import os
import httpx

# 配置日志
logger = logging.getLogger(__name__)

router = APIRouter(
    prefix='/ais',
    tags=['ais']
)


# 请求模型定义
class AIRequest(BaseModel):
    prompt: str
    model: str = "deepseek-chat"
    messages: Optional[List[Dict[str, Any]]] = None  # 多轮消息历史，优先于 prompt 使用
    temperature: float = 1
    max_tokens: Optional[int] = None
    stream: bool = False
    response_format: str = "json"  # json | text
    json_schema: Optional[Dict[str, Any]] = None  # 新增：JSON结构定义


# 响应模型定义 - 更严格的类型定义
class AIResponse(BaseModel):
    code: int
    msg: str
    data: Dict[str, Any]


def _build_openai_kwargs(api_key: str, base_url: str) -> dict:
    """构建 OpenAI 客户端参数，自动从环境变量读取代理（兼容企业网/本地抓包代理）"""
    kwargs = {"api_key": api_key, "base_url": base_url}
    proxy = (
        os.getenv("HTTPS_PROXY")
        or os.getenv("HTTP_PROXY")
        or os.getenv("https_proxy")
        or os.getenv("http_proxy")
    )
    if proxy:
        # verify=False 用于兼容本地代理的 TLS 中间人证书，仅建议在本地开发环境使用
        kwargs["http_client"] = httpx.Client(proxy=proxy, verify=False)
    return kwargs


class DeepSeekClient:
    def __init__(self, api_key: str, base_url: str = "https://api.deepseek.com"):
        self.client = OpenAI(**_build_openai_kwargs(api_key, base_url))
        self.available_models = {
            "deepseek-chat": "通用对话模型",
            "deepseek-reasoner": "推理模型"
        }

    def generate_response(self, request: AIRequest) -> Dict[str, Any]:
        """生成AI响应"""
        try:
            # 验证模型
            if request.model not in self.available_models:
                raise ValueError(f"不支持的模型: {request.model}")

            # 处理prompt，确保JSON格式要求
            processed_prompt = self._process_prompt(request)

            # 构建消息：优先使用前端传来的多轮历史，否则用单条 prompt
            if request.messages:
                messages = request.messages
            else:
                messages = [{"role": "user", "content": processed_prompt}]

            # 构建请求参数
            completion_kwargs = {
                "model": request.model,
                "messages": messages,
                "temperature": request.temperature,
                "stream": request.stream,
            }

            # 设置JSON响应格式
            if request.response_format == "json":
                completion_kwargs["response_format"] = {"type": "json_object"}

            # 添加可选的max_tokens参数
            if request.max_tokens:
                completion_kwargs["max_tokens"] = request.max_tokens

            # 调用API
            response = self.client.chat.completions.create(**completion_kwargs)

            # 处理响应
            if request.stream:
                return self._handle_stream_response(response)
            else:
                return self._handle_normal_response(response, request.response_format)

        except Exception as e:
            logger.error(f"DeepSeek API调用失败: {str(e)}")
            raise

    def _process_prompt(self, request: AIRequest) -> str:
        """处理prompt，确保符合API要求"""
        prompt = request.prompt

        if request.response_format == "json":
            # 如果提供了JSON schema，添加到prompt中
            if request.json_schema:
                schema_instruction = f"\n\n请严格按照以下JSON格式返回数据：\n{json.dumps(request.json_schema, ensure_ascii=False, indent=2)}"
                prompt += schema_instruction
            # 确保prompt包含JSON关键字
            elif not self._contains_json_keyword(prompt):
                json_instruction = "\n\n请以JSON格式返回结果，确保返回纯JSON数据，不要包含其他文本。"
                prompt += json_instruction

        return prompt

    def _contains_json_keyword(self, text: str) -> bool:
        """检查文本中是否包含json相关词汇"""
        json_keywords = ['json', 'JSON', 'Json', 'javascript object notation']
        return any(keyword in text.lower() for keyword in json_keywords)

    def _handle_normal_response(self, response, response_format: str) -> Dict[str, Any]:
        """处理非流式响应"""
        ai_response = response.choices[0].message.content
        usage = response.usage.dict() if response.usage else {}

        # 清理响应内容
        cleaned_response = self._clean_response_content(ai_response)

        # 解析JSON响应
        parsed_response = None
        is_valid_json = False

        if response_format == "json":
            try:
                parsed_response = json.loads(cleaned_response)
                is_valid_json = True
            except json.JSONDecodeError:
                logger.warning("响应不是有效的JSON格式，尝试提取JSON内容")
                # 尝试从文本中提取JSON
                extracted_json = self._extract_json_from_text(cleaned_response)
                if extracted_json:
                    parsed_response = extracted_json
                    is_valid_json = True
                else:
                    logger.warning("无法提取有效的JSON内容，返回原始文本")
                    parsed_response = cleaned_response

        return {
            "content": parsed_response if parsed_response else cleaned_response,
            "raw_content": ai_response,
            "usage": usage,
            "model": response.model,
            "finish_reason": response.choices[0].finish_reason,
            "is_json": is_valid_json,
            "response_format": response_format
        }

    def _clean_response_content(self, content: str) -> str:
        """清理响应内容"""
        # 移除可能存在的markdown代码块标记
        content = re.sub(r'```json\s*', '', content)
        content = re.sub(r'```\s*', '', content)
        content = content.strip()
        return content

    def _extract_json_from_text(self, text: str) -> Optional[Dict[str, Any]]:
        """从文本中提取JSON内容"""
        try:
            # 查找第一个{和最后一个}
            start = text.find('{')
            end = text.rfind('}') + 1

            if start != -1 and end != 0:
                json_str = text[start:end]
                return json.loads(json_str)
        except (json.JSONDecodeError, ValueError):
            pass

        return None

    def _handle_stream_response(self, response) -> Dict[str, Any]:
        """处理流式响应"""
        collected_content = []
        for chunk in response:
            if chunk.choices[0].delta.content:
                content = chunk.choices[0].delta.content
                collected_content.append(content)

        full_content = "".join(collected_content)
        cleaned_content = self._clean_response_content(full_content)

        # 尝试解析JSON
        parsed_content = None
        is_json = False
        try:
            parsed_content = json.loads(cleaned_content)
            is_json = True
        except json.JSONDecodeError:
            parsed_content = cleaned_content

        return {
            "content": parsed_content,
            "raw_content": full_content,
            "stream": True,
            "is_json": is_json
        }


# 初始化客户端：API Key 仅从环境变量读取，切勿硬编码（已在 .env / 生产环境变量配置）
deepseek_client = DeepSeekClient(
    api_key=os.getenv('DEEPSEEK_API_KEY', '')
)


@router.post("/chat", response_model=AIResponse)
async def ai_chat(request: AIRequest):
    """
    AI对话接口 - 确保返回标准JSON格式
    """
    try:
        logger.info(f"收到AI请求，模型: {request.model}, 字符数: {len(request.prompt)}")

        # 校验服务端是否已配置 DeepSeek Key
        if not deepseek_client.client.api_key:
            raise HTTPException(status_code=500, detail="服务端未配置 DEEPSEEK_API_KEY，请联系管理员")

        # 调用DeepSeek客户端
        result = deepseek_client.generate_response(request)

        # 构建标准化的响应数据
        response_data = {
            "detail": result["content"],
            "raw_content": result.get("raw_content"),
            "usage": result.get("usage", {}),
            "model": result.get("model", request.model),
            "finish_reason": result.get("finish_reason"),
            "is_json": result.get("is_json", False),
            "response_format": result.get("response_format", request.response_format)
        }

        return AIResponse(
            code=200,
            msg="success",
            data=response_data
        )

    except ValueError as e:
        logger.warning(f"请求参数错误: {str(e)}")
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.error(f"AI服务内部错误: {str(e)}")
        raise HTTPException(status_code=500, detail="AI服务暂时不可用")
