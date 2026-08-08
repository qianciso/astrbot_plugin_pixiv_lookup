# 贡献指南

感谢你改进本插件。提交变更前请：

1. 不要提交 Pixiv refresh token、代理凭据、AstrBot 配置文件或真实 R18 图片。
2. 为行为变更增加测试，并执行 `pytest -q`。
3. 保持 provider、图片反代、内容策略和消息发送之间的接口边界。
4. 新增网络目标时必须使用明确的主机白名单，不能接受任意下载 URL。
5. 日志只记录作品 ID、页码、分级和错误类型，不能记录 token 或完整图片 URL。
6. `tag_aliases.tsv` 使用 `alias<TAB>target` 格式；修改筛选规则后应运行
   `python scripts/build_tag_aliases.py` 重新生成，并保留第三方许可说明。

问题报告请包含 AstrBot、NapCat 和 Python 版本，以及脱敏后的插件日志。
