# Docker secrets

复制每个 `.example` 文件为同名 `.txt` 文件，并替换为本地随机值。`.txt` 文件已被
`.gitignore` 忽略，不得提交真实密码、Token 或 API Key。生产环境使用外部 secret manager
或受控部署流水线生成这些文件。
