# 官方实践与本项目的实现范围

## 智谱、可选历史与任务状态（当前版本）

- 依[智谱模型文档](https://docs.bigmodel.cn/cn/guide/models/vlm/glm-5.3-flash)及[官方HTTP接口](https://docs.bigmodel.cn/cn/guide/develop/http/introduction)，接入 `glm-5.3-flash`，使用固定官方HTTPS通用端点和后端Bearer认证。密钥按本人明确授权从本机TXT或进程环境读取；网页只选择提供方，不接收或返回密钥、文件指针、任意端点。拒绝重定向，不自动重试或回退到Codex。
- 依[对话补全文档](https://docs.bigmodel.cn/api-reference/模型-api/对话补全)，使用流式输出、JSON模式及受控的低推理强度。仅保留最终 `content`，不存储或展示 `reasoning_content`；确认完整结束后复用三稿或修订正文的结构校验，再交给本地控制器保存。
- 依[NN/g系统状态可见性](https://www.nngroup.com/articles/visibility-system-status/)及[Carbon加载反馈](https://www.carbondesignsystem.com/building-blocks/core/components/loading/guidelines)，点击后立即显示一个全局悬浮状态，提供模型、目标稿件、真实后台阶段及已用时；阶段依据请求、实际响应、校验和保存事件更新。切换导航仍可见，处理中禁重复操作，短暂轮询断连只恢复状态，不自动再次调用模型。不展示虚构百分比。
- 依[W3C不只使用颜色传意](https://www.w3.org/WAI/WCAG22/Understanding/use-of-color.html)，三稿以文字大标题、不同图形、色带和一句区别说明共同区分“讲过程 / 说判断 / 代入读者场景”。编号沿用原variant，未选稿清理后不重排。
- 按本人最新要求，正式网页仅个人说明书的三项必填；历史作品可随时补充，没有历史也能从本人当次材料开始。只有明确确认已发且正文匹配的最终稿，才自动进入素材库参考。

真实智谱调用及Chrome页面验收见[验证记录](VALIDATION.md)。采用官方参数不保证每次输出的事实与文风都合格，仍需本人审阅。

## 本轮提示词与界面实践

- 依[OpenAI提示词指导](https://developers.openai.com/api/docs/guides/prompt-engineering)，将任务、输入、约束和输出约定分清：三个改稿预设由后端ID白名单选择，共享事实、人称、立场和不确定性的约束，并允许附加本人意见。口语化侧重常用词与短句；真实侧重克制、已有细节和不补造成效；专业侧重准确表达与证据边界。实际稿件仍须本人审阅。
- 依[GOV.UK输入组件指导](https://design-system.service.gov.uk/components/text-input/)，标签置于输入框上方，提示文字不替代标签；本项目按本人要求显示红色“必填”，并保留文字标识、原生校验和后端校验。页面只要求身份与业务、目标读者、表达偏好三项，保存不清空旧的隐藏资料。
- 依[W3C对比度指导](https://www.w3.org/WAI/WCAG22/Understanding/contrast-minimum.html)，普通文字按至少4.5:1核对，大字至少3:1；本轮使用实底面板与更深的前景色，并实际回读浏览器样式。不以浅色玻璃效果承载关键文字。
- 根据本人最新规则，素材库仅汇集导入原文和本人明确确认已发的最终稿。后续按需参考这些表达，不是自动训练基础模型；未发表稿件及其自动偏好案例不作为后续素材。

本轮实际验证见[验证记录](VALIDATION.md)。以下保留其他官方能力与分发说明。

核对日期：2026-10-29。这里区分官方能力、本项目的使用方式和实际验收；采用官方参数不等于本项目已通过真人试用。

## 本地界面与模型调用

普通使用者通过正式入口 `start.cmd` 打开工作台，先保存本人说明书，素材库可跳过，在首页直接写作并在“我的内容”查稿。当前导航为“工作台 / 素材库 / 个人说明书 / 我的内容”。界面与后端由本项目提供，受限写作上下文交给本人选择的智谱API或Codex CLI处理；下表为其中Codex路线的官方能力与边界。

本项目实际使用：

| 官方能力 | 项目用途 | 边界 |
| --- | --- | --- |
| `codex exec` | 由本地后端发起一次生成或改稿 | 需账号、网络、可用模型和用量，不能把登录成功当生成成功 |
| 官方原生 CLI 0.165.0 或更新版 | 启动前检查版本，旧版返回更新要求；`MOMENTS_CODEX` 可选择已有隔离原生文件 | 本项目最低要求，不等于承诺以后每一版都自动通过；不附带运行时、不改全局配置 |
| `--sandbox read-only` | 收窄模型运行的文件写权限 | 不是“推理离线”的声明；真实稿件由控制器校验后写入工作区 |
| `--ephemeral` | 不为这次模型运行持久化会话 rollout | 不表示服务提供方完全不处理数据，也不删除本地应用自己保存的稿件 |
| `--output-schema` | 要求结构化三稿或完整修订正文 | 项目还会校验数量、字段、长度与目标轮次，再保存回读 |
| `--ignore-user-config` | 不加载用户 `config.toml`，减少非业务工具和设置混入 | 官方明确认证仍使用 `CODEX_HOME`；自定义 provider/代理配置可能不随用户配置加载，需要如实诊断 |
| 明确模型选择 | 默认 `gpt-6.1-sol`，可用 `MOMENTS_MODEL` 临时覆盖 | 只能选择账号实际支持的官方模型；模型覆盖不能代替新版 CLI 要求，旧版不在支持范围 |

依据：[非交互模式](https://learn.chatgpt.com/docs/non-interactive-mode)、[CLI 参数参考](https://learn.chatgpt.com/docs/cli/reference)、[模型说明](https://learn.chatgpt.com/docs/models)。

CLI 安装与升级按 [官方安装页](https://learn.chatgpt.com/docs/cli)及 [Windows 部署说明](https://learn.chatgpt.com/docs/enterprise/windows-deployment)进行，可由使用者主动下载官方安装器并用 `-Release` 固定版本。具体更新命令和本机 `MOMENTS_CODEX` 路径选择见 [INSTALL.md](INSTALL.md)，不由应用代替执行。

本次联调观察：CLI 0.142.2 已登录仍出现当前模型请求失败；使用已核验官方发行资产的 CLI 0.165.0＋`gpt-6.1-sol`后，已完成真实三稿、只改第2稿、手工终稿、保留、刷新和搜索。本轮新视觉仍待最终界面复核；本机流程通过不替代陌生用户安装或真人文风验收，各项完整证据以 [验证记录](VALIDATION.md)为准。

生成时传递本次材料、受限的作者/读者/偏好上下文和最多两篇参考历史；AI改稿还需要原稿和修改意见。导入历史本身不调用模型，但后续生成可能选取历史作参考。文件保存在本地不代表全程离线，具体处理规则依使用者所用的 Codex 服务。

当前适配器不请求模型执行 shell、浏览器、连接器或文件写入。页面显示就绪、机械测试通过和真实生成分别记录；实际结果见 [验证记录](VALIDATION.md)。

## Skill 与插件分发

[官方 Build skills](https://learn.chatgpt.com/docs/build-skills)说明，仓库内 `.agents/skills` 可用于本地发现和项目工作流；面向更广泛安装分发，推荐把 Skill 打包为插件。

[官方 Package your plugin](https://developers.openai.com/plugins/build/plugins)当前推荐 portable 包：根 `plugin.json`、根 `skills/`，可选 MCP 与资产；`.codex-plugin/plugin.json` 仍是兼容回退。repo/personal marketplace 与公开目录分开，安装后可能从缓存副本加载。具备 manifest 不等于安装、相对路径和运行时已验收。

本次实际交付是完整目录/ZIP＋Windows 启动器。仓库 Skill 作为 Codex 中的高级自然语言入口保留，工作台不要求先安装全局 Skill。当前没有生成或安装插件、没有改全局配置、没有公开 marketplace，也不宣传“插件一键安装”。

后续做插件时，需要保留 Skill 所需代码和模板的完整路径，核验缓存安装后的引用、Python/CLI 依赖、权限和新会话行为，再形成安装说明。不要只补一份 manifest 就把这条路线标为完成。

## 文档、开源与 CI

- 参考 [GitHub Open Source Guides](https://opensource.guide/starting-a-project/)，将用途、开始方法、许可、贡献和求助边界写清；发布仍需核对具体文件与历史。
- 参考 [Diátaxis](https://diataxis.fr/start-here/)，区分首次教程、任务操作、参考和解释，不让新用户先读研发流水账。
- GitHub 工作流采用官方 [checkout](https://github.com/actions/checkout) 和 [setup-python](https://github.com/actions/setup-python) 经上游仓库核对的完整提交 SHA 引用。仅用于维护验证，不是本地产品运行依赖。
- 本地 CI 在 Windows 上使用 Python 3.11/3.12/3.13 跑标准库 `--core-only` 验证；云端 CI 使用 Node 24 和锁文件安装开发依赖，执行测试与 Wrangler dry-run。输出到 runner 临时目录，不登录模型、不调用真实付费API、不自动部署。

CI 文件存在不代表某次提交已经验证通过，实际运行以 GitHub Actions 结果为准。工作流使用只读权限、不保留 checkout 凭据，固定完整提交 SHA，参考[GitHub Actions 安全指南](https://docs.github.com/en/actions/reference/security/secure-use)。许可与公开决定见 [RELEASE_DECISION.md](RELEASE_DECISION.md)。

## 独立云端发行

云端版使用 Cloudflare Worker、D1 和 Queues，将模型写作从请求链转为后台任务。保留模型接口 `redirect: manual` 并拒绝非200响应，避免把Authorization跟随到重定向地址；Workers不支持`redirect: error`，见[官方Request文档](https://developers.cloudflare.com/workers/runtime-apis/request/)。D1只初始化本项目表；资源与Secrets由部署者自行创建，不附带维护者生产配置。
