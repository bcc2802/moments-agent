# 安装与启动

本页介绍 Windows 本地版：取得完整目录，准备 Python，再选择自己的智谱 GLM-5.3-Flash API 或已登录的官方 Codex CLI ≥0.165.0，双击启动器。智谱路线不需要 Codex CLI。希望部署供浏览器访问的服务，见[云端部署](../cloudflare/README.md)；两种方式均需使用者或部署者提供自己的模型账号与额度。

## 1. 取得完整目录

从[项目 Releases](https://github.com/bcc2802/moments-agent/releases)下载对应版本完整 ZIP，按 `SHA256SUMS` 校验后完整解压；也可从[源码仓库](https://github.com/bcc2802/moments-agent)克隆并选择对应 tag。

应能看到正式入口 `start.cmd`、`scripts/`、`web/`、`templates/` 和 `README.md`。不要只复制启动器，也不要直接双击 `web/index.html`；界面需要本机后端读写工作区。`start-demo.cmd` 与固定脚本是开发者合成验收的附加选项，不是普通使用者的首轮入口。

## 2. 准备 Python

使用 Python 3.11、3.12 或 3.13，具体已验环境以 [验证记录](VALIDATION.md)为准。先在新的 PowerShell 窗口检查：

```powershell
py -3 --version
```

没有 `py` 时再检查 `python --version`。两者均不可用，再从 [Python 官方 Windows 下载页](https://www.python.org/downloads/windows/)选择适合自己系统的受支持版本并安装，参考 [官方 Windows 使用说明](https://docs.python.org/3/using/windows.html)。安装后重新打开终端确认命令可用。

基础界面与文字存档使用 Python 标准库，不需要先安装 Pillow、Node.js、Obsidian、TrendRadar 或飞书服务。若机器上有多个 Python，启动器优先 `py -3`，其次 `python`；想指定解释器可以用文末的手动启动方式。

## 3. 选择并配置写作模型

网页可选择“智谱 GLM-5.3-Flash”或“Codex”。只需完成所用服务的一条配置路线。材料会发送给选中的服务，模型用量或API费用由该服务及本人账号决定；本工具不附送额度，失败时不会自动换服务或重复付费请求。

### 路线A：智谱 GLM-5.3-Flash

使用本人有权使用的智谱API Key。先双击一次 `start.cmd` 创建工作区，再停止服务。在工作区根目录（默认 `%USERPROFILE%\MomentsAgent`）创建 `model.local.json`，例如：

```json
{
  "provider": "zhipu",
  "api_key_file": "<本机智谱密钥TXT文件的绝对路径>"
}
```

将占位文字换成本机真实TXT文件的绝对路径；JSON中的Windows路径可用正斜杠，若用反斜杠则需写成双反斜杠。TXT中放本人API Key，不要把密钥正文写进这个JSON、写作输入框或素材库。这个文件位于私有工作区，不在代码目录和分发包中。重新启动后，在网页选择智谱模型并检查状态。使用官方通用接口 `https://open.bigmodel.cn/api/paas/v4/chat/completions` 和模型标识 `glm-5.3-flash`，不是GLM Coding Plan专属端点；套餐权限与通用API可用性须按本人账号核对。

也可以在启动终端配置密钥文件路径，不在命令中写出密钥值：

```powershell
$env:MOMENTS_ZHIPU_API_KEY_FILE = '<本机智谱密钥TXT文件的绝对路径>'
.\start.cmd
```

后台也支持已在当前进程环境中配置的 `ZHIPU_API_KEY`。不要为了排错打印环境变量内容。终端的临时变量只被从同一终端启动的程序继承；希望双击入口长期使用时，优先使用私有工作区的文件路径配置。网页只显示服务、模型及配置状态，不显示密钥。连接错误需按提示修复，不会擅自回退到Codex。

官方参考：[GLM-5.3-Flash](https://docs.bigmodel.cn/cn/guide/models/vlm/glm-5.3-flash)、[HTTP API调用](https://docs.bigmodel.cn/cn/guide/develop/http/introduction)。本项目使用Python标准库HTTP，不额外安装模型SDK。

### 路线B：官方 Codex CLI 0.165.0 或更新版与账号

先检查 `codex --version` 和 `codex login status`。本项目要求本机可调用的原生 CLI 版本至少 0.165.0，旧版本会显示 `CLI_UPDATE_REQUIRED`；只打开一个 ChatGPT 网页或仅登录成功都不代表满足要求。

缺 CLI 或版本过旧时，按 [Codex 官方安装页](https://learn.chatgpt.com/docs/cli)选择 Windows 独立安装。下面是由使用者主动执行的固定版本安装/更新步骤，参数依据 [官方 Windows 部署说明](https://learn.chatgpt.com/docs/enterprise/windows-deployment)：

```powershell
$MomentsCodexRelease = '0.165.0'
$MomentsCodexInstaller = Join-Path $env:TEMP 'moments-codex-install.ps1'
Invoke-WebRequest -UseBasicParsing -Uri 'https://chatgpt.com/codex/install.ps1' -OutFile $MomentsCodexInstaller
# 先审阅下载的官方安装器，再由本人执行下一行。
powershell -ExecutionPolicy Bypass -File $MomentsCodexInstaller -Release $MomentsCodexRelease
```

这会安装/更新当前用户的官方 CLI，而不是把运行时放进本项目。启动器不会自动安装或升级，也不会改 Codex 全局 `config.toml`。安装后重新打开终端，用 `Get-Command codex` 与 `codex --version` 确认实际命中新版，避免旧 PATH 或多个安装位置仍启动旧版。采用更新版本时，主动选择批准的版本并重新核对实际运行结果。

此前联调中，旧 CLI 0.142.2 虽可登录，当前模型请求仍失败；改用已核验官方发行资产的 CLI 0.165.0 与 `gpt-6.1-sol` 后，实际完成过三稿、只改第2稿、手工终稿、保留、刷新和搜索。本轮首页、必填与素材规则调整的实际结果以 [验证记录](VALIDATION.md)为准，不用旧版结果代替新验收，也不代表所有账号或陌生使用者已通过。

接着由账号本人执行：

```powershell
codex login
codex login status
```

按官方流程登录自己有权使用的账号。不要把密钥、登录文件或 token 填到本项目表单里。CLI 登录成功只证明认证状态，模型权限、用量、网络和具体生成仍以当次结果为准。

### 指定已有原生 CLI，不改全局配置

如果新版与旧版并存，或你将官方 CLI 隔离安装在另一个本机目录，可以用 `MOMENTS_CODEX` 明确指定已有可执行文件。必须是本机现存文件的绝对路径，不能是 `.cmd/.bat` 包装器。以下使用官方独立安装器的默认 Windows 路径；隔离安装时替换成自己的实际路径：

```powershell
$env:MOMENTS_CODEX = Join-Path $env:LOCALAPPDATA 'Programs\OpenAI\Codex\bin\codex.exe'
& $env:MOMENTS_CODEX --version
& $env:MOMENTS_CODEX login status
.\start.cmd
```

在同一终端中启动才能继承这个变量，直接从资源管理器双击不会继承临时终端变量。该设置仅选择当前进程使用的已有 CLI，不复制或打包运行时，也不更改 PATH、全局 Codex 设置或认证文件；CLI 仍使用原有受支持认证。

当前适配器明确使用 `gpt-6.1-sol`，不依赖旧 CLI 的默认模型。账号不支持或该模型不可用时，可按 [官方模型说明](https://learn.chatgpt.com/docs/models)选择自己账号实际支持的模型，用启动终端的环境变量覆盖，例如：

```powershell
$env:MOMENTS_MODEL = 'gpt-6-luna'
.\start.cmd
```

这只是选择示例，不承诺每个账号都能用该模型。变量只影响从此终端启动的进程，不修改 Codex 全局模型设置。切换模型不能绕过 0.165.0 的最低版本要求。若出现已退休模型、额度或网络错误，先核对具体错误，不反复提交或擅自购买额度。

## 4. 双击正式入口开始

双击 `start.cmd`。默认工作区是 `%USERPROFILE%\MomentsAgent`，打开正式工作台。先在“个人说明书”保存身份与业务、目标读者、表达偏好三项红字“必填”，选择已配置的模型，即可用当次材料生成。历史作品选填，可以稍后补充。

工作台首页直接显示本次材料输入框与三稿结果；“开始写作”仅定位同一工作台，不另开写作页面。个人说明书和素材库用于准备资料，“我的内容”用于回看稿件，不需要加载合成人物或切换演示模式。

浏览器会打开本机地址，服务只监听 `127.0.0.1`，端口由系统分配。浏览器没自动打开时，查看启动窗口里的本机 URL；不要把带会话 token 的地址发给别人。

启动窗口需要保持运行。关闭浏览器页面不等于关闭后端；在启动窗口按 `Ctrl+C` 停止服务。再次启动会读取原工作区，不覆盖已有稿件。目标目录已存在但不是合法工作区时会拒绝，不要删除原文件来绕过检查。

## 5. 第一次在界面里做什么

1. “个人说明书”：只填写身份与业务、目标读者、表达偏好三项必填并保存。这一步只存本机，不清空Markdown里原有的其他资料。
2. “素材库”（选填）：可稍后导入自己认可、允许参考的 Markdown/文本历史原文，或粘贴一篇作品。原文件不覆盖，导入不推断已发表。
3. 工作台首页：选择已配置的写作模型，写本次材料和观点，阅读发送范围后点击生成。会把本次材料、资料、偏好及最多两篇导入原文/本人确认已发最终稿交给所选服务；没有历史也能写。三种切口有醒目的标题与视觉标记，便于比较。
4. 在稿件卡片里选看、复制，在“让AI改一改”选择“更口语化 / 更真实 / 更专业”并可补充意见，或手工保存终稿。预设不改变事实边界；这些操作都不等于发表。
5. 到“我的内容”回看稿件；页面搜索只覆盖已加载内容。只有本人已在微信手工发表，才确认已发，此时这条最终正文进入素材库和后续写作参考；未发稿及同轮其他候选不入。初次体验不需要发表。

完整步骤见 [第一轮教程](QUICKSTART.md)。

生成或润色后，全局悬浮状态持续显示真实任务阶段与已用时；切换导航页仍可见，任务期间AI按钮会防止重复提交。短暂轮询失败会提示连接状态，不把未知任务当成已结束；请等待明确完成或失败。没有按时间编造的完成百分比或模型内部推理展示。

### 开发者附加：合成验收

`start-demo.cmd` 可创建/恢复独立的 `%USERPROFILE%\MomentsAgentDemo` 合成资料空间，供开发者检查；不是正式用户推荐入口。产品界面没有供用户点击的“填入合成示例”、Demo 徽标或“模拟已发”按钮，不以这些元素作为检查步骤。合成身份不能当成本人的真实身份，应在验收记录中明确资料性质。

固定 `scripts/demo.py` 用于机械存档和状态演练，不调用模型；合成空间中的真实生成/AI改稿仍调用所选模型并使用对应账号用量。发布机制模拟只在开发者隔离合成测试中执行，不在正式界面假报已发。

## 手动启动与常见卡点

在项目根目录 PowerShell 可运行：

```powershell
python -B .\scripts\app.py
```

指定自己的新工作区：

```powershell
$MomentsWorkspace = Join-Path $env:USERPROFILE 'MyMomentsWorkspace'
python -B .\scripts\app.py --workspace $MomentsWorkspace
```

路径是示例，请替换为自己的目录；父目录必须存在，工作区必须在代码目录之外。可以加 `--no-browser` 只启动服务，再自行打开输出的 URL。

| 表现 | 下一步 |
| --- | --- |
| 找不到 Python | 按第2步安装并重新打开终端；不要改文件扩展名冒充解释器 |
| 界面能开，Codex 未就绪 | 按第3步检查原生 CLI 版本、登录和 PATH；界面打开不代表模型可用 |
| 智谱未配置或请求失败 | 检查本工作区 `model.local.json` 的路径、TXT格式、账号权限及通用API余额；不要打印密钥，不会自动改用Codex |
| `CLI_UPDATE_REQUIRED` 或旧版已登录却模型失败 | 主动更新至0.165.0或更新版；多版本并存时用 `MOMENTS_CODEX` 指定新版原生文件，再重新启动 |
| 只有 npm 的 `.cmd` 包装器 | 当前适配器不执行批处理包装器；按官方 Windows 独立安装路径准备原生 CLI |
| 已登录但生成失败 | 核对账号可用模型、额度和网络；页面应显示真实错误，不能改用固定三稿冒充成功 |
| 生成按钮不可用，提示准备资料 | 保存三项必填说明并核对所选模型状态；历史选填。修正错误时原已填内容应保留 |
| 切换页面后想知道AI是否还在工作 | 查看全局悬浮状态；任务未明确终止时不要再次提交，短暂失联不代表失败 |
| 连接不到本机 | 保持启动窗口运行，重新从它给出的 URL 打开；不要把服务绑定到公网 |
| 想换资料目录 | 使用手动 `--workspace`；不要把私人资料放进代码目录或发布包 |

[故障排查](TROUBLESHOOTING.md) · [隐私](PRIVACY.md) · [官方实践与分发边界](OFFICIAL_PRACTICES.md)
