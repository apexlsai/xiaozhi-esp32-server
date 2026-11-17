# dev 分支整合记录

本次以 `8d46d4ec` 为 dev 起点，保留既有未提交工作，先提交新闻检索、Agent 提示词所有权与20轮会话治理，再以线形提交整合剩余差异。没有修改 main、删除分支、强制推送或部署运行服务。

| 来源 | 整合方式 |
| --- | --- |
| 本地 `feature/ksz/news-retrieval` | `8e668bfd` 快进合入 dev，包含脱敏真实模型记录及已知事实审查限制 |
| 本地 `feature/ksz/ota-default-auto-bind` (`1bbe2055`) | 剩余 OTA 默认值及验收文档压缩为 `b29eace8`；重复的定位、讲解实现沿用 dev 最新版本，冲突处保留新提示词与新闻规则 |
| 其他本地业务功能分支 | 本次整合前已是 dev 的祖先，无需重复合并；`backup/`、`tmp` 和 main 不作为功能来源 |
| `origin/feature/ksz/korean-asr` (`a14c0986`) | 移入韩语独立 ASR 配置说明与变更记录，无运行代码差异 |
| `origin/fix/weather-news-api-compat` (`63f3860f`) | 天气差异移为 `KSZ/patches/weather-api-compat.patch`，测试移至 KSZ；新闻 URL/参数及响应校验已有现行补丁覆盖，不用旧重构覆盖新容错 |
| `origin/KSZ/feat/commercial-showroom` (`fa7111d5`) | 通用设备/场馆/信标/讲解功能已在 dev；剩余部署端口、镜像、数据库初值、nginx/启动脚本、环境示例及 showroom 文案保留在原环境分支，不覆盖普通环境 |

压缩/移植整合不会让 Git 将所有来源提交标记为祖先，来源分支仍保留；该记录区分功能已整合与原始提交拓扑。Showroom 专属环境差异有意隔离。

整合后验证：服务端592项通过、31个 subtests 通过，既有缺少git的1项hello补丁自检排除，ASR自应用补丁测试文件同样未运行；完整13个服务端补丁Docker构建通过。Compose 的默认目标、显式空值、自定义目标3种配置验证通过。Agent沿用77项最终回归及两种基底构建结果，本次整合未修改Agent实现。
