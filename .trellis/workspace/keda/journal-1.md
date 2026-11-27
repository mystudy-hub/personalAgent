# Journal - keda (Part 1)

> AI development session journal
> Started: 2026-11-26

---



## Session 1: 清理项目开发流程配置
<!-- trellis-session: v=3 fp=7e2d6d8e43f99773 -->

**Date**: 2026-11-26
**Task**: 清理项目开发流程配置
**Branch**: `main`

### Summary

按用户要求清理项目原开发流程集成章节及环境检查项，更新模型调用章节编号。项目现使用 mini-trellis 保存开发知识。此次仅作用于 D:/pa。
Topics: [docs/01-项目建设规划书.md](../../../docs/01-%E9%A1%B9%E7%9B%AE%E5%BB%BA%E8%AE%BE%E8%A7%84%E5%88%92%E4%B9%A6.md)

### Git Commits

(No associated commits)


## Session 2: 确认 M0 隔离门禁与本地分支交付
<!-- trellis-session: v=3 fp=0c02fe9fc6749778 -->

**Date**: 2026-11-26
**Task**: 确认 M0 隔离门禁与本地分支交付
**Branch**: `main`

### Summary

规划书已更新。Windows 提权后端启动预检未通过；Ubuntu 22.04 WSL2 已能启动，隔离仍待验收。临时 Git 仓库通过分支引用创建与冲突拒绝检查。
Topics: [docs/01-项目建设规划书.md](../../../docs/01-%E9%A1%B9%E7%9B%AE%E5%BB%BA%E8%AE%BE%E8%A7%84%E5%88%92%E4%B9%A6.md)

### Git Commits

(No associated commits)


## Session 3: M0 CSV 验收基线与隔离预检
<!-- trellis-session: v=3 fp=7fe7e0f2aa33765e -->

**Date**: 2026-11-26
**Task**: M0 CSV 验收基线与隔离预检
**Branch**: `main`

### Summary

独立 CSV 样例已验证基线恰好失败一项，临时修复后四项通过。桌面版 Windows 提权后端预配服务不可用，现有 WSL2 挂载宿主 D 盘，均未通过隔离门禁。
Topics: [docs/01-项目建设规划书.md](../../../docs/01-%E9%A1%B9%E7%9B%AE%E5%BB%BA%E8%AE%BE%E8%A7%84%E5%88%92%E4%B9%A6.md)

### Git Commits

(No associated commits)


## Session 4: M0 WSL2 运行时隔离复验
<!-- trellis-session: v=3 fp=1388ef2cf4a14f49 -->

**Date**: 2026-11-26
**Task**: M0 WSL2 运行时隔离复验
**Branch**: `main`

### Summary

Windows 提权沙箱仍无法启动。WSL2 Bubblewrap 原型通过样例读写、目录外假数据和宿主盘不可见、回环隔离；隔离内 CSV 基线预期失败一项，临时修复四项通过。真实 Codex 和审批链仍待验收。
Topics: [docs/01-项目建设规划书.md](../../../docs/01-%E9%A1%B9%E7%9B%AE%E5%BB%BA%E8%AE%BE%E8%A7%84%E5%88%92%E4%B9%A6.md)

### Git Commits

(No associated commits)


## Session 5: M0 进程清理与回执恢复原型复验
<!-- trellis-session: v=3 fp=40cb454feaf5893a -->

**Date**: 2026-11-26
**Task**: M0 进程清理与回执恢复原型复验
**Branch**: `main`

### Summary

WSL2 Bubblewrap 通过取消、超时、启动器被强制终止的合成进程清理检查。SQLite 加 Git 临时原型已复现回执补记。WSL2 Codex 0.147.0 的独立空配置未登录，真实模型与正式 Worker 仍待集成验收。
Topics: [docs/01-项目建设规划书.md](../../../docs/01-%E9%A1%B9%E7%9B%AE%E5%BB%BA%E8%AE%BE%E8%A7%84%E5%88%92%E4%B9%A6.md)

### Git Commits

(No associated commits)


## Session 6: M0 WSL2 Codex API 调用通过
<!-- trellis-session: v=3 fp=74179b6653ef2942 -->

**Date**: 2026-11-26
**Task**: M0 WSL2 Codex API 调用通过
**Branch**: `main`

### Summary

WSL2 Codex 0.172.0 通过现有 API 配置完成只读非交互固定提示，退出码 0、JSON 事件完成且无工具调用。独立 Worker 配置和命令子进程隔离仍待验收。
Topics: [docs/01-项目建设规划书.md](../../../docs/01-%E9%A1%B9%E7%9B%AE%E5%BB%BA%E8%AE%BE%E8%A7%84%E5%88%92%E4%B9%A6.md)

### Git Commits

(No associated commits)


## Session 7: M0 独立 Worker API 与命令隔离复验
<!-- trellis-session: v=3 fp=e5be96e39801360a -->

**Date**: 2026-11-27
**Task**: M0 独立 Worker API 与命令隔离复验
**Branch**: `main`

### Summary

临时独立配置使用现有 API 成功。真实 codex exec 的合成命令完成授权读写，外部假文件不可读且未继承合成凭据变量。命令沙箱脚本另证实宿主外部路径无落盘及回环隔离。
Topics: [docs/01-项目建设规划书.md](../../../docs/01-%E9%A1%B9%E7%9B%AE%E5%BB%BA%E8%AE%BE%E8%A7%84%E5%88%92%E4%B9%A6.md)

### Git Commits

(No associated commits)


