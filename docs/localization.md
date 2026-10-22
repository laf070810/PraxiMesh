# 中文与英文

界面支持 `zh-hans` 和 `en`，URL 和内容稳定 ID 不因语言变化。页首语言表单使用 POST 和 CSRF；选项保存在 HttpOnly、SameSite=Lax 的 cookie 中，生产环境还启用 Secure。没有 cookie 时采用受支持的浏览器语言，否则回退中文。

界面目录为 `locale/en/LC_MESSAGES/django.po` 和 `djangojs.po`。JS 使用同源 `/jsi18n/`；不放宽 CSP。维护命令：

```sh
python manage.py makemessages -l en --no-location --no-wrap
python manage.py makemessages -l en -d djangojs --no-location --no-wrap
python manage.py compilemessages
python -m pytest tests/test_localization.py
```

需要 GNU gettext 工具。英文目录必须有实际译文；空译文、fuzzy、格式占位符错误或 `.mo` 与 `.po` 不一致均不能发布。构建必须携带 `localization/`、`locale/` 和编译后的 `.mo`，源码制品也要纳入这些目录。

## 可信内容翻译

`content/en.json` 包含原 Python 到数值计算路径的 20 个知识节点与 8 道题。其余内容按对象显示“中文原文”，详情页明确说明该版本没有英文译文；这不表示剩余内容已完成英文翻译。用户的帖子、评论、私有笔记、源代码及历史导出不自动翻译。

```sh
python manage.py migrate
python manage.py content_import content/starter.json --translations content/en.json
# 或仅更新已经存在的源版本对应的译文：
python manage.py translation_import content/en.json
```

这些命令只供可信维护者/数据库 owner 使用。Web 对以下表只有 SELECT 权限，worker 不需要新权限：

- `localization_knowledgesnapshot`
- `localization_knowledgetranslation`
- `localization_problemtranslation`

授权应先检查当前数据库已记录的 localization 迁移，兼容旧代数据库恢复；不能对尚不存在的表直接 GRANT。`content_import --translations` 将源包与译文包导入放在同一事务，译文无效则源包变更一起回滚。

知识快照由稳定 node ID、canonical digest、源数据构成。迁移只保存升级时实际存在的知识内容；导入前后分别保存旧、新快照。不能据此声称恢复了以前从未记录的知识版本。题目继续使用原 ProblemVersion，其 data、digest、历史提交和评测规则不因翻译而改变。

翻译包格式为 `schema_version: 1`、`locale: "en"`、`nodes` 和 `problems` 数组。每个节点条目含 `id`、`source_digest`、正整数 `revision` 和 `fields`；题目还必须含 `source_revision`。翻译 revision 追加，正文与摘要不可覆盖。可用相同 revision/相同正文和 `published: false` 撤回译文；撤回最新译文后回退中文，不自动恢复更早译文。包中缺席的条目不受影响。

节点 fields 必须恰含 title、summary、body、outcomes、aliases。题目 fields 必须恰含 title、summary、statement、constraints、outcomes、hints、changelog、tags、examples。examples 是该明确源版本的**公开样例顺序**对应的名称列表，不接受测试输入、输出或隐藏用例。关系、ID、许可、参考解、starter、函数名、评测配置等均不允许覆盖。译文以纯文本转义显示，翻译仍需人工核对数值约束及语义。

展示先执行现有对象访问检查，再选择精确源版本译文；不存在匹配译文时明确回退。题目下线与社区隐藏等权限不因语言变化。旧提交只使用其绑定版本的译文。阅读记录始终比较原始源 digest。判题结果按稳定 verdict 在 Web 展示层翻译，不重写历史结果或改变 judge 安全策略。

## 浏览器验收

`tests/browser_smoke.py` 和 `browser_community.py` 明确使用 `zh-CN`。`tests/browser_i18n.py` 使用 `en-US`，实际切换语言、注册、保存草稿、记录阅读、提交社区审核请求、撤回/删除并删除自己的合成账号；`BROWSER_JUDGE=1` 时还要求真实英文 AC。语言切换本身不依赖评测服务。

英文脚本沿用 `BROWSER_URL`、`BROWSER_TEST_SPKI` 的约束：自签例外仅允许回环 HTTPS 的明确临时 SPKI，不关闭上下文 TLS 校验或安装系统信任。receipt 只记录步骤、布尔结果和失败行号；不保存凭证、恢复码或私有代码。`scripts/browser_acceptance.py` 在一次性数据库中运行中文 smoke、社区、英文个人流程和只读项目页面；`--judge` 还实际执行三种契约的 CLI 提交及自愿分享。

项目目录与阶段页面提供英文界面，计划和阶段说明目前保留中文并明确标注。阶段题目按其固定版本选择译文，不能借用当前另一版本的译文。`tests/browser_projects_i18n.py` 检查四个阶段及原文回退，只读浏览验收不代表项目评测或完成证据已通过。
