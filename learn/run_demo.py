# 运行演示 + 自检断言（三 stage 独立跑）
#
# 用法（项目根）：
#   python learn/run_demo.py --stage B1
#   python learn/run_demo.py --stage B2
#   python learn/run_demo.py --stage B3
#   python learn/run_demo.py            # 全跑
#
# 断言失败退出码非 0；成功打印 PASS。
import argparse
import sys
from pathlib import Path

# 项目根进 sys.path（复用 agent.reasoning_model / .env）
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from langchain_core.messages import HumanMessage, ToolMessage
from langgraph.types import Command

from learn.mini_orchestrator import (
    add_approval_node,
    add_numbers,
    build_agent_loop,
    build_subagent,
    build_task_tool,
    get_model,
)


# ============ B1：最小 agent 循环 ============
def demo_b1(model):
    print("== B1: 最小 agent 循环（model→tools→model）==")
    agent = build_agent_loop(model, tools=[add_numbers], system_prompt="你是数学助手，遇到计算必须调用 add_numbers 工具。")
    result = agent.invoke({"messages": [HumanMessage(content="计算 1+2，用工具算")]})
    final = result["messages"][-1].content
    print("  最终回答:", final[:100])
    # 断言：模型调用了工具（存在 ToolMessage）
    has_tool = any(isinstance(m, ToolMessage) for m in result["messages"])
    assert has_tool, "B1 FAIL: 没有 ToolMessage（模型没调工具）"
    assert "3" in final, f"B1 FAIL: 回答不含结果 3: {final}"
    print("  PASS\n")


# ============ B2：task 子智能体 ============
def demo_b2(model):
    print("== B2: task 子智能体（主 agent→task→子 agent→Command 注入）==")
    # 子智能体：研究员，会算数
    researcher = build_subagent(model, tools=[add_numbers], system_prompt="你是研究员，计算必须用 add_numbers 工具，只返回结果数字。")
    # 主 agent：能调 task 工具
    task_tool = build_task_tool(subagents={"researcher": researcher})
    main_agent = build_agent_loop(
        model, tools=[task_tool],
        system_prompt=(
            "你是一个协调者。遇到需要计算的任务，调用 task 工具，subagent_type 传 'researcher'。"
            "不要自己算，全部交给子智能体。"
        ),
    )
    result = main_agent.invoke({"messages": [HumanMessage(content="计算 3*4 是多少")]})
    final = result["messages"][-1].content
    print("  最终回答:", final[:120])
    # 断言：出现了 TaskMessage 结果（子智能体返回的内容注入了主图）
    texts = [m.content for m in result["messages"] if hasattr(m, "content")]
    sub_result = [t for t in texts if "12" in str(t)]
    assert sub_result, f"B2 FAIL: 子智能体结果(12)没回传: {texts}"
    print("  PASS（子智能体返回 12 注入主图）\n")


# ============ B3：interrupt/resume 审批 ============
def demo_b3(model):
    print("== B3: interrupt/resume 审批节点 ==")
    from learn.mini_orchestrator import generate_report
    agent = add_approval_node(
        None,
        model=model, tools=[add_numbers, generate_report],
        system_prompt="你是助手。用户要报告时调用 generate_report 工具。",
    )
    config = {"configurable": {"thread_id": "b3-demo"}}
    import asyncio

    async def run():
        # 第一次跑：到审批点挂起，捕获 __interrupt__
        intr = None
        async for chunk in agent.astream(
            {"messages": [HumanMessage(content="生成一份标题为'测试'内容为'abc'的报告")]}, config=config
        ):
            for k, v in chunk.items():
                if k == "__interrupt__":
                    intr = v[0].value
        assert intr and intr.get("tool") == "generate_report", "B3 FAIL: 没在审批点挂起"
        print("  审批挂起, 待审:", intr)

        # 模拟人类批准 → resume
        msgs = []
        async for chunk in agent.astream(Command(resume={"approve": True}), config=config):
            for k, v in chunk.items():
                if isinstance(v, dict) and "messages" in v:
                    msgs.extend(v["messages"])
        return intr, msgs

    intr, msgs = asyncio.run(run())
    # 断言：resume 后有 ToolMessage（generate_report 执行了）且有最终 AIMessage
    has_tool = any(isinstance(m, ToolMessage) for m in msgs)
    has_final = any(getattr(m, "content", "") and not getattr(m, "tool_calls", None) for m in msgs)
    assert has_tool, "B3 FAIL: resume 后没有 ToolMessage"
    assert has_final, "B3 FAIL: resume 后没有最终回答"
    print("  PASS（挂起→批准→工具执行→最终回答）\n")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", choices=["B1", "B2", "B3"], help="只跑指定 stage")
    args = parser.parse_args()

    print("加载模型（deepseek-v4-flash + 自定义流式）...")
    model = get_model()

    stages = [("B1", demo_b1), ("B2", demo_b2), ("B3", demo_b3)]
    if args.stage:
        stages = [s for s in stages if s[0] == args.stage]

    for name, fn in stages:
        try:
            fn(model)
        except Exception as e:
            print(f"\n❌ {name} 失败: {type(e).__name__}: {e}")
            sys.exit(1)
    print("全部 PASS ✓")


if __name__ == "__main__":
    main()
