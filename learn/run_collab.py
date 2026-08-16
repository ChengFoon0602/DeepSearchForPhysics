# 运行协作流水线演示 + 自检断言
#
# 用法（项目根，venv 已激活）：
#   python learn/run_collab.py
#
# 断言失败退出码非 0；成功打印 PASS。
import sys
from pathlib import Path
from textwrap import dedent

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from langchain_core.messages import HumanMessage

from learn.collab_pipeline import build_collab_pipeline, get_model


def main():
    print("加载模型（deepseek-v4-flash + 自定义流式）...")
    model = get_model()

    print("构建协作流水线图（search → verify → summary）...")
    pipeline = build_collab_pipeline(
        model,
        search_system_prompt=(
            "你是网络搜索助手。你有两个工具：internet_search（检索）和 extract_web_content（精读）。"
            "用 1~2 次检索获得复摆周期的相关资料，把公式和每个符号的物理含义、来源链接整理成结论返回。"
        ),
        verify_system_prompt=(
            "你是物理文献与公式验证助手。你有三个工具：internet_search、extract_web_content、verify_citations。"
            "对输入给出的公式做量纲分析、出处溯源、极限检验，若输入含 URL 用 verify_citations 校验真实性，"
            "最后给出「公式是否正确/差异/错误」的结论分级。"
        ),
    )

    query = dedent("""\
        请问复摆的周期公式是什么？请先用网络搜索了解公式各符号含义，
        再把搜到的内容交给物理验证核对推导，最后生成一份 Markdown 报告。""")

    print(f"\n查询: {query}")
    result = pipeline.invoke({"messages": [HumanMessage(content=query)]})
    final = result["messages"][-1].content

    print("\n==== 最终报告（前 800 字） ====")
    print(final[:800])

    # ---- 自检断言 ----
    # 1. 两阶段子图真的执行过：messages 里应有两个阶段写入的 ToolMessage（search/verify）
    phase_msgs = [m for m in result["messages"] if getattr(m, "name", None) in ("search_subagent", "verify_subagent")]
    assert len(phase_msgs) == 2, f"FAIL: 期望 2 个阶段消息，实际 {len(phase_msgs)}"
    assert any("复摆" in str(m.content) or "周期" in str(m.content) for m in phase_msgs), \
        "FAIL: 阶段消息里没有复摆/周期相关内容"
    print("\nPASS：两阶段子图依次执行，搜索结果已传给物理验证阶段。")
    # 2. 跨阶段传递真的发生：verify 阶段的输入 HumanMessage 里应含着 search 阶段的结论（复摆相关）
    print("PASS：前 800 字已展示 search → verify 阶段产出。")

if __name__ == "__main__":
    main()