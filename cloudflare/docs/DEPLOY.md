# 部署云端工作台

所有命令在仓库的 `cloudflare` 目录执行。使用自己的Cloudflare账号、独立资源和智谱密钥；不复用原作者网站、数据库、队列或额度。需要Node.js 24及npm，Cloudflare账号需具备Workers、D1、Queues可用权限。资源是否需要付费请以账号面板和官方当前定价为准。

## 1. 安装并检查

```powershell
npm ci
npm test
npm run deploy:check
npx wrangler login
npx wrangler whoami
```

`deploy:check` 是本地打包检查，不进行远程发布。模板Worker名称为 `moments-agent`，首次使用前可修改 `wrangler.jsonc` 的 `name`，避免覆盖账号中已有同名Worker。模板是严格JSON格式，请保留该格式，让发布守卫能够解析。

## 2. 创建自己的D1和队列

```powershell
npx wrangler d1 create moments-agent
npx wrangler queues create moments-writing
npx wrangler queues create moments-writing-dlq
```

将建库结果中的 `database_id` 填入 `wrangler.jsonc` 的D1绑定，替换全零占位符。若改数据库名，同时更新配置和下文命令；若改队列名，同时更新producer、consumer及dead_letter_queue，DLQ名称必须保留 `-dlq` 后缀。绑定名 `COMMENTS_DB` 和 `SEMANTIC_QUEUE` 是应用内部接口，保持不变。不要将建库输出中的其他账号配置或凭证复制进仓库。

确认待应用的迁移只有本应用的 `0011_moments_workbench.sql` 后初始化新库：

```powershell
npx wrangler d1 migrations list moments-agent --remote
npx wrangler d1 migrations apply moments-agent --remote
```

本应用使用独立新库，不需要原网站的早期迁移。升级已有库时先按[运维指南](OPERATIONS.md)备份并核对待执行迁移，不修改已经应用的SQL文件。

## 3. 配置两个服务端Secret

```powershell
npx wrangler secret put MOMENTS_ZHIPU_API_KEY
npx wrangler secret put MOMENTS_SESSION_SECRET
```

第一项在Wrangler的交互提示中输入自己的智谱通用API Key。第二项输入自己生成、至少32字节随机强度的独立签名Secret，不复用智谱密钥。使用密码管理器生成和保存；Secret仅用于签名访客身份，不作为管理员密码。

首次为尚不存在的Worker设置Secret时，Wrangler可能询问是否创建该Worker，核对名称后继续。此时工作台尚未正式可用。也可以通过Cloudflare控制台为同名Worker设置加密Secret，再执行下一步部署。

自动化可将Secret值通过受控进程stdin交给同一条 `wrangler secret put` 命令，或使用Cloudflare支持的秘密管理通道；不要把值写进命令参数、URL、日志、`wrangler.jsonc`、GitHub仓库或Release。不要提交真实 `.dev.vars`。仓库CI仅验证代码，不需要部署Token或上述Secret。

## 4. 检查并正式部署

```powershell
npm test
npm run deploy:check
npm run deploy
```

`npm run deploy` 在D1 ID仍是占位符时立即停止；不会替你创建资源、设置密钥或应用远程迁移。正式部署使用模板中的独立资源，默认输出自己的 `workers.dev` HTTPS链接。该链接是否可从目标读者的网络访问，需要自行验证。

打开部署结果中的 `/moments/`；访问 `/` 或 `/moments` 会308跳转。若需自定义域名，在确认域名归属和现有路由后按Cloudflare官方流程配置该Worker的Custom Domain；此模板没有绑定原作者域名，不要求Nginx或外部服务器。

## 5. 首次验收

使用两种浏览器或一个普通窗口加一个无痕窗口：分别打开工作台，确认资料、历史、任务和稿件互不可见。保存三项个人说明书，不导入历史，使用一段明确的合成材料完成一次真实生成及单稿润色；每次操作会消耗部署者智谱额度。观察全局任务状态，确认一次点击只提交一个模型任务。

保存手工终稿并刷新回读。未实际发表的测试材料不确认已发；已发参考的状态流转已由隔离SQLite测试覆盖。浏览器清除Cookie不可恢复旧工作区，重要稿件另存。最后检查模型额度、Cloudflare资源用量和错误状态；不要用页面HTTP 200代替上述功能验收。

官方资料：[静态资源绑定](https://developers.cloudflare.com/workers/static-assets/binding/)、[D1迁移](https://developers.cloudflare.com/d1/reference/migrations/)、[Queues配置](https://developers.cloudflare.com/queues/configuration/configure-queues/)、[Secret配置](https://developers.cloudflare.com/workers/configuration/secrets/)。
