# 贡献指南 / Contributing

感谢你愿意改进 **AI Crypto Portfolio**。提交之前请先阅读本指南。

## 授权

本项目以 [Apache License 2.0](LICENSE) 发布。依许可证第 5 条，除非你在提交时另行明确声明，你提交的任何贡献都将按 Apache-2.0 授权并入本项目。
请确认你有权提交所贡献的内容，并且内容不含受其他不兼容许可证约束的代码。

新增源文件可以在文件头加上 SPDX 标识（可选）：

```python
# SPDX-License-Identifier: Apache-2.0
```

## 提 Issue

- 描述问题或需求，附上复现步骤、期望结果与实际结果、环境信息（OS、Python、Node 版本）。
- 贴日志前请**先脱敏**：删除 API key、令牌、账户信息与本机路径。
- 安全问题请私下联系维护者，不要在公开 Issue 中披露细节。

## 提交 Pull Request

1. Fork 本仓库，从 `main` 拉出独立分支，每个 PR 聚焦一个主题。
2. 按 [README](README.md#快速开始) 搭建环境：`pip install -r requirements-dev.txt`，并在 `apps/web` 中执行 `npm ci`。
3. 提交前至少运行：

   ```bash
   # 仓库根目录
   PY=.venv/bin/python bash scripts/run-tests.sh
   # apps/web
   npm run lint && npm run verify && npm run build
   ```

   全新克隆中已知会失败的测试见 README 的“验证状态”一节。请确保你的改动没有引入新的失败。
4. 在 PR 描述中说明改了什么、为什么改、如何验证。修改了已有文件时，按许可证第 4(b) 条保留原有的版权与署名声明。

## 约定

- **不要提交**：`.env`、任何凭据、`data/` 下的运行数据、`node_modules/`、`dist/`、日志或数据库文件。
- 选币参数的改动请通过 `packages/config/board-variants.json` 的 `overrides` 与规则 manifest 流程完成，**不要直接修改已冻结的主板面（选币榜 v1.4.0）参数**。
- 生产路径禁止启用 `SM_FAST` / `--fast-confirm`；`dmr_executable=false` 的板面不得进入 `DMR_PARAM_WHITELIST`。
- 前端不重算 Score，浏览器不直连交易所、不持有密钥。
- 展示图表或 CoinGecko 数据的页面须保留 `NOTICE` 中要求的署名。

## 署名

贡献被合并后，你的署名将保留在 Git 历史中，并与原项目作者 [@shenchuangwl](https://github.com/shenchuangwl) 的归属并存（见 [COPYRIGHT.md](COPYRIGHT.md)）。
