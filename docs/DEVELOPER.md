# 开发者入口

本地版沿用 Skill＋Python＋Markdown。Skill负责对话、选题和正文判断；模型生成文案；Python维护读取、文件事实源、状态、备份与恢复。单独运行Python不等于完成写作。

## 目录与事实源

- `.agents/skills/moments-draft/`：按场景加载的自然语言工作流。
- `scripts/workspace.py`：新工作区初始化、只读诊断、三份资料读取与只读历史输入查询。
- `scripts/moments.py` 及配套模块：自产稿件、准备、保留、历史查询、任务与媒体等接口。
- `templates/` 与 `examples/`：可分发模板和明确虚构的材料；示例不是用户数据。
- `tests/v02/`：合成数据测试，不能用真实内容库运行模拟发表。

工作区 `config.json` 与唯一 `state.json` 并列。配置中的绝对路径由初始化工具按本机位置生成；源码不附作者配置。`inputs/history/` 是普通只读输入，不能转换为假自产已发稿；`archive/` Markdown才是Agent内容库的正文、标签、发表状态事实源。

## 工作区CLI

以下命令从候选根执行。`$MomentsWorkspace` 为代码目录外的新工作区绝对路径。

```powershell
python -B .\scripts\workspace.py init --workspace $MomentsWorkspace --demo
python -B .\scripts\workspace.py doctor --workspace $MomentsWorkspace
python -B .\scripts\workspace.py profile --workspace $MomentsWorkspace
python -B .\scripts\workspace.py history --workspace $MomentsWorkspace --query '清单' --limit 10
python -B .\scripts\workspace.py history --workspace $MomentsWorkspace --read '<实际返回的ID>'
```

`--demo` 可省略；query/read互斥。输出为JSON，成功或已合格初始化退出0，错误与部分读取失败退出1。profile/history输出可能包含私人数据，不能直接作为公开日志。初始化若中断留下不完整新目录，返回的`changed`与错误必须保留，不能声称零变更。

业务CLI显式传 `--config <工作区/config.json>`，输入为结构化JSON或UTF-8 `--request-file`。不要省略配置后借用源码目录的假默认；不要用手工编辑state替代业务API。具体动作字段以代码和测试为准，日常用户无需记住它们。

## 验证

先做单项针对验证，再运行所需隔离回归。发行候选的验证报告只记录本次实际执行结果，不搬用原作者生产项目的历史数字。

完整发行候选隔离入口（基础环境不需要Pillow）：

```powershell
python -B .\scripts\verify_release.py --core-only --output-dir <代码目录外的新证据目录>
```

具备Pillow时可省略`--core-only`验证可选图片处理。验证器从`release-files.json`复制完整静态候选到临时目录，分别运行`tests/v02`及`tests`，记录源码哈希、运行环境和实际结果。[验证记录](VALIDATION.md)

只审核文件、语法和本地链接：`python -B scripts/release.py check`。打包用`python -B scripts/release.py pack --output <代码目录外的新ZIP绝对路径>`，不覆盖已有包；ZIP仅含白名单与MANIFEST.json；本项目采用根目录MIT许可，生成ZIP本身不会自动上传或发布。

原核心隔离入口仍保留：

```powershell
python -B .\scripts\verify_isolated.py
```

此入口在候选相邻的`iterations/`创建一次独立代码/测试副本及运行证据，不复制生产config、state或真实Markdown。它按自身清单验证核心模块；新增工作区、文档与包装检查还应分别执行，不能因为核心回归成功就声称全部发行路径通过。

单独运行测试时，确认使用合成夹具和隔离目录。测试及演示文件出现“已发布”只表示模拟状态转换；真人原文、经营效果和在线第三方服务需独立验证。

## 可选适配与公开交付

扩源先改来源契约、域名保护和测试，不只加配置名字；保持当前七源基线与旧记录只读兼容。飞书消息入口和托管进程不在本候选打包范围，任务API可复用但要自行完成身份、权限、队列、幂等和部署验证。

提交候选前检查真实文件清单、绝对路径、个人绑定、凭证模式、模板与示例标识、相对链接、依赖及权利说明。`.gitignore`不是审查结果；不要整目录加入runs、备份、工作区或其他安装的源码。本项目已采用根目录MIT许可，新增第三方内容仍须单独核对。远程上传与CI状态以实际结果为准。

## 云端开发

`cloudflare/` 是独立的 Cloudflare Worker 项目，前端沿用工作台，数据以 D1 为事实源，队列负责异步写作任务。它不依赖本地 Python、Codex、维护者的网站或私人工作区。请从[云端教程](../cloudflare/README.md)执行锁文件安装、Node 24 测试和 Wrangler dry-run。

本地核心回归与云端测试分别执行。不要用真实密钥、稿件或已部署数据库作为测试夹具；CI 只验证，不创建远程资源、不调用付费模型、不自动部署。两种实现分别维护自己的存储适配，公共产品语义必须保持一致。
