# 运行、故障和回滚

## 身份与数据

工作台公开访问，模型用量计入部署者账号。每位访客用签名Cookie识别，Cookie为 `Secure + HttpOnly + SameSite=Strict`，范围 `/moments`，有效期30天并随访问续期。资料、稿件、修订、素材和任务均按访客隔离，写操作校验同源Origin和CSRF。**没有账号、跨设备同步或Cookie丢失后的恢复入口**；清理Cookie、换浏览器、过期或轮换会话Secret会使原身份不可继续使用，数据库原记录仍保留。

云端D1是正文唯一当前事实源，修订追加记录证据。仅本人确认已发且当前正文Hash匹配的最终稿作为后续参考；导入历史保持独立来源、不推断发表状态。不会操作微信，也不会训练基础模型。资料会按本次生成需要传给智谱，禁止上传凭证和未获授权的私有材料。

这是独立Cloudflare Worker，`CF-Connecting-IP` 来自Cloudflare边缘；不要部署一个直接信任外部自报转发头的额外代理。若自行增加反向代理，需要另行正确解决可信IP来源，否则IP限额不可靠。

## 限额与成本

按北京时间日期计数，生成和润色共用AI次数。三个限制同时生效，配置在 `wrangler.jsonc` 的公开vars中，修改后重新部署：

| 变量 | 默认每日次数 |
|---|---|
| `MOMENTS_VISITOR_DAILY_LIMIT` | 每访客5 |
| `MOMENTS_IP_DAILY_LIMIT` | 每IP20 |
| `MOMENTS_DAILY_LIMIT` | 全站100 |

此外固定限制：每IP每日20个新工作区、全站500个新工作区；写操作每访客200/IP500/全站5000次每日；每工作区300份稿件、20篇导入历史、300份手工修订。达到上限保留已有内容，不删除历史换额度。

请求上限不等于金额上限：智谱按实际模型使用规则计费，Cloudflare Worker、D1和Queues另按各自规则与计划计费。上线前检查两个服务的余额、账单提醒、配额与可用地区；从保守限额开始，观察后再调整。当前仅支持智谱 `glm-5.3-flash`，没有自动切换服务或自动重试付费请求。

后台任务先入D1与Queue再返回页面，避免长模型调用占住网页请求。模型请求最多等待90秒；队列重复投递通过原子领取和状态检查防止同一任务重复调用。请求结果不确定时保留错误并由用户决定是否发起新任务，不自动重放。

## 检查和排错

| 现象 | 检查 |
|---|---|
| 页面可开但写作服务不可用 | D1绑定、迁移、两个Secret名称是否齐全；不输出Secret值 |
| 排队后一直没有结果 | Queues producer/consumer是否同名，DLQ后缀是否正确，任务阶段；不要直接重放付费请求 |
| 智谱返回失败 | 自己账号的API权限、余额、模型可用性；错误已脱敏，不记录认证头和原始请求 |
| 身份或CSRF错误 | 从同一个HTTPS域名重新打开工作台，允许Cookie；不要将不同域名的页面与API拼接 |
| 可查看但不能生成或保存 | 当日AI/写操作限额或工作区容量；不要通过清Cookie绕过限制 |
| 重要稿件消失 | 是否换浏览器、无痕窗口、清Cookie、身份过期或换了会话Secret；当前无自动恢复功能 |

使用Cloudflare面板核对错误、Queue积压、D1用量及部署版本。不要把模型输入、资料、正文、Cookie、CSRF、认证头或推理内容写入日志。模板不启用请求日志采集；如自行开启，需要先审查日志内容和访问权限。

本地代码验收用 `npm test` 和 `npm run deploy:check`，测试全为合成内容、不调用真实模型。远程人工验收需另外记录实际版本、URL和真实结果；不要把单元测试当作新部署成功证明。

## 升级、备份与回滚

更新前保存当前Worker版本与配置，导出自己D1的备份到被忽略的 `output/`，备份含访客私人内容，不能上传GitHub或Release：

```powershell
New-Item -ItemType Directory -Force output
npx wrangler deployments list
npx wrangler d1 export moments-agent --remote --output=output/before-upgrade.sql
npx wrangler d1 migrations list moments-agent --remote
```

只在确认待应用迁移与升级范围一致后运行 `migrations apply`；不能改写已应用的迁移。完成测试、dry-run，再用 `npm run deploy` 发布并进行公网回读和两个访客隔离验收。

发生应用回归时可用 `npx wrangler rollback <此前确认的Worker版本ID>` 恢复旧代码，并回读工作台与API。Worker回滚不等于D1回滚，不删除表、不覆盖整个数据库、不清除访客稿件，也不回放失败模型任务。回滚前确认旧版本与当前Schema及绑定兼容；有不兼容迁移时先停写并制定单独恢复方案。

会话Secret轮换会使现有Cookie失效；密钥轮换应在服务商和Cloudflare的Secret通道完成，不修改正文或公开配置。仓库和Release只包含代码及合成资料，不包含生产备份、真实配置和Secret。
