# gongtou 技术参考

Claude 写卡和集成，Sol 或 Astra 在独立的 Git worktree 里并行开发。每张产生提交的卡先在自己的 worktree 接受 Sol 审查和测试。`codex-flow-lite ship` 再审查整组改动，通过后自动将同一个提交送到配置的各站，逐站测试。服务器只需要 Git、Bash、SSH 和项目自己的测试环境。

## 安装

```sh
git clone https://github.com/lazitech/gongtou.git
cd gongtou
./install.sh --with-skills          # 安装到 ~/.local/share/codex-flow-lite 并注册 /gongtou
codex-flow-lite selfcheck           # 可选：本地权限探测
```

目录结构：`codex-flow` 是启动器，`lite/` 是实现，`skills/gongtou/SKILL.md` 是 Claude Code skill，`defaults/credentials.toml` 是默认禁读清单，`examples/` 是卡片和项目配置示例。

## 开始使用

控制端需 Linux、Python 3.11+、Git 和支持命名权限配置的 Codex CLI。此版本在 Codex 0.171.0 上验证。可以直接使用本目录的 `./codex-flow`。安装命令为 `./install.sh [绝对安装前缀]`，默认命令名为 `~/.local/bin/codex-flow-lite`。传 `--with-skills` 安装并注册 `/gongtou`，也可直接输入 `/gongtou` 手动调用。“gongtou”取“工头”之意，Claude 负责分活、验收、合并和上线。升级时，安装器备份本安装目录拥有的旧 skill 链接。待用户同意退役 R6 后，可用 `./install.sh --with-skills --migrate-r6`。迁移会备份设置，移走已识别的旧 codex-enable，并从 SessionStart、Stop 中移除对应的 R6 命令。旧程序和 profiles 保留。

安装 gongtou 后，Claude 在任何 Git 工作树中都按改动规模分流。小改动直接完成，只读问题直接回答。需要独立测试的新功能、跨模块改动、待诊断的缺陷或可并行的工作默认派卡，单个大功能也适用。明确要求用 Codex 或让 Claude 自己做时，以用户选择为准。

首次派卡时 Claude 自动注册项目，只启用审查，并用一行报告配置、凭据规则、状态和卡片目录。基线优先取当前分支的 upstream，其次取 origin/HEAD；没有远端时取本地默认分支。已有项目沿用原配置。首次注册后或用户说“帮我配置”时，Claude 会识别解释器，实际运行本地测试并记录耗时，将验证过的命令写入仓库外的配置。通常只需回答是否要部署到服务器。需要部署时再选择主机，确认批次检查和发布目标。Claude 负责填写并运行 `codex-flow-lite validate-config` 校验，用户无需手工编辑 TOML。手动注册示例：

```sh
# 在本地 clone 中运行。base 选实际跟踪发布分支的 ref。
codex-flow-lite init --base refs/remotes/origin/main
codex-flow-lite status
```

`init` 打印项目配置、凭据清单、状态目录和卡片目录的绝对路径。`init` 与 `status` 输出的 `drafts` 指向状态目录内的 `drafts/`，任务卡和反馈都放在这里。带部署站点的项目可以把示例复制到仓库外，编辑后用 `init --config /absolute/project.toml` 初始化。项目配置默认在 `$XDG_CONFIG_HOME/codex-flow-lite/projects/`，凭据清单是同级的 `../credentials.toml`。状态默认在 `$XDG_STATE_HOME/codex-flow-lite/projects/`。XDG 默认目录分别是 `~/.config` 和 `~/.local/state`。也可用 `CODEX_FLOW_CONFIG_HOME`、`CODEX_FLOW_STATE_HOME` 直接指定两棵目录。项目以 Git 公共目录的绝对路径区分，配置和状态放在 checkout 外。

## 并行写卡与集成

```sh
base=$(git rev-parse refs/remotes/origin/main)
codex-flow-lite task sol /absolute/cards/parser.md --base "$base" &
first=$!
codex-flow-lite task astra /absolute/cards/export.md --base "$base" &
second=$!
wait "$first"
wait "$second"
codex-flow-lite status parser
```

卡片是 Markdown，示例见 `examples/card.md`。可填写 `id` 及 `effort`。省略时采用文件名作为 ID，Sol 和 Astra 都使用 `high`。CLI 的 `-C /path/to/checkout` 放在子命令前，卡片和反馈文件的相对路径以启动目录为准。每张卡独占一个 `codex-flow/card/<id>` 分支，worktree 在项目状态目录的 `worktrees/<id>/`。不同卡可以同时运行。同一卡的并发操作会返回占用错误。

worker 返回 `done` 且有可提交改动时，启动器用本机 Git 身份在卡分支提交，末行写入 `Codex-Card: <id>`。路径清单也用于自动暂存的排除。无改动时保留原提交。返回 `partial` 或 `blocked` 时保留工作文件和会话，使用 `codex-flow-lite resume parser /absolute/feedback.md` 续接。每次执行的事件、stderr 和回执在 `cards/<id>/attempt-N/`；`status <id>` 的命令和退出码来自已完成的 Codex 命令事件，`null` 表示事件未提供退出码。

