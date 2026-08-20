# 全局规范 (Main)

> 系统指令：极简响应，最大化 Token 效率。严禁寒暄、逻辑推演铺垫与冗余总结。直达最终代码或结果。

✏️LANGUAGE

默认使用语言：中文

## 1. AGENT规范结构层级

- Main（全局基准）：本文档，定义通用底座规则。
- Local/Override（项目覆盖）：项目根目录下的规范文件，特定规则优先级高于 Main。

## 2. 代码风格

- 极致纯净：除非显式要求，严格杜绝一切冗余或机械化注释，代码须完全自解释。

## 3. Git 规约

- 身份默认：Name: Jacob​, Email: [jacob_ng@163.com](mailto:jacob_ng@163.com)​。
- 版本 Tag：固定以小写 v​ 开头。修饰词（dev​/alpha​/beta​ 等）前禁止加连字符（如：v1.0.0dev​, v1.2.0alpha1​）。
- 历史纯净：遵循语义化提交规范（如 feat:, fix:），合理压缩（Squash）细碎提交，保持分支历史整洁、线形与规范。
- 分支隔离：优先使用 main​ 分支，严禁直接在 main​ 分支上开发。若检测到当前处于 main​ 分支，须主动告知用户并确认意图。

# 项目规范（Override）

## KSZAI Agent 协作说明

本仓库是 [xiaozhi-esp32-server](https://github.com/xinnan-tech/xiaozhi-esp32-server) 的 KSZ Fork。凡是不同于上游的开发、部署、配置和文档，均以 [`KSZ/`](KSZ/) 为唯一基地；不要将 KSZ 专用内容混入上游通用文件。

在本项目正式脱离上游前，非 KSZ 特有功能、通用重构和上游缺陷由上游团队维护；除非用户明确授权，本 Fork 不主动实现或改写，仅保留 KSZ 集成所需的最小兼容与防护。

- `KSZ/README.md`：KSZ 本地构建、部署、配置和运维使用方法。
- `KSZ/CHANGELOG.md`：基于 Keep a Changelog，按版本记录 KSZ 定制变更（自 `0.1.1` 起不标日期）；新增变更须追加到 `[Unreleased]` 或对应版本节。
- `KSZ/compose.yml`：KSZ 本地构建部署的唯一 Compose 入口。

开发分支从 `dev` 创建 `feature/ksz/<name>` 或 `dev/<topic>`；不得直接在 `main` 开发。通用修复须与 KSZ 定制分离，确保后续可同步 `upstream/main`。
