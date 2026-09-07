"""RemoteSensingAgent 包：模型驱动的工具循环。

- waiting.py: HITL 等待/恢复协议（WaitingForUser 信号 + action code 翻译）
- tools.py:   工具注册表，包现有领域函数，经 views 运行时查找保持 patch 契约
- loop.py:    agent_step（唯一 GLM 决策拦截点）+ run_agent_loop + resume
"""