可在外部项目 TOML 中设 `interpreter = "/absolute/shared-venv/bin/python"`，worker 指令会收到该绝对路径。新 worktree 从提交检出，项目内未跟踪的 venv 不会随卡复制。各卡可复用已有解释器和系统下载缓存。需要不同依赖的任务可单独建环境。

### worktree 初始化

新 worktree 只有已跟踪文件。项目 TOML 的 `[worktree]` 表声明每张新卡需要的未入库文件，路径相对主 checkout：

- `link`：符号链接到主 checkout，适合 venv、工具链、头文件目录这类大而只读的环境。worker 会被告知这些路径是共享的，不得修改。
- `copy`：复制小的本地文件。
- `setup`：一条命令，在卡的 worktree 里、Codex 沙箱内执行，用于重新编译原生扩展等由已跟踪源码生成的产物。不要链接或复制这类产物，否则改了源码的卡会测到旧的二进制。

`validate-config` 会检查每个来源存在、未被 Git 跟踪、且不匹配凭据禁读清单。链接不匹配 `.venv/` 这类仅目录的忽略规则，启动器会把它们写入 `.git/info/exclude`，避免被提交。`setup` 失败或留下未被忽略的文件时，启动器回滚这张新卡（worktree、分支和记录），日志保存在状态目录的 `worktree-setup/`，修正配置后可用同一 ID 重新派卡。setup 结束后出现的忽略文件和链接、复制的路径视为启动器所有，不会阻止卡 worktree 的自动清理。

worker 指令会自动附上 `card_tests` 命令和链接路径；Sol 审查指令附上测试命令，单卡审查还会说明这些命令已在该提交通过。

```sh
git switch -c integrate/feature "$base"
git merge --no-edit codex-flow/card/parser
git merge --no-edit codex-flow/card/export
# 运行项目适用的本地测试，然后从干净的集成分支发布
codex-flow-lite ship --base "$base"
```

Claude 逐卡核对 `status <id>` 的 worker 状态和 `card_review`。外部项目配置可用 `card_tests = ["python -m pytest -q"]` 指定验收命令，使用项目实际所需解释器。未配置时由 Sol 查找并运行适用测试，回执需有可核对的成功执行事件。没有有效测试证据时卡保留现场。通过退出码为 0，有审查发现为 2，worker 的 partial/blocked 分别为 3/4，测试失败为 5，缺少可验证的测试为 6，执行错误为 1。审查依据和测试结果可在该卡记录中分别查看。卡的 HEAD 等于基线时记为 `no_changes`，跳过审查和测试，退出码为 0。已配置的测试失败时立即返回 5，修复并通过测试后再启动 Sol。已有改动的卡在续接时即使没有新增提交，也会重新验收以便复核误报。通过配置测试的卡增加一次 Sol/high 调用和测试时间，不同卡的这些步骤仍可并行。

单卡出现问题时由 Claude 核实误报，按问题 ID 写出证据，再续接原 worker 修改。卡完成后会在独立的 Sol 审查会话复审。通过的卡由 Claude 合并。冲突由 Claude 在集成 checkout 解决、测试并提交。集成 Sol 重点查冲突解决、Claude 自己的提交和跨卡交互，仍可打开完整提交范围。手工提交及缺少有效单卡证据的内容需要完整审查。集成阶段出现发现时，由 Claude 直接修复并提交，用该集成报告路径运行 `codex-flow-lite ship --after-review`，最多修一轮。审查重点可写成项目 TOML 的 `review_focus` 字符串。完成后清理已集成且干净的卡 worktree，检查包含忽略文件。分支、运行记录、未完成的卡及带本地文件的 worktree 均保留。

项目 TOML 的 `timeout_seconds` 控制每次 task、resume 和 Sol review 的最长执行时间，默认 7200 秒。超时会结束本次本地执行进程组，保留当前 worktree 和日志。若 Codex 已发出启动会话事件，可以用原卡续接。

在集成分支运行 `codex-flow-lite clean` 预览已合入 HEAD 的卡。`clean --apply` 清理干净卡。要处理测试缓存，先用 `clean --include-ignored <id>` 查看具体文件，再用 `clean --apply --include-ignored <id>` 删除整个符合条件的卡 worktree，包括列出的忽略文件。

## 修复后的增量复审

用首轮 `needs_changes` 输出的绝对 `report` 路径，在修复后的干净 checkout 中执行 `codex-flow-lite ship --base <原基线> --after-review <原报告路径>`。首审和复审都附上限长的提交和文件目录。复审检查修复范围、旧问题、新问题及受影响的调用方，也可打开完整差异。同一份完整首审报告只能启动一次续接；基线变化、非后代提交、凭据规则或审查配置变化、缺少依据时走完整审查。超时或未分类错误会结束本次命令。若只允许续接，可加 `--require-incremental`。每次审查的报告和事件按独立目录保存，只有通过的最终结论可供相同候选提交发布。

