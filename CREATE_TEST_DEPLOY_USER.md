# 创建测试环境专用部署用户

本文用于为测试环境创建专用部署用户，并配合 GitHub Actions 自动部署当前项目。

## 配置参数

| 配置项 | 值 |
| --- | --- |
| 部署用户名 | `zhengzebiao_test` |
| 部署目录 | `/opt/test/tsuz-api-main` |
| SSH Key 文件名 | `tsuz_github_zhengzebiao_test` |
| 服务器 IP | `1.14.132.121` |
| GitHub Environment | `test` |

> 以下服务器命令需要通过已有管理员账户执行。生成 SSH Key 的命令需要在本地安全电脑上执行。
>
> 在确认新用户可以登录前，不要关闭当前服务器管理员 SSH 会话。

---

## 一、在服务器上创建专用用户

登录服务器：

```bash
ssh <管理员用户>@1.14.132.121
```

创建用户：

```bash
sudo adduser \
  --disabled-password \
  --gecos "" \
  --shell /bin/bash \
  zhengzebiao_test
```

确认用户已创建：

```bash
id zhengzebiao_test
```

该用户禁用了系统密码，后续只允许通过 SSH Key 登录。

---

## 二、确认服务器已安装 Docker

在服务器执行：

```bash
docker --version
docker compose version
```

如果 Docker 尚未安装，请先按照服务器操作系统官方文档安装 Docker Engine 和 Docker Compose Plugin。

---

## 三、授予用户 Docker 权限

将部署用户加入 `docker` 用户组：

```bash
sudo usermod -aG docker zhengzebiao_test
```

确认用户组：

```bash
id zhengzebiao_test
```

输出中应包含：

```text
docker
```

> `docker` 用户组拥有接近 root 的系统权限，因此该用户的 SSH 私钥必须严格保护。不要把个人日常 SSH 私钥用于 GitHub Actions 部署。

---

## 四、创建部署目录并设置权限

创建测试环境部署目录：

```bash
sudo mkdir -p /opt/test/tsuz-api-main
sudo chown -R zhengzebiao_test:zhengzebiao_test /opt/test/tsuz-api-main
sudo chmod 750 /opt/test/tsuz-api-main
```

建议同时限制父目录权限：

```bash
sudo chmod 755 /opt/test
```

检查目录权限：

```bash
ls -ld /opt/test /opt/test/tsuz-api-main
```

预期类似：

```text
drwxr-xr-x ... root              root              /opt/test
drwxr-x--- ... zhengzebiao_test zhengzebiao_test /opt/test/tsuz-api-main
```

---

## 五、在本地生成专用 SSH Key

以下命令在本地电脑执行，不要在服务器上执行：

```bash
ssh-keygen \
  -t ed25519 \
  -C "github-actions-tsuz-test" \
  -f ~/.ssh/tsuz_github_zhengzebiao_test
```

命令会生成两个文件：

```text
~/.ssh/tsuz_github_zhengzebiao_test       # 私钥，只放入 GitHub Secret
~/.ssh/tsuz_github_zhengzebiao_test.pub   # 公钥，放到服务器
```

由于 GitHub Actions 不能交互输入密钥密码，建议该自动部署专用 Key 不设置 passphrase。该 Key 只能用于测试环境部署，并应限制权限。

查看公钥：

```bash
cat ~/.ssh/tsuz_github_zhengzebiao_test.pub
```

---

## 六、把公钥添加到服务器

在服务器管理员会话中执行：

```bash
sudo install -d \
  -m 700 \
  -o zhengzebiao_test \
  -g zhengzebiao_test \
  /home/zhengzebiao_test/.ssh
```

打开公钥文件：

```bash
sudo nano /home/zhengzebiao_test/.ssh/authorized_keys
```

将本地 `~/.ssh/tsuz_github_zhengzebiao_test.pub` 的完整内容粘贴为一行，例如：

```text
ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA... github-actions-tsuz-api-test
```

设置权限：

```bash
sudo chown zhengzebiao_test:zhengzebiao_test /home/zhengzebiao_test/.ssh/authorized_keys
sudo chmod 600 /home/zhengzebiao_test/.ssh/authorized_keys
```

检查权限：

```bash
sudo ls -ld /home/zhengzebiao_test/.ssh
sudo ls -l /home/zhengzebiao_test/.ssh/authorized_keys
```

预期为：

```text
drwx------ ... zhengzebiao_test zhengzebiao_test /home/zhengzebiao_test/.ssh
-rw------- ... zhengzebiao_test zhengzebiao_test /home/zhengzebiao_test/.ssh/authorized_keys
```

### 可选：限制该 Key 的 SSH 功能

为了禁止 Agent Forwarding、端口转发、X11 Forwarding 和交互式终端，可将 `authorized_keys` 中的公钥行改为：

```text
no-agent-forwarding,no-port-forwarding,no-X11-forwarding,no-pty ssh-ed25519 AAAAC3... github-actions-tsuz-api-test
```

当前 GitHub Actions 使用 SSH、SCP 和远程命令部署，通常不需要上述额外功能。

---

## 七、从本地测试 SSH 登录

在本地电脑另开终端执行：

```bash
ssh \
  -i ~/.ssh/tsuz_github_zhengzebiao_test \
  zhengzebiao_test@1.14.132.121
```

如果 SSH 使用非 22 端口：

```bash
ssh \
  -p <SSH端口> \
  -i ~/.ssh/tsuz_github_zhengzebiao_test \
  zhengzebiao_test@1.14.132.121
```

登录后执行：

```bash
whoami
id
docker version
docker compose version
```

应满足：

- `whoami` 输出 `zhengzebiao_test`；
- `id` 输出中包含 `docker`；
- Docker 命令不需要 `sudo`；
- Docker Compose 命令可以正常运行。

