# RFC-009：组织课程与有归属范围的第三方题库

状态：C 本地验收通过；范围见 [C 验收](../acceptance-c.md)。依赖 RFC-006；本次不改 core 历史 schema，也不将技术审核当作公开运营或版权批准。

## 身份与课程

`organizations` 新应用使用稳定组织 slug 和 owner / instructor / learner 三种成员角色。所有组织成员入口登录可见；组织创建者成为 owner。只有 owner 可以发送针对具体账号的角色邀请、撤销成员或调整角色；邀请必须由目标本人接受，不自动加入课程。最后一个 owner 不能通过普通离开/降权操作移除。账号删除会清除其成员关系和个人学习记录；组织内容属于组织，保留不可变版本，贡献者外键置空，不保留私人资料。失去 owner 的组织由可信维护命令明确恢复负责人，不能由任意用户认领。

课程编排仅限本组织 owner/instructor。每次保存生成不可变 `CourseRevision`，每个单元固定已发布的 `ProblemVersion` 和 digest；发布/撤回仅改变可见状态，不重写旧版本。课程默认组织内可见，不另建公开课程正文通道。成员主动加入固定课程修订；自己的页面只查询本人的正式评测，经 `core.result_evidence.verified_evidence` 校验。没有教师逐人查看或聚合学习进度的入口；角色或组织身份不能替代学习者同意。题目下线后停止显示绑定题面/标题和新提交入口，历史本人证据保留，当前题目升级不改旧课程绑定。

## 第三方包

包身份由不可变 `namespace`（组织）及包 slug 构成；导入全名为 `namespace--package--local-id`，三个部分各有长度上限，总长不超过 core 的 80。提交的内容包仍经现有 schema/引用/评测类型校验，不能加入可执行插件或自定义 checker。未知引用拒绝；跨包引用只能来自显式 allowlist，固定已发布对象的稳定 ID、版本（题目）和 digest。所有递归依赖都必须显式列入，不能因校验补齐而隐式引入其他来源。

`PackageRevision` 保存来源 URL、许可声明、署名、原始 payload 与 digest。Web owner/instructor 保存草稿→主动送审；平台独立 `organizations.review_packages` 权限可审核，不能审核本组织的包。审核记录绑定精确 digest，拒绝/批准都有最小日志；编辑产生新的不可变修订。来源声明只记录，不自动抓取远端 URL，不把填写许可或技术批准视为法律授权。

Web 对 core 内容表仍只读。可信管理命令在 owner 连接中发布已批准的精确修订：重验当前组织成员角色、审核者权限和独立性、payload/审核 digest、外部引用当前可见性、命名归属及版本不可变性；然后一个事务创建新 ProblemVersion、更新本包的当前内容并记录归属。任何冲突或失败全事务回滚。初次全名若碰到既有知识/题目/项目/译文绑定则拒绝；历史版本永不更新。同一包升级只撤回本包之前归属而本次缺席的对象，不影响其他包或官方内容。

撤回也由可信管理命令执行，限定本包归属范围，保留版本和学习历史；组织负责人可通过 Web 请求撤回，发布不能覆盖待撤回状态。运维命令是执行公共内容变更的受信边界，不把 DB owner 凭证交给 Web。

## 集成点

需主分支注册 app/urls、导航、源码发布清单/镜像 COPY、账号导出和新表权限。Web 只写组织业务表，core 内容写权限不放宽；worker 不读组织身份或学习表。官方 `core.content.import_package` 必须调用本应用的归属 helper：拒绝输入碰到已归属 ID，全量撤回排除已归属 ID。否则现有全表 `published=False` 会破坏包隔离。该 root 集成点不改变 core schema。

课程和包需要实际权限/状态/事务测试，覆盖撤权、撤回、不可变版本、跨包保留、未知引用、审核摘要失配、个人记录隔离与账号删除。包中的 learner/reference 代码只进入既有版本化沙箱协议；发布校验不运行它，技术审核不替代正确/错误程序的真实沙箱验证。