## 本地凭据误读防护

worker 和 Sol 审查都使用 Codex 命名权限配置。系统根目录可读，HOME、工作区和临时目录可写，允许联网及原生网页搜索，审批策略为 `never`。Codex 按个人配置加载执行规则、MCP 和子代理。Git 和 rsync 子进程显式使用当前 HOME 下的 SSH 配置，缺少该文件时使用 `-F /dev/null`，并在已有 SSH agent 时继承其 socket。

首次初始化复制 `defaults/credentials.toml`，之后编辑用户配置中的 `credentials.toml` 即可。默认涉及 SSH 私钥、Codex 和 Claude 登录文件、云服务、浏览器配置、系统钥匙环，以及工作区各层的 `.env*`。卡片工作区和已知的源 checkout 都套用工作区规则。SSH 的 config、公钥和 known_hosts 可读。Zeron 默认只列 `ipc-token`、`session.json`、`agent-accounts/`、`browser/`。清单支持 `{home}`、`{codex_home}` 和 `{workspace}`。

这套防护针对 Codex 本地执行工具的路径访问。MCP、个人执行规则及远程命令使用各自的权限。0.171 的部分精确禁读路径在不存在时会导致启动失败，启动器只对已存在的精确路径设置遮蔽。通配规则仍在本次运行中生效。自定义 SSH 私钥名可以加入清单。外部服务、子进程环境变量、其他文件副本和远端凭据均有自己的访问范围。

首次执行、升级 Codex 或变更规则/执行实现时会自动进行一次快速探测，可用 `selfcheck` 强制重测。它使用临时 HOME、合成凭据和回环假模型服务，走实际 `codex exec` 读取可读对照文件，同时要求禁读夹具返回权限错误。通过记录供后续卡共享。

## 自动部署

主示例采用本机、一台 SSH 服务器和 GitHub。按 `examples/project.toml` 填写一个 `[[sites]]`。`ssh_config` 是本机 SSH 配置的绝对路径，`host` 是其中的目标主机，`repo` 是服务器上的 checkout。将服务器仓库的 `origin` 配为目标 GitHub 仓库，并确保服务器有推送权限。

1. 本地审查通过后，`codex-flow-lite ship` 将基线、候选提交和进度写入 `deployments/<id>.json`。部署前先检查服务器分支、HEAD 是否等于基线、工作树是否干净，并运行项目的 `idle` 命令。有批次在跑或无法确认状态时，命令应返回非零，部署在写入前停止。
2. 如配置了 `pre_deploy`，先在服务器执行备份并复查。本机将候选提交推送到服务器的临时 ref。部署器核对提交并再次检查服务器状态，再以 `--ff-only --no-overwrite-ignore` 更新工作树。原来的提交可作为回退点。
3. 在服务器仓库根目录核对 HEAD 并运行 `tests`。自动测试须声明 `cpu = true, paid = false`。项目的检查、备份和发布命令也应遵守这个约定。GPU 场次和付费请求由用户逐次批准。
4. 测试通过并再次核对 HEAD、工作树和空闲状态后，服务器执行 `git push origin HEAD:refs/heads/main`，用自己的 Git 配置将测过的提交推到 GitHub。部署器最后复核服务器状态并清理临时 ref。

部署器为每次部署建立私有 SSH 控制套接字，远程命令和向服务器推送提交共用连接，结束时关闭。用户已有的 SSH 配置继续提供主机别名和认证信息。

一次单服务器实测中，每步新建 SSH 连接约需 2.8 秒，整次部署耗时 39 秒。启用连接复用后，首次 SSH 调用约 2.8 秒，后续约 0.4 秒，平均每次约 0.9 秒。

需要多站时，可按执行顺序追加一个 `[[sites]]`，后续站用 `source` 和 `source_ref` 从 GitHub 拉取并核对同一提交。使用本地 checkout 时可设 `transport = "local"`，只做本地审查和清理时可设 `sites = []`。

已有 partial clone 可继续使用。建立单分支 checkout 可用 `git clone --filter=blob:none --single-branch --branch main --no-tags <仓库> <目录>`。缺失对象可能触发 Git 按需获取。下次开卡前，可用 `git fetch --no-tags origin refs/heads/main:refs/remotes/origin/main` 刷新本地基线。

任一步失败即停止，保留现场。报告给出失败步骤和本地记录路径。中断时，记录中最后的 `running` 表示结果有待核实，尤其是推送后断线时，重试前应核对服务器和 GitHub 分支的实际 OID。空闲检查反映检查当时的状态。需要让部署与批次启动严格互斥的项目，可让两者共用项目锁。

## 本地复核

```sh
codex-flow-lite --help
codex-flow-lite selfcheck
```

`selfcheck` 使用临时 HOME、合成凭据和回环假模型服务，实际运行 `codex exec` 验证禁读规则生效。单元测试和基准脚本暂未随本仓库发布。

## 许可

MIT，见 `LICENSE`。
