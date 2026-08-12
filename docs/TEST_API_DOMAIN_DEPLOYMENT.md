# 使用 `test-api.tusz.online` 访问服务器 API

本文说明如何将已经通过 Docker 部署在服务器上的 API 配置为通过以下地址访问：

```text
https://test-api.tusz.online
```

推荐架构：由**宿主机 Nginx** 负责域名、HTTPS 和证书，Docker 内的 Nginx 继续监听宿主机的 `8080` 端口。

```text
客户端
  → https://test-api.tusz.online:443
  → 宿主机 Nginx
  → 127.0.0.1:8080
  → Docker Nginx
  → api:8000
```

## 1. 配置域名解析

在 `tusz.online` 的 DNS 管理后台添加一条记录：

```text
记录类型：A
主机记录：test-api
记录值：服务器公网 IP
TTL：默认
```

等待 DNS 生效后检查：

```bash
dig +short test-api.tusz.online
```

返回值应该是服务器的公网 IP。

也可以使用：

```bash
ping test-api.tusz.online
```

> DNS 解析未生效前，不要申请证书，否则 Certbot 无法完成域名验证。

## 2. 开放服务器端口

在云服务器安全组或防火墙中开放：

```text
TCP 80
TCP 443
```

如果服务器启用了 UFW：

```bash
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
```

不建议向公网开放以下内部服务端口：

```text
8000  API 容器
8080  Docker Nginx
5432  PostgreSQL
6379  Redis
```

## 3. 确认 Docker API 正常运行

在服务器上检查容器：

```bash
docker ps
```

确认 Docker Nginx 的宿主机端口为 `8080`，然后测试健康检查：

```bash
curl --fail -i http://127.0.0.1:8080/health
```

只有该请求正常返回后，再继续配置宿主机 Nginx。

建议在部署环境变量中保持：

```env
NGINX_PORT=8080
```

为了避免 `8080` 暴露到公网，建议将 `docker-compose.deploy.yml` 中 Docker Nginx 的端口绑定为：

```yaml
services:
  nginx:
    ports:
      - "127.0.0.1:${NGINX_PORT:-8080}:80"
```

修改后重新创建相关容器：

```bash
docker compose \
  --env-file .env \
  -f docker-compose.deploy.yml \
  up -d --no-build --force-recreate api nginx
```

再次检查：

```bash
curl --fail -i http://127.0.0.1:8080/health
```

## 4. 安装宿主机 Nginx 和 Certbot

Ubuntu/Debian：

```bash
sudo apt update
sudo apt install -y nginx certbot python3-certbot-nginx
sudo systemctl enable --now nginx
```

确认 Nginx 正常运行：

```bash
sudo systemctl status nginx
```

## 5. 配置 HTTP 反向代理

创建宿主机 Nginx 配置：

```bash
sudo tee /etc/nginx/sites-available/test-api.tusz.online >/dev/null <<'EOF'
server {
    listen 80;
    listen [::]:80;

    server_name test-api.tusz.online;

    client_max_body_size 10m;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_http_version 1.1;

        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Request-ID $request_id;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        proxy_connect_timeout 10s;
        proxy_send_timeout 60s;
        proxy_read_timeout 60s;
    }
}
EOF
```

启用站点：

```bash
sudo ln -sfn \
  /etc/nginx/sites-available/test-api.tusz.online \
  /etc/nginx/sites-enabled/test-api.tusz.online
```

如果默认站点与当前配置冲突，可以取消启用默认站点：

```bash
sudo rm -f /etc/nginx/sites-enabled/default
```

检查配置并重载 Nginx：

```bash
sudo nginx -t
sudo systemctl reload nginx
```

验证 HTTP 请求：

```bash
curl -i http://test-api.tusz.online/health
```

如果返回 API 的健康检查结果，说明域名和反向代理已配置成功。

## 6. 使用 Certbot 申请 HTTPS 证书

执行：

```bash
sudo certbot --nginx -d test-api.tusz.online
```

根据提示完成以下操作：

1. 输入用于接收证书到期通知的邮箱；
2. 同意 Let’s Encrypt 服务条款；
3. 选择将 HTTP 自动重定向到 HTTPS。

Certbot 会自动完成：

- 域名所有权验证；
- 申请 Let’s Encrypt 证书；
- 配置 Nginx 的 HTTPS 监听；
- 配置 HTTP 到 HTTPS 跳转；
- 安装自动续期任务。

也可以使用非交互命令，将邮箱替换成真实邮箱：

```bash
sudo certbot --nginx \
  -d test-api.tusz.online \
  --redirect \
  --agree-tos \
  --no-eff-email \
  -m your-email@example.com
```

> 如果已决定使用 Certbot，就不需要同时安装腾讯云下载的证书，以免出现重复的 HTTPS 配置或证书路径冲突。

## 7. 验证 HTTPS

测试健康检查：

```bash
curl --fail -i https://test-api.tusz.online/health
```

在浏览器中访问：

```text
https://test-api.tusz.online/health
```

如果部署环境启用了 API 文档，也可以访问：

```text
https://test-api.tusz.online/docs
```

检查 HTTP 是否自动跳转到 HTTPS：

```bash
curl -I http://test-api.tusz.online/health
```

预期返回 `301` 或 `308`，并包含类似响应头：

```text
Location: https://test-api.tusz.online/health
```

## 8. 检查证书自动续期

查看 Certbot 定时器：

```bash
systemctl status certbot.timer
```

模拟续期，确认配置正确：

```bash
sudo certbot renew --dry-run
```

查看当前证书：

```bash
sudo certbot certificates
```

Let’s Encrypt 证书通常有效期为 90 天。只要 Certbot 定时器正常，证书会在到期前自动续期。

## 9. 常见问题

### Certbot 域名验证失败

依次检查：

```bash
dig +short test-api.tusz.online
curl -I http://test-api.tusz.online
sudo ss -lntp | grep -E ':80|:443'
```

需要确保：

- 域名解析到当前服务器公网 IP；
- 云服务器安全组已开放 `80` 和 `443`；
- 服务器防火墙没有拦截；
- 宿主机 Nginx 正在监听 `80`；
- `test-api.tusz.online` 的 Nginx 配置已启用。

### 返回 `502 Bad Gateway`

先检查 Docker 服务：

```bash
curl -i http://127.0.0.1:8080/health
docker ps
docker compose --env-file .env -f docker-compose.deploy.yml logs nginx api
```

如果 `127.0.0.1:8080` 无法访问，说明问题在 Docker Nginx、API 容器或容器网络，而不是域名证书。

### Nginx 配置检查失败

执行：

```bash
sudo nginx -t
```

根据输出检查配置文件语法、重复的 `server_name` 或端口冲突。修复后再执行：

```bash
sudo systemctl reload nginx
```

### 查看请求日志

宿主机 Nginx：

```bash
sudo tail -f /var/log/nginx/access.log /var/log/nginx/error.log
```

Docker 服务：

```bash
docker compose --env-file .env -f docker-compose.deploy.yml logs -f nginx api
```

## 完成标准

以下命令都正常后，部署完成：

```bash
dig +short test-api.tusz.online
curl --fail http://127.0.0.1:8080/health
curl -I http://test-api.tusz.online/health
curl --fail -i https://test-api.tusz.online/health
sudo certbot renew --dry-run
```

最终 API 地址：

```text
https://test-api.tusz.online
```
