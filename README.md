# ltt2077 的 GKD 合并订阅

合并指定的三个在线源和上传的 `gkd.json5`，保留原作者署名和来源快照。

## 导入链接

将文件发布到本仓库 `main` 分支后，在 GKD → 订阅 → 添加订阅中填入：

```text
https://raw.githubusercontent.com/ltt2077/gkd-merged/main/dist/gkd.json5
```

备用 CDN（可能有缓存延迟）：

```text
https://cdn.jsdelivr.net/gh/ltt2077/gkd-merged@main/dist/gkd.json5
```

**导入合并订阅后，请停用之前单独添加的这四个订阅**，避免重复执行。建议先在常用应用中验证，再按需开启其他分类。合并测试验证的是数据及引用关系，并不等于对每个 Android 应用进行了实机测试。

## 来源与优先顺序

仅在规则确认等价时，按下列顺序保留首份。各来源的版本号不可相互比较。

| 优先级 | 来源 | 更新方式 |
| --- | --- | --- |
| 1 | 上传文件，ID 667，初始版本 575 | 固定保存在 `sources/local.json5`；由用户替换 |
| 2 | [AIsouler](https://github.com/AIsouler/GKD_subscription) | 每日请求原始 URL；作者已停止维护 |
| 3 | [ganlinte](https://github.com/ganlinte/GKD-subscription) | 每日请求指定的 npmmirror URL |
| 4 | [梦念逍遥](https://github.com/MengNianxiaoyao/gkd-subscription) | 每日请求指定的 npmmirror URL |

完整 URL 及固定命名空间见 `sources/config.json`。上传文件的 `supportUri` 指向 [Lin-arm/GKD_subscription](https://github.com/Lin-arm/GKD_subscription)，本项目不会擅自用该仓库的最新文件覆盖用户上传的版本。

## 去重原则

- 按应用包名合并；每个来源的规则组使用固定数字命名空间，新增或修改规则不会把后面所有组重新编号。
- 去重时忽略名称描述、截图及演示 URL 等说明字段，比较选择器、动作、默认启用状态、分类、界面/版本限制、冷却时间、次数等实际行为。保留所有不确定是否等价的变体。
- 无依赖的规则可以逐条去重；带 `preKeys`、`actionCdKey`、`actionMaximumKey` 的规则组作为整体比较。`scopeKeys` 连接的多个规则组作为一个整体处理，保留引用次序并同步重映射组 key。
- 不重写选择器，不交换选择器数组顺序，不将字符串当 JavaScript 执行。严格 JSON 输出也是合法 JSON5。
- 保留原始默认开关：将各来源分类默认状态落实到组上，合并后的分类默认跟随规则组。用户仍可在 GKD 的分类设置里批量启用/禁用。
- 保留全局应用排除列表。`disableIfAppGroupMatch` 在合并后的整个应用列表上生效，使对应的应用专用规则优先于全局规则。
- 原始快照保留完整元数据；`dist/report.json` 提供保留规则组的来源、去重记录和源状态。规则有差异、依赖关系不同或开关不同，都不视为重复。
- 同一来源的组 key 保持稳定。如果原本优先的来源删除一条规则，而另一来源的等价副本接替，该副本仍使用自身命名空间，GKD 对该组的手动设置可能需要重新检查。

## 每日更新

GitHub Actions 的 `Update GKD subscription` 工作流配置为每天 **北京时间 / 新加坡时间 04:23**（UTC 前一天 20:23）运行；GitHub 的定时任务可能延迟。也可在 Actions 页面点击 `Run workflow`。

首次提交脚本或修改本地补充规则时，`push` 也会触发。流程先运行测试，再拉取源、合并、校验，最后提交 `dist/` 和 `sources/cache/`。

- 单个源下载失败最多尝试三次，然后沿用最近一次有效快照，在报告和运行日志中警告，不会悄悄丢掉该源的规则。
- 没有有效快照、订阅 ID 变化或结构校验失败时拒绝发布。源版本回退或规则数骤减超过 50% 时使用旧快照，需人工检查后更新缓存。
- 只有输出内容变化才增加订阅版本号；仅检查时间变化不会迫使 GKD 重新下载大文件。
- 每天提交检查报告，便于确认自动任务是否仍在运行，并避免长期无提交导致公开仓库的 schedule 被 GitHub 停用。请仍定期查看 Actions 是否失败。
- 流程只使用仓库自带的 `GITHUB_TOKEN`，无需另填 PAT。仓库须允许 Actions 运行及 `contents: write`；分支保护如禁止直接提交，需要另外配置。
- 仓库更新与手机端刷新是两件事：GKD 也需要开启订阅自动更新，或手动下拉刷新。

## 本地运行

需要 Python 3.12 或更高版本：

```sh
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
python scripts/merge.py
```

无网络时可用最近一次快照重建：

```sh
python scripts/merge.py --offline
```

## 来源声明

本项目是个人使用的规则聚合，不代表原作者，也不声称拥有原始规则版权。各来源的规则、说明、快照链接仍归原作者及贡献者；本仓库不对这些来源统一重新授权。

ganlinte 来源的 MIT 许可证原文保存在 `licenses/ganlin-MIT.txt`。AIsouler 的原仓库声明包括“禁止在国内平台传播”“本仓库仅供本人学习使用”，并于 2026 年 2 月停止维护；请保留并遵守来源说明，勿将个人聚合用于商业分发。其他来源的使用条件以各自仓库为准。

格式与引用语义参考：[GKD 订阅格式](https://gkd.li/api/interfaces/rawsubscription)、[规则组与 scopeKeys](https://gkd.li/api/interfaces/rawappgroup)、[分类默认值](https://gkd.li/api/interfaces/rawcategory)、[全局规则](https://gkd.li/api/interfaces/rawglobalgroup)。定时执行行为参考 [GitHub Actions schedule 文档](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows#schedule)。