测试部署目录写入权限：

```bash
touch /opt/test/tsuz-api-main/permission-test
rm /opt/test/tsuz-api-main/permission-test
```

测试 Docker 运行权限：

```bash
docker run --rm hello-world
```

如果出现 Docker socket 权限错误，退出 SSH 后重新登录，使新的 `docker` 用户组权限生效。

---

## 八、禁止该用户使用密码登录

`adduser --disabled-password` 已经禁用了密码登录。建议通过 SSH 配置进一步明确限制该用户。

在服务器执行：

```bash
sudo nano /etc/ssh/sshd_config.d/zhengzebiao-test-deploy.conf
```

填写：

```sshconfig
Match User zhengzebiao_test
    PubkeyAuthentication yes
    PasswordAuthentication no
    KbdInteractiveAuthentication no
    PermitTTY no
    AllowTcpForwarding no
    X11Forwarding no
```

检查 SSH 配置：

```bash
sudo sshd -t
```

只有命令无输出且退出码为 0 时，才重新加载 SSH 服务：

```bash
sudo systemctl reload ssh
```

部分发行版使用：

```bash
sudo systemctl reload sshd
```

> 确认新配置生效前，不要关闭当前管理员 SSH 会话。

---

## 九、配置 GitHub `test` Environment

进入 GitHub 仓库：

```text
Settings
  → Environments
  → test
```

### 9.1 配置 `SSH_PRIVATE_KEY`

在本地执行：

```bash
cat ~/.ssh/tsuz_github_zhengzebiao_test
```

复制包含首尾标记的完整内容：

```text
-----BEGIN OPENSSH PRIVATE KEY-----
...
-----END OPENSSH PRIVATE KEY-----
```

将内容保存为 GitHub Environment Secret：

```text
SSH_PRIVATE_KEY
```

注意：

- 这里填写没有 `.pub` 后缀的私钥；
- 不要填写 `.pub` 公钥；
- 私钥不能提交到 Git；
- 不要通过聊天、邮件或普通文档传递私钥；
- 该 Key 只用于 `test` Environment。

### 9.2 配置服务器变量

在 GitHub `test` Environment Variables 中配置：

```text
DEPLOY_HOST=1.14.132.121
DEPLOY_USER=zhengzebiao_test
DEPLOY_PORT=22
DEPLOY_PATH=/opt/test/tsuz-api-main
```

如果服务器 SSH 不是 22 端口，将 `DEPLOY_PORT` 改为实际端口。

### 9.3 配置 `SSH_KNOWN_HOSTS`

在可信网络中获取服务器 host key：

```bash
ssh-keyscan -p 22 1.14.132.121
```

如果使用非 22 端口：

```bash
ssh-keyscan -p <SSH端口> 1.14.132.121
```

将输出保存为 GitHub Environment Secret：

```text
SSH_KNOWN_HOSTS
```

> `ssh-keyscan` 本身不会验证服务器身份。正式配置前，应在服务器上查看 host key 指纹，并通过可信渠道核对。

服务器查看 Ed25519 host key 指纹：

```bash
sudo ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
```

---

## 十、服务器部署目录中的敏感文件权限

GitHub Actions 会向部署目录上传环境配置，其中可能包含数据库密码和 JWT 私钥。部署 Workflow 写入文件后，应确保服务器端权限为：

```bash
chmod 600 /opt/test/tsuz-api-main/.env
chown zhengzebiao_test:zhengzebiao_test /opt/test/tsuz-api-main/.env
```

并确认部署目录权限：

```bash
chmod 750 /opt/test/tsuz-api-main
chown zhengzebiao_test:zhengzebiao_test /opt/test/tsuz-api-main
```

不要执行：

```bash
chmod 644 /opt/test/tsuz-api-main/.env
```

---

## 十一、服务器防火墙建议

部署用户只负责通过 SSH 部署，服务器防火墙至少应保证：

- SSH 端口只对可信来源开放，或使用实际 SSH 端口；
- HTTP/HTTPS 根据业务需要开放；
- PostgreSQL `5432` 不对公网开放；
- Redis `6379` 不对公网开放；
- 应用内部端口不对公网开放。

如果使用 UFW，可先查看当前规则：

```bash
sudo ufw status verbose
```

不要直接复制防火墙命令到生产服务器执行。应先确认当前 SSH 端口和已有规则，避免把管理员自己锁在服务器外。

---

## 十二、最终检查清单

```text
[ ] zhengzebiao_test 用户已创建
[ ] 用户没有系统密码
[ ] GitHub Actions 专用公钥已加入 authorized_keys
[ ] /home/zhengzebiao_test/.ssh 权限为 700
[ ] authorized_keys 权限为 600
[ ] zhengzebiao_test 属于 docker 用户组
[ ] 用户可以不使用 sudo 运行 docker
[ ] 用户可以不使用 sudo 运行 docker compose
[ ] /opt/test/tsuz-api-main 属于 zhengzebiao_test:zhengzebiao_test
[ ] 用户可以写入 /opt/test/tsuz-api-main
[ ] SSH Key 文件为 ~/.ssh/tsuz_github_zhengzebiao_test
[ ] 私钥只保存于本地安全位置和 GitHub Secret
[ ] GitHub test Environment 已配置 SSH_PRIVATE_KEY
[ ] GitHub test Environment 已配置 SSH_KNOWN_HOSTS
[ ] DEPLOY_HOST=1.14.132.121
[ ] DEPLOY_USER=zhengzebiao_test
[ ] DEPLOY_PATH=/opt/test/tsuz-api-main
[ ] DEPLOY_PORT 已配置为实际 SSH 端口
[ ] 服务器防火墙未暴露 PostgreSQL 和 Redis
[ ] 服务器 .env 权限为 600
```
