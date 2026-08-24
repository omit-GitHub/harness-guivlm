# -*- coding: utf-8 -*-
"""Harness integrations — 真实设备适配层。

本包提供 ADB Android 适配，实现 Harness 的 Protocol 接口：
  - AdbClient：底层 ADB 命令封装（白名单方法，无任意 shell 入口）
  - AdbStateProvider：设备状态采集（截图 + dumpsys → UiState）
  - AdbActionExecutor：ActionExecutor Protocol 实现

安全约束：
  - 所有 subprocess.run 使用 list 参数，shell=False
  - input_text 参数化转义，禁止 shell 注入
  - 不提供任意 adb shell 命令执行入口
  - 不记录任何密钥或敏感信息
"""
