from agent.prompts import sub_agents_content
from tools.tavily_tool import internet_search, extract_web_content


physics_lit_agent = {
    "name": sub_agents_content['physics_lit']['name'],
    "description": sub_agents_content['physics_lit']['description'],
    "system_prompt": sub_agents_content['physics_lit']['system_prompt'],
    "tools": [internet_search, extract_web_content]
}