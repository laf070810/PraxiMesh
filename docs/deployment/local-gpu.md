# 本地 GPU 公开样例：构建、运行与恢复

适用于在自己的编辑器中编写 CUDA、使用独占单卡运行下载包的公开样例，不需要启动网站或数据库。正式成绩仍由服务器重新评测；本地结果不能上传为成绩或就绪凭证。仅需编译检查且不能使用 GPU 时，选择[无设备编译工具](local-compiler.md)。

**当前实现已连接公开 CLI、完整启动检查和原任务恢复，真实 GPU 全路径尚未验收。** 维护者当前要求继续不占 GPU，因此这里只执行了构建、组件和固定 CPU 容器检查；设备准备、初始化、样例运行和设备恢复均须等获得独占设备授权后验证。当前范围见 [G69 验收](../acceptance-g69.md)。

## 准备匹配的镜像

使用独立开发环境的锁定 judge 依赖、非 root Linux x86_64 操作者和配置了 CPU/内存/PID 限额的私有 rootless Docker。运行时、驱动兼容要求及固定 runsc/NVRTC 归档见 [GPU 准备](gpu.md#提前保存固定镜像减少下载依赖)。Docker CLI 归档也必须符合仓库固定版本和 SHA-256；不能用任意同名二进制替换。

以下入口只静态读取并复制本机驱动库，构建控制镜像与任务镜像；不查询设备占用、不映射 GPU、不初始化 CUDA。输出目录必须不存在，父目录须已存在：

```sh
.venv/bin/python scripts/build_local_gpu.py \
  --docker-socket /run/user/$(id -u)/docker.sock \
  --docker-cli-archive /absolute/path/docker-29.9.2.tgz \
  --runtime-archive /absolute/path/gvisor.tar.bz2 \
  --nvrtc-archive /absolute/path/nvrtc-13.1.115.tar.xz \
  --library-dir /usr/lib64 \
  --output /absolute/path/new-local-gpu-build
```

`--library-dir` 指向受信系统库目录；Debian/Ubuntu 通常是 `/usr/lib/x86_64-linux-gnu`。构建会核对固定归档、静态 ELF、驱动版本、源码契约和镜像配置。最后生成私有 `local-gpu.json`，记录两个不可变镜像 ID 和匹配制品。它不是 GPU 已可用的证明；构建失败时不会发布这个文件，诊断保留在输出目录的构建日志中。

后续命令使用同一源码版本与清单。源码或驱动更新后，在新输出目录重建，保留原物理状态，先使用新清单执行 `recover-gpu`。恢复只清理旧记录所指向的原对象，不启动旧镜像；新恢复控制器必须与当前源码匹配。清单及驱动快照只留在本机私有目录，不加入 Git 或源码发布包。

## 首次绑定一张独占卡

以下操作会访问 GPU。先由维护者协调独占设备，再设置完整物理 UUID。不要用数字索引、每题新目录或另一份锁来绕过已有任务。首次目录不得已用于 worker、发布协调器或另一套本地状态。

```sh
gpu_bundle=/absolute/path/new-local-gpu-build/local-gpu.json
gpu_state=/absolute/path/original-local-gpu-state
gpu_socket=/run/user/$(id -u)/docker.sock

.venv/bin/python scripts/practice.py prepare-gpu \
  --gpu GPU-REPLACE_WITH_FULL_PHYSICAL_UUID \
  --gpu-bundle "$gpu_bundle" --state "$gpu_state" --docker-socket "$gpu_socket"

.venv/bin/python scripts/practice.py initialize-gpu \
  --gpu-bundle "$gpu_bundle" --state "$gpu_state" --docker-socket "$gpu_socket"
```

`prepare-gpu` 只允许新目录，检查已授权设备身份与占用，保存原 daemon、目录、锁、主机与管理制品身份；发现外部进程时拒绝操作，不终止它们。`initialize-gpu` 将这份原状态绑定为本地练习，检查固定控制器；返回 `initialized` 仍不表示完整 GPU 启动检查通过。它不会将状态交给网站 worker。

同一物理 GPU 的所有练习必须沿用这一目录，并由同一操作者和 daemon 管理。物理锁与宿主命令互斥都在原状态中。另一条命令忙、状态损坏或身份变化时保留现场，不能删除锁或重建状态来继续。

## 运行公开样例

按[本地练习指南](../local-practice.md)下载公开包，编辑其中的 `solution.cu`，执行：

```sh
.venv/bin/python scripts/practice.py test-gpu ./cuda-work \
  --gpu-bundle "$gpu_bundle" --state "$gpu_state" --docker-socket "$gpu_socket"
```

工具先校验整个下载包，再确认原状态和镜像。完成两次身份核对后才发送源码；源码只经既有隔离任务执行，不在宿主解释或编译。每次运行都经过完整 24 阶段启动检查，标准错误输出固定阶段进度，不能导入旧报告跳过检查。完整启动上限为 72 分钟，公开任务最多 180 秒，外层另留 30 秒退出余量；实际耗时取决于设备和检查结果。

只有协议完成、原附着进程及容器成功退出、原任务已清理、内部操作持久化完成，才输出 JSON。结果包含公开样例反馈、固定版本、环境身份和 `formal_result: false` / `ready: false`，不带正式性能分数。退出码 0 表示公开样例通过，1 表示学员程序未通过，2 表示平台、配置、传输或取消错误。

## 中断后恢复

Ctrl+C/SIGTERM 会让正常控制路径停止原控制器；SIGKILL、终端断开或机器故障可能留下原任务。原始记录和目录必须保留：

```sh
.venv/bin/python scripts/practice.py recover-gpu \
  --gpu-bundle "$gpu_bundle" --state "$gpu_state" --docker-socket "$gpu_socket"
```

恢复不需要下载包或源码，也不要求旧任务镜像仍在本地。先核对并停止旧控制器，再由新的固定控制器清理原内部操作。原 ID、启动身份或状态不匹配时拒绝接管；不会按同名容器认领替代对象。`recovered` / `idle` 均只证明此次恢复的状态，不代表设备就绪，下次运行仍完整重检。

## 不访问 GPU 的开发检查

以下入口用明确的固定 CPU 测试镜像、合成设备元数据、真实 Docker 管道与文件锁验证宿主调用；不会执行 `prepare-gpu` 或 CUDA：

```sh
.venv/bin/python scripts/local_gpu_client_acceptance.py \
  --docker-socket /run/user/$(id -u)/docker.sock \
  --docker-cli-archive /absolute/path/docker-29.9.2.tgz \
  --out test-results/local-gpu-client-new
```

检查成功退出、非零退出、额外输出、取消和 SIGKILL 后原状态恢复，并清理自有容器、测试状态和镜像标签。结果在指定的新目录；这类 CPU 验证不能代替真实 CUDA 正确性、性能、设备恢复、队列或完整 GPU 部署验收。
