# Docker CLI

固定 Linux x86_64 Docker CLI 29.9.2，仅用于可信容器控制端。原始二进制归档来自 [Docker 官方下载](https://download.docker.com/linux/static/stable/x86_64/docker-29.9.2.tgz)；不入库，构建时先核对整个归档，再只提取 `docker/docker`。版本和 SHA-256 位于 `judge/container_controller.py`，镜像入口再次核对 CLI 字节。

`LICENSE` 和 `NOTICE` 保留自 [docker/cli v29.9.2](https://github.com/docker/cli/tree/v29.9.2)，并包含在控制端镜像中。这些上游授权不决定 PraxiMesh 自身的许可证。
