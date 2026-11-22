# gongtou（工头）

gongtou 是一个 Claude Code skill，把 Claude Code 和 Codex CLI 组合成一条多代理开发流水线：**Claude 负责拆分任务、验收、合并和发布，Codex 在独立的 Git worktree 中并行实现每个任务。** 每个改动合并前都要经过测试和独立审查，通过后可以自动部署到服务器。

## 特性

- **按工作量分流**：局部小改动由 Claude 直接完成；跨模块功能、重构和需要诊断的缺陷则派给 Codex
- **并行开发**：每张任务卡独占一个分支和 worktree，互不干扰
- **两级审查**：先做单卡审查（提交 + 测试），再做集成审查（冲突解决、跨卡交互）
- **受控部署**：依次做空闲检查、fast-forward 更新、服务器端测试，最后推送到 GitHub
- **凭据隔离**：Codex 执行时无法读取 SSH 私钥、云服务凭据、登录文件和 `.env`

## 安装

环境要求：Linux、Python 3.11+、Git、[Claude Code](https://claude.com/claude-code)、[Codex CLI](https://github.com/openai/codex)（已在 0.171.0 上验证）

```sh
git clone https://github.com/lazitech/gongtou.git
cd gongtou
./install.sh --with-skills
```

安装后会把 `codex-flow-lite` 命令链接到 `~/.local/bin`，并在 `~/.claude/skills` 下注册 `/gongtou`。

## 配置

在项目的 Git checkout 中启动 Claude Code，输入：

```
/gongtou 帮我配置
```

Claude 会依次完成以下步骤：

1. **注册项目**：优先以当前分支的 upstream 为基线，其次用 `origin/HEAD`
2. **确定测试命令**：从 CI 配置和项目清单中识别测试命令（Python 项目还会确定虚拟环境解释器），实际运行一次，记录结果和耗时
3. **选择部署方式**：如需部署，从 `~/.ssh/config` 中选择主机，以只读方式检查服务器上的 checkout，然后确认测试命令、空闲检查和发布分支

配置保存在仓库外（`~/.config/codex-flow-lite/`），不会改动项目工作区。

## 工作原理

```
需求
 ├─ 小改动 ──→ Claude 直接修改并验证
 └─ 大改动
     ▼
 Claude 编写任务卡：目标、负责的文件、依赖、验收标准
     ▼
 每张卡独立执行（可并行）
   新建 worktree 与分支 codex-flow/card/<id>
   Codex 实现 → 自动提交 → 运行项目测试 → Codex 审查
   审查有发现：Claude 逐条核实，把确认的问题交回原会话修复
     ▼
 Claude 在集成分支合并通过的卡，解决冲突，运行整体测试
     ▼
 集成审查：检查冲突解决、跨卡交互以及 Claude 自己的改动
     ▼
 部署（可选）：每个站点依次执行
   检查空闲与工作区 → 推送提交 → fast-forward 更新 → 服务器测试 → 推送 GitHub
```

**任务卡与 worktree**：任务卡是一个 Markdown 文件，写明这张卡的目标、改动范围和验收方式。所有卡都从同一个基线提交检出，各自在独立的 git worktree 中开发，所以多张卡可以同时运行而不会互相覆盖。卡没完成时，worktree 和 Codex 会话都会保留，Claude 补充反馈后可以在原会话中继续。

**Codex 角色**：`sol` 默认使用 medium 推理强度，适合范围明确的功能和修复；`astra` 默认使用 high，用于疑难诊断和跨多个接口的改动。审查由 Sol 在独立会话中完成。

**审查**：卡审查针对具体的提交和测试结果，Claude 会先核实每条发现，再决定是要求修复还是判为误报。集成审查只重点检查合并时新引入的内容；没有通过单卡审查的提交会接受完整审查。

**部署**：部署前要确认服务器分支与基线一致、工作区干净、没有正在运行的批次任务。代码只做 fast-forward 更新；服务器测试通过后，再由服务器把测过的提交推送到 GitHub。任何一步失败都会停下并保留现场。

## 常用指令

| 指令 | 作用 |
|---|---|
| `/gongtou <需求>` | 按上述流程评估并派卡 |
| `帮我配置` | 初始化项目，或修改测试与部署配置 |
| `你自己做` | 由 Claude 直接实现，不派卡 |
| `合并上线` | 合并已通过的卡，执行集成审查和部署 |

## 安全

- Codex 的禁读清单默认包括 SSH 私钥、云服务和 Git 凭据、Claude/Codex 登录文件、浏览器配置和工作区内的 `.env*`，可以在 `~/.config/codex-flow-lite/credentials.toml` 中调整
- 自动执行的测试只允许 CPU、无付费的命令；GPU 任务和付费服务每次都需要用户批准
- 部署过程在每一步失败时都会停止，部署记录保存在本地，便于核对和恢复

## 文档

命令参数、退出码和配置项等完整说明见 [技术参考](docs/reference.md)。

## 许可

MIT
