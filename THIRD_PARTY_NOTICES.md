# 随包运行环境

GitHub Releases 的 Apple Silicon 安装包（v1.0.0、v1.1.0）包含 CPython 3.14.9，来自 Astral 维护的 [python-build-standalone](https://github.com/astral-sh/python-build-standalone) 项目。

- 上游发布版本：[20261021](https://github.com/astral-sh/python-build-standalone/releases/tag/20261021)
- 原始资产：`cpython-3.14.9+20261021-aarch64-apple-darwin-install_only_stripped.tar.gz`
- 原始资产 SHA-256：`9f8c3480b8f0db553c270db548e8a21ae8e39df412e5661d66e8050c5c6ed2a8`
- [分发格式与可搬移安装说明](https://gregoryszorc.com/docs/python-build-standalone/main/distributions.html)

App 中完整保留这份运行环境的解释器、标准库及所附许可证。CPython 的许可证位于 `Contents/Resources/python/lib/python3.14/LICENSE.txt`，上游仓库同一版本的 LICENSE 文件及 `python-licenses.rst` 位于 `Contents/Resources/python/upstream-licenses/`，运行环境来源记录位于 `Contents/Resources/python/runtime-source.json`。pip 及其附带组件的原始版权与许可文件保留在运行环境内的原位置。

`upstream-licenses/` 同时保留上游其他平台或可选组件的通知，具体组件以随包文件为准。各第三方组件仍受各自的许可证约束；本文件不替代或变更这些许可证。

复现发行构建时，请从上述固定上游版本取得运行环境并核对 SHA-256，同时保留相应版权和许可通知。源码构建默认使用本机 Python，只有传入 `--bundle-python` 时才会把指定运行环境一并复制进 App。
