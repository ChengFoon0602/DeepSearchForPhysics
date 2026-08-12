from agent.prompts import sub_agents_content
from tools.rag_tools import list_knowledge_documents, search_knowledge_base

knowledge_base_agent = {
    "name": sub_agents_content['kb']['name'],
    "description": sub_agents_content['kb']['description'],
    "system_prompt": sub_agents_content['kb']['system_prompt'],
    "tools": [list_knowledge_documents, search_knowledge_base]
}
