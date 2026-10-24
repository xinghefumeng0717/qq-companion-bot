# GitHub 发布手册（第二次发布实录 + 以后复用）

> 第一次发布（v1.0.0）的教训：没洗历史、没 LICENSE、敏感信息直接上去了（已靠第二次发布强推覆盖救回）。
> 本手册是第二次发布（v2.0.0，2026-10-05）的完整实录，以后发新版照此办理。

## 一、发布前硬闸（不满足不许推）

1. **红线文件零跟踪**：`git ls-files | grep -E "config\.toml|local_env|test_keys|^data/|characters/example"` 必须零输出；
2. **测试全绿**：`./venv/Scripts/python.exe -m unittest discover -s tests`；
3. **敏感串全历史扫描**（仓库即记忆 = 历史也是曝光面）：

```powershell
# 【本地电脑执行】
cd "D:\qq-companion"
.\venv\Scripts\python.exe -m pip install git-filter-repo   # 只需装一次
# 替换清单格式：旧串==>新串，逐行写入 replacements.txt，然后：
.\venv\Scripts\python.exe -m git_filter_repo --replace-text "D:\qq-wash\replacements.txt" --force
# 提交信息里的敏感串要单独再洗一遍（--replace-text 只管文件内容）：
.\venv\Scripts\python.exe -m git_filter_repo --replace-message "D:\qq-wash\replacements.txt" --force
# 作者身份脱敏（mailmap 格式：新名 <新邮箱> 旧名 <旧邮箱>）：
.\venv\Scripts\python.exe -m git_filter_repo --mailmap "D:\qq-wash\mailmap.txt" --force
```

4. **洗后必做两件事**：①全历史 grep 自检零残留（**注意变体**：源码里双反斜杠 `D:\\路径` 与单反斜杠是两个不同字面量，都要列进清单）；②重跑测试——洗历史会改 HEAD 的文件内容（占位符替换），测试必须保持全绿；
5. **前置备份**：`git clone --mirror` 整仓历史到本地另一目录（洗坏了能整个搬回来）。

v2.0.0 实际洗净的串：第三方姓名 ×4 写法、机主 QQ 号、服务器 IP、本机用户名路径、私有目录、机主真名与称呼（含一条漏在提交信息里的）。

## 二、推送

```powershell
# 【本地电脑执行】filter-repo 会移除 origin，需按当前地址重加
cd "D:\qq-companion"
git remote add origin https://github.com/xinghefumeng0717/qq-companion-bot.git
git push --force origin master:main     # 本地 master → 远端 main（本项目固定映射）
git tag -a v2.0.0 -m "第二轮 V2 大版本"
git push origin v2.0.0
```

## 三、打 Release（网页操作）

仓库页 → 右侧 **Releases** → **Draft a new release** → **Choose a tag** 选刚推的 `v2.0.0` → 标题填版本号 → 描述粘贴发布说明 → **Publish release**。

## 四、开源内容分层（本项目定稿的标准）

| 层 | 内容 | 处置 |
|---|---|---|
| 零容忍 | API key / token / 密码 | 永不入库（gitignore），泄露几分钟内会被机器人盗刷 |
| 现实身份 | 真名、QQ 号、手机号、IP、私有路径、第三方姓名 | 洗历史清零 |
| 语料与卡内容 | 真实聊天记录、青梓角色卡本体 | 永不跟踪（data/ 与 characters/example/ 被拦） |
| 项目内容 | "有个角色叫青梓"这件事、迭代故事、测试工程、卡结构模板（characters/example/） | 大方公开——这是项目的故事与价值 |

License：MIT（署名 星河赴梦）。
