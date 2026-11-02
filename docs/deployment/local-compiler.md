# 本地 CUDA 编译工具

适合在自己的编辑器中编写 CUDA、检查能否编译，同时不占用显卡的场景。它使用独立 rootless Docker 和 gVisor 内的 NVRTC，不需要网站、数据库、CUDA 驱动或 GPU。公开样例执行另见[本地 GPU 命令](local-gpu.md)，该路径已连接但真实硬件全路径尚未验收；正式性能成绩仍需已验收的 GPU 服务。

## 准备与构建

使用非 root Linux x86_64 环境、当前源码的[锁定 judge 依赖](README.md#development)，以及启用了 CPU、内存、swap、PID 控制器的私有 rootless Docker。内核须支持 [pidfd（Linux 5.3+）](https://docs.python.org/3.11/library/os.html#os.pidfd_open)，供固定镜像中的控制器确认原连接进程退出。限额缺失时见 [CPU 控制器配置](gpu.md#其他主机遇到-cpu-配额缺失时)。需要的两个固定离线归档与 GPU 开发检查共用：[gVisor 下载](gpu.md#提前保存固定镜像减少下载依赖)、[NVRTC 13.1.115 下载](gpu.md#重放内核正确性检查)。下载和本页构建均不访问设备；归档摘要由构建器核对。

下面的输出目录必须不存在，父目录必须已存在。构建器不会覆盖旧构建，也不会操作站点容器。

```sh
.venv/bin/python scripts/build_practice_compiler.py \
  --docker-socket /run/user/$(id -u)/docker.sock \
  --runtime-archive var/gpu-preflight/gvisor.tar.bz2 \
  --nvrtc-archive var/gpu-preflight/nvrtc-13.1.115.tar.xz \
  --output "$PWD/var/local-compiler-build"
```

成功返回 `checked: true` 和不可变的 `image: sha256:…`，表示镜像内正确源码和语法错误两项真实检查通过。镜像保留供后续练习使用；私有输出目录保留 `build.log`、`build-result.json`、固定制品信息和 `check-state`。采用输出中的完整镜像 ID，不使用可变标签作为运行参数。源码契约变化后构建新镜像和新输出目录；旧构建结果不授予新源码执行能力。

编译的固定总预算为 0.5 CPU、512 MiB 内存、零 swap、256 PID，包含监督器、runsc 和 guest；总期限不超过 180 秒。目标架构由调用者显式指定，不自动探测显卡。编译镜像与普通 GPU 任务镜像用途不同，程序会检查完整镜像契约。

## 使用与恢复

下载公开包、编辑 `solution.cu`、执行 `compile` 和解读退出码，见[本地练习指南](../local-practice.md#cuda-compilation)。每个工作流使用一个独立私有状态目录；首次调用创建，后续调用必须继续使用它。同一目录不能并发编译。

正常结束或 Ctrl+C 会停止并检查原任务。进程被强制终止、机器异常或清理未确认时，保留原目录和 Docker 引擎，执行：

```sh
.venv/bin/python scripts/practice.py recover-compiler \
  --state /absolute/path/to/compiler-state \
  --docker-socket /run/user/$(id -u)/docker.sock
```

`recovered` 表示原记录对应的任务已清理，`idle` 表示无活动任务。恢复不需要源码或镜像参数，不重新运行旧任务，也不声明 GPU 就绪。随后可重试原 `compile` 命令。不要删除、复制或修改 `compiler.json`、`locks`、`journal` 来绕过拒绝；目录、原安装记录或 daemon 变化时工具保留现场并报错。

若首次初始化被打断、目录已有其他文件，或标记损坏，工具不自动接管或修补。保留该目录用于检查；只有确认未创建任务的初始化失败，才另外选一个不存在的状态路径。已有活动记录时必须恢复原状态。

构建检查中断时，恢复路径为该构建输出下的 `check-state`；构建日志和结果保留。重新构建使用新的输出目录。

## 重放实际检查

使用刚构建的完整镜像 ID，选择不存在的验收输出目录：

```sh
.venv/bin/python scripts/practice_compiler_acceptance.py \
  --docker-socket /run/user/$(id -u)/docker.sock \
  --compiler-image sha256:REPLACE_WITH_COMPLETE_IMAGE_ID \
  --output "$PWD/test-results/local-compiler-check"
```

检查两道候选的参考实现和六份语义错解、语法错误、未知架构、错误镜像、并发拒绝、任务包拒绝、SIGINT/SIGKILL、原任务恢复及新的编译。它只使用自有公开包夹具和容器，不导入内容、不生成成绩。原始输出、源码摘要和容器清理记录位于输出目录；总 `passed: true` 才表示该轮通过。底层编译器或资源策略的变更还须重放[完整编译与预算检查](gpu.md#container-compiler)。
