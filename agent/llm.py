from dotenv import load_dotenv,find_dotenv
import os
from agent.reasoning_model import ReasoningChatModel

# 加载配置文件
# find_dotenv() 确保找到 .env文件 递归查询当前项目文件夹
load_dotenv(find_dotenv())

# 用 ReasoningChatModel（处理 deepseek-v4-flash 等思考模型的真流式）：
# langchain-openai 原版 _stream 对 reasoning chunk（delta.content 为 null）合并报
# "No generation chunks were returned"，自定义模型绕开 SDK 做 raw SSE 解析。
model = ReasoningChatModel(
    model=os.getenv("LLM_QWEN_MAX"),
    base_url=os.getenv("OPENAI_BASE_URL"),
    api_key=os.getenv("OPENAI_API_KEY"),
    max_retries=3,   # 上游瞬断（RemoteDisconnected/Timeout）自动重试，学习日记问题 5
    timeout=60,      # HTTP 超时 60s，长检索任务不轻易被打断
)