## Session 8: M0 远端写入边界与真实 CSV 候选复验
<!-- trellis-session: v=3 fp=223e162b4ce29933 -->

**Date**: 2026-11-27
**Task**: M0 远端写入边界与真实 CSV 候选复验
**Branch**: `main`

### Summary

合成 Git 远端的宿主推送正对照成功，沙箱内可读本地提交但推送失败，远端引用不变。真实 Codex 仅修改样例导入器，独立运行时回归由一项预期失败变为四项全通过。
Topics: [docs/01-项目建设规划书.md](../../../docs/01-%E9%A1%B9%E7%9B%AE%E5%BB%BA%E8%AE%BE%E8%A7%84%E5%88%92%E4%B9%A6.md)

### Git Commits

(No associated commits)


## Session 9: 完成 M0 WSL2 样例集成验收
<!-- trellis-session: v=3 fp=dc8f62a70f330301 -->

**Date**: 2026-11-27
**Task**: 完成 M0 WSL2 样例集成验收
**Branch**: `main`

### Summary

同一 API 适配器通过 Chat、Main、Research 与 Review，真实独立 Codex Worker 修复合成 CSV，运行时四项回归通过。临时独立仓库演练拒绝与批准、分支回读与回执补记。真实 Worker 的取消、超时和启动器被强制终止均确认后代退出。
Topics: [docs/01-项目建设规划书.md](../../../docs/01-%E9%A1%B9%E7%9B%AE%E5%BB%BA%E8%AE%BE%E8%A7%84%E5%88%92%E4%B9%A6.md)

### Git Commits

(No associated commits)


## Session 10: Windows MXC 基础沙箱复验通过
<!-- trellis-session: v=3 fp=2161e4552aad98d0 -->

**Date**: 2026-11-27
**Task**: Windows MXC 基础沙箱复验通过
**Branch**: `main`

### Summary

Windows 11 25H2 构建 26200.9711，CLI 0.170.0 通过官方 MXC 启动检查。桌面版 CLI 在关闭命令网络和最小文件权限下通过授权读写、外部文件阻断及回环阻断。真实 Windows Worker 与运行时测试待验收，WSL2 已通过样例集成。
Topics: [docs/01-项目建设规划书.md](../../../docs/01-%E9%A1%B9%E7%9B%AE%E5%BB%BA%E8%AE%BE%E8%A7%84%E5%88%92%E4%B9%A6.md)

### Git Commits

(No associated commits)


## Session 11: 完成 M1 任务底座与重启验收
<!-- trellis-session: v=3 fp=dab00cffe683c120 -->

**Date**: 2026-11-27
**Task**: 完成 M1 任务底座与重启验收
**Branch**: `main`

### Summary

本机 API 和 SQLite 支持项目注册、幂等任务提交、事件补读及模拟 Worker 调度。五项测试通过，含 12 次并发提交、原子领取、暂停继续、取消、实际服务进程被强制终止后的租约恢复，以及旧版数据库迁移。
Topics: [docs/01-项目建设规划书.md](../../../docs/01-%E9%A1%B9%E7%9B%AE%E5%BB%BA%E8%AE%BE%E8%A7%84%E5%88%92%E4%B9%A6.md)

### Git Commits

(No associated commits)


## Session 12: 完成 M2 Agent 协作与审查验收
<!-- trellis-session: v=3 fp=d1e66b36cea40e27 -->

**Date**: 2026-11-27
**Task**: 完成 M2 Agent 协作与审查验收
**Branch**: `main`

### Summary

Chat/Main 通过本机 Responses 测试服务及真实 WSL API 合成提示验证。固定计划可纠错一次，越权或无效参数阻塞。模拟 Research、Coding、Review 保存产物，校验候选证据，审查退回达到上限阻塞。完整测试 8 项通过。
Topics: [docs/01-项目建设规划书.md](../../../docs/01-%E9%A1%B9%E7%9B%AE%E5%BB%BA%E8%AE%BE%E8%A7%84%E5%88%92%E4%B9%A6.md)

### Git Commits

(No associated commits)


## Session 13: 确认 M3 Windows 控制服务与专用 WSL2 Worker
<!-- trellis-session: v=3 fp=cd5ae78e746718b7 -->

**Date**: 2026-11-27
**Task**: 确认 M3 Windows 控制服务与专用 WSL2 Worker
**Branch**: `main`

### Summary

采纳跨边界执行优先验收，Windows 保管状态、审批及 Git，专用 WSL2 非 root Worker 使用字节流。首版直连 Codex，推迟 OpenHands，对第二种 Coding Agent 的实际需求再评估 ACP 客户端。规划已更新，环境与集成待验收。
Topics: [docs/01-项目建设规划书.md](../../../docs/01-%E9%A1%B9%E7%9B%AE%E5%BB%BA%E8%AE%BE%E8%A7%84%E5%88%92%E4%B9%A6.md)

### Git Commits

(No associated commits)


## Session 14: 修复 M2 模型断流与取消
<!-- trellis-session: v=3 fp=82cb36fdb9f8a43c -->

**Date**: 2026-11-27
**Task**: 修复 M2 模型断流与取消
**Branch**: `main`

### Summary

异步模型客户端施加 15 秒总时限，断流归入模型错误。Main 在取消和停机时先关闭请求，再确认取消或释放尝试。11 项回归测试通过。
Topics: [docs/01-项目建设规划书.md](../../../docs/01-%E9%A1%B9%E7%9B%AE%E5%BB%BA%E8%AE%BE%E8%A7%84%E5%88%92%E4%B9%A6.md)

### Git Commits

(No associated commits)
