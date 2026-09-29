# 开发者操作习惯与终端命令生成规范

> 本文件由项目所有者制定，优先级高于一切默认习惯。在本项目内提供任何终端命令时必须遵守。

## 一、固定环境与路径信息（禁止使用占位符，直接填写真实值）

- **本地开发机**：Windows 11
  - 项目根目录（PowerShell/CMD）：`D:\qq-companion`
  - 项目根目录（Git Bash）：`/d/qq-companion`
- **云服务器**：Ubuntu
  - IP：`SERVER_IP`，SSH 用户：`ubuntu`
  - 部署路径：`/opt/qq-companion`
  - Python 虚拟环境：`/opt/qq-companion/venv/bin/python`
  - Systemd 服务名：`qq-companion.service`

## 二、终端命令生成死律

1. **明确标注执行地点**：每条命令必须醒目标注【本地电脑执行】或【服务器终端执行】；严禁本地命令（scp）与服务器命令（systemctl、chown）混在同一代码块。
2. **拒绝任何占位符**：禁止 `<服务器IP>`、`<path>` 等变量，一律填真实值，命令必须可直接一键复制执行。
3. **双端同步必须"先传后挪"**：
   - 步骤 1：本地 `scp` 传到服务器 `/tmp/` 下；
   - 步骤 2：服务器端复制到部署路径，并附带属主修复 `sudo chown -R ubuntu:ubuntu /opt/qq-companion`；
   - 不得假定 /tmp 已有文件（先创建目录再传）。
4. **绝对保护核心文件（高压红线）**：
   - **严禁**提供直接覆盖服务器 `config.toml` 的命令（含真实 QQ 账号、OneBot 密钥、API Key，覆盖即瘫痪）。配置变更一律改为"在服务器上编辑指定字段"的指引；
   - **严禁**修改或覆盖角色卡 `characters/qingzi/character.json`（本地编辑后需所有者确认方可同步）。
5. **回答风格**：直奔主题，精简、高可靠、无歧义、可直接复制的命令块。

## 三、项目角色分工备忘

- 角色卡（青梓人设）由项目所有者与其指定流程维护，任何执行代理不得改动；
- PLAN.md、FIXES*.md、DEPLOY.md 等历史文档均已归入 `docs/` 目录：PLAN.md 为架构蓝图，FIXES*.md 为历次迭代任务书，DEPLOY.md 为部署手册。
