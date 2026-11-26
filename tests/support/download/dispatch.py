"""
下载链 ``run_module`` 的测试替身：按目标方法签名过滤参数。
"""

from __future__ import annotations

import inspect
from typing import Any


def run_module_by_signature(owner: Any) -> Any:
    """
    生成与真实调度器一致的 ``run_module`` 替身，只传目标方法签名里声明的参数。

    下载链会把网盘等可选参数一并交给 ``run_module``；真实调度器只转发各模块方法
    实际接收的参数，测试替身若原样透传，qbittorrent 等模块会收到多余的关键字参数。

    :param owner: 提供 ``method`` 同名方法的模块对象
    :return: 可直接赋给 ``chain.run_module`` 的可调用对象
    """

    def run_module(method: str, **kwargs: Any) -> Any:
        """
        过滤掉目标方法不接收的参数后调用该方法。

        :param method: 模块方法名
        :param kwargs: 下载链传入的全部关键字参数
        :return: 目标方法的返回值
        """
        func = getattr(owner, method)
        allowed = set(inspect.signature(func).parameters)
        return func(**{key: value for key, value in kwargs.items() if key in allowed})

    return run_module