## 操作与验证边界

入口为 `/organizations/`；创建组织后 owner 可以定向邀请账号，目标本人在该入口接受。组织详情页提供课程计划与题库包创建入口。课程表单预填一个当前可见题目的完整版本绑定示例；内容由编辑者填写，每次提交产生新修订。`/organizations/packages/` 仅列出真正发布的包，`/organizations/review/` 只接受独立审核权限。

初始化审核者只授予单一权限，不设 staff/superuser：

```console
python manage.py organization_reviewer REVIEWER_USERNAME --grant
python manage.py organization_reviewer REVIEWER_USERNAME --revoke
```

审核通过后，可信维护者使用 owner 数据库连接执行：

```console
python manage.py org_package_publish REVISION_ID --digest APPROVED_SHA256
python manage.py org_package_withdraw PACKAGE_UUID --digest CURRENT_SHA256
```

传入摘要必须与 Web 审核页面显示的精确摘要相同。重复发布当前已发布且内容未漂移的修订是幂等操作；更旧版本、成员/审核者撤权、摘要不符、未知依赖或归属冲突都拒绝。Web 连接不能执行这些 owner 写入。无 owner 的组织仅可由维护者执行 `organization_restore_owner ORGANIZATION USERNAME` 恢复；仍存在 owner 时拒绝认领。

题库上传是最多 75 KB 的 UTF-8 JSON。顶层严格包含 `source_url`（HTTPS，无凭据）、`license`、`attribution`、`content`（现有 schema_version/nodes/problems）和 `external`。引用包内节点使用 local-id，由发布器加稳定前缀；引用外部对象必须先把稳定 ID 及 digest（题目另含 revision）列入 external。上传不抓取网络、不执行参考程序、不继承来源以外的许可。课程计划最多 20 KB、1–30 单元；题库身份总长不超过 80，namespace/package 各不超过 24。

所有 JSON/表单文本按普通文本转义。学习者记录只包含本人的加入版本和可核验正式结果；组织角色不会授予其他学习者成绩查询权。用户导出仅返回自己的成员关系、收到的邀请、加入记录和本人贡献的快照；不导出其他成员的私有学习记录。个人账号删除清除这些个人关系，组织课程/题库快照保留为组织资料，贡献者/邀请者/审核者/事件操作者关联置空。课程和题库提交表单要求明确接受这一保留边界；根账户删除说明也应纳入此差异。

新应用共 12 张表，Web 可写工作流表 10 张：`organizations_organization`、`organizations_membership`、`organizations_invitation`、`organizations_course`、`organizations_courserevision`、`organizations_courseunit`、`organizations_courseenrollment`、`organizations_package`、`organizations_packagerevision`、`organizations_organizationevent`。`organizations_managedobject` 与 `organizations_publicationlock` 只允许 Web SELECT，owner 执行写入。worker 不授予任何 organizations 表权限。不要自动对未来新表放宽权限；以迁移 `organizations.0002_publication_lock` 为完整功能入口。

官方导入和第三方发布共用数据库行锁，锁内重验归属并执行原子变更。集成 helper 必须在官方 importer 事务内、首次写入之前调用；官方全量撤回排除 helper 返回的两个集合。这不是事后恢复步骤，不能省略。新迁移只添加组织表和种子锁，不更新 core 的已发布 ProblemVersion。

验证包括真实 PostgreSQL 行锁竞争（两个连接在 `pg_stat_activity` 显示 Lock 等待）、真实 Web 角色 owner 命令拒绝、worker 无读取权、owner 实际发布、作用域撤回、官方全量导入保留第三方内容、HTTP 导出/删除、版本绑定/个人进度及英文页面。进度单元测试使用结构化评测证据来验证筛选逻辑，不将其当作新的真实沙箱执行；第三方题目的参考/错误程序仍应由维护者使用既有真实沙箱流程验证。本次不宣称已获得公共运营许可。
