# 核心操作参考

以下JSON由Agent写入工作区内UTF-8请求文件，再运行`python -B scripts/moments.py --config <工作区/config.json> --request-file <文件>`。路径必须来自已核实的工作区。

| 动作 | 请求要点 |
|---|---|
| 健康检查 | `{"action":"doctor"}` |
| 恢复 | `{"action":"resume"}`；指定轮次可加`round_id` |
| 近期日记录 | `{"action":"discover","days":7}`；按需使用`strict_window:true`，不能自动扩扫 |
| 保存选题 | `{"action":"topics","items":[...]}`；字段按实际接口，保存来源定位而非全文 |
| 选题 | `{"action":"select-topic","n":3}` |
| 创建三稿 | `{"action":"create","title":"主题","source_ref":"用户当次提供素材","variants":[{"title":"切口1","angle":"过程","body":"完整正文","tags":["实践"]},...]}`；必须恰好三个不同切口 |
| 选看 | `{"action":"handle","text":"选2"}` |
| 修改 | `{"action":"handle","text":"修改2：完整修订正文"}`；意见由模型先消化，不能直接保存意见 |
| 原样终稿 | `{"action":"record-user-text","n":2,"body":"本人全文","round_id":"明确轮号","published":false}` |
| 保留 | `{"action":"keep-variant","n":2,"keep":true}` |
| 本人确认已发 | `{"action":"handle","text":"已发2"}`；只能在本人明确报告后执行 |
| 已发查询 | `{"action":"query","status":"已发布","page":1,"page_size":10}` |
| 历史 | `{"action":"history","query":"文字"}`；这是Agent自有稿，不是inputs/history普通作品 |

字段不确定时读脚本或合成测试，不猜接口。列表及正文最终回读Markdown。选稿与准备发不更改发表状态。清理仅针对同轮未明确保留的临时候选，先核验备份，保留已发稿、原编号和改稿证据。未知发表时间不填采集时间或确认时间。

收到本人最终文字后，`record-user-text`会记录原字文本、风格案例引用和机械差异；这不是模型训练，不从单个样本推断永久身份或人格。下一次写稿只读相关偏好和1–2个案例。

飞书话题任务的独立终稿与桌面轮次有不同接口。未安装完整桥接时，不把桌面“最新轮”套到飞书旧话题，也不声称机器人能接收消息。
