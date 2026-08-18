# HTTPS 域名访问故障分层排查手册

本文用于排查以下部署链路中出现的域名无法访问、连接超时、TLS 握手失败和 `502 Bad Gateway`：

```text
公网客户端
  → DNS
  → 云安全组 / 云防火墙 / 宿主机防火墙
  → 宿主机 Nginx :443（TLS 终止）
  → 127.0.0.1:${NGINX_PORT}
  → Docker Nginx :80
  → API 容器 :8000
```

以测试环境为例：

```text
https://test-api.tusz.online:443
  → 宿主机 Nginx
  → 127.0.0.1:18080
  → Docker Nginx :80
  → api:8000
```

端口应以当前部署配置为准。`NGINX_PORT=18080` 时，宿主机 Nginx 的 `proxy_pass` 必须指向 `127.0.0.1:18080`；API 映射端口 `APP_PORT=18000` 与这一跳无关。

---

## 1. 本次问题结论

本次 `https://test-api.tusz.online/health` 无法访问包含两个独立问题。

### 1.1 宿主机 Nginx 上游端口不一致

Docker Nginx 实际发布在宿主机 `18080`，但宿主机 Nginx 原先配置为：

```nginx
proxy_pass http://127.0.0.1:8080;
```

由于宿主机 `8080` 没有服务监听：

```text
curl http://127.0.0.1:8080/health
→ Connection refused
```

宿主机 Nginx 因此返回：

```text
502 Bad Gateway
```

修复为：

```nginx
proxy_pass http://127.0.0.1:18080;
```

然后平滑重载：

```bash
sudo nginx -t && sudo systemctl reload nginx
```

验证结果：

```bash
curl http://127.0.0.1:18080/health
```

能够正常返回，说明 Docker Nginx 和 API 链路正常。

### 1.2 公网 443 设置了来源 IP 白名单

云安全组中的 `443/TCP` 虽然已经开放，但来源被限制为指定 IP。未在白名单中的客户端无法访问，因此请求可能一直停留在：
```text
curl -v https://test-api.tusz.online/health
```

```text
Trying <服务器公网 IP>:443...
```

或者 TCP 已建立，但 TLS ClientHello 后迟迟收不到响应。

处理方式取决于服务用途：

- 公开 API：将 `443/TCP` 来源设置为 `0.0.0.0/0`；需要 IPv6 时另加 `::/0`。
- 受限测试 API：保留白名单，并将访问端当前真实公网出口 IP 以 `/32` 加入规则。

查询访问端真实公网 IPv4：

```bash
curl -4 https://api.ipify.org
printf '\n'
```

如果客户端使用 VPN、Clash、公司代理或其他出口代理，安全组需要放行服务器实际看到的代理出口 IP，而不是客户端局域网 IP或 DNS Fake-IP。

修改云安全组后无需重启 Nginx或 Docker。

---

## 2. 先按症状判断故障层级

| 现象 | 通常所在层级 | 优先检查 |
| --- | --- | --- |
| `Could not resolve host` | DNS | A/AAAA 记录、DNS 缓存、代理 DNS |
| 一直停在 `Trying IP:443` | TCP / 防火墙 | 云安全组、云防火墙、UFW、来源 IP 白名单 |
| `Connection refused` | 端口没有监听 | `ss`、容器状态、端口映射 |
| TCP 已连接，TLS ClientHello 后超时 | TLS 前置网络或安全策略 | 安全组、云防火墙、SNI 拦截、外部抓包 |
| 证书名称或有效期错误 | TLS 证书 | SNI、证书域名、证书有效期、证书链 |
| HTTPS 返回 `502 Bad Gateway` | 反向代理上游 | `proxy_pass` 端口、Docker Nginx、API |
| `127.0.0.1:${NGINX_PORT}` 返回 `502` | Docker Nginx 到 API | Docker 网络、`api:8000`、API 状态 |
| `/health` 返回 `200`，业务接口失败 | 应用层 | API 日志、数据库、Redis、认证配置 |

`502` 表示客户端已经完成 TCP 和 TLS，并到达宿主机 Nginx；此时不应继续优先排查证书或公网 DNS。

---

## 3. 第 1 层：确认 DNS

### 3.1 查询普通 DNS

```bash
dig +short A test-api.tusz.online
dig +short AAAA test-api.tusz.online
```

A 记录应指向当前服务器的真实公网 IPv4。

### 3.2 用公共 DoH 交叉验证

Google DNS：

```bash
curl -sS \
  'https://dns.google/resolve?name=test-api.tusz.online&type=A'
```

Cloudflare DNS：

```bash
curl -sS \
  -H 'accept: application/dns-json' \
  'https://cloudflare-dns.com/dns-query?name=test-api.tusz.online&type=A'
```

如果普通 DNS 与 DoH 结果不一致，应检查本地代理、DNS 劫持和缓存。

### 3.3 识别代理 Fake-IP

`198.18.0.0/15` 是保留的测试网段，常被 Clash、Surge 等代理软件用作 Fake-IP。它通常不是服务器真实公网地址。

如果本机解析得到类似 `198.18.x.x`，可以：

- 临时关闭代理/TUN；
- 为域名设置 `DIRECT`；
- 将域名加入 `fake-ip-filter`；
- 使用 `--resolve` 强制请求真实公网 IP。

示例：

```bash
curl -vk --noproxy '*' \
  --resolve test-api.tusz.online:443:<服务器真实公网IP> \
  --connect-timeout 5 \
  --max-time 10 \
  https://test-api.tusz.online/health
```

`--resolve` 仍会保留正确的 Host 和 TLS SNI，比直接访问 `https://<IP>` 更适合测试证书和虚拟主机。

---

## 4. 第 2 层：确认公网 80/443 和访问控制

### 4.1 云端规则

公网通常只需要开放：

```text
TCP 80
TCP 443
```

不应向公网开放：

```text
APP_PORT（例如 18000）
NGINX_PORT（例如 18080）
5432 PostgreSQL
6379 Redis
```

检查以下所有可能的访问控制层：

1. 云服务器安全组；
2. 云防火墙；
3. 负载均衡/CDN/WAF；
4. 宿主机 UFW、iptables 或 nftables。

### 4.2 IP 白名单

如果 `443` 限制来源 IP：

- 白名单应填写客户端的真实公网出口 IP；
- 单一 IPv4 通常写作 `<公网IP>/32`；
- 家庭宽带、移动网络和代理出口可能变化；
- GitHub Actions Runner 的出口 IP通常也不是固定单一地址。

公网 API 通常开放 `443`，再由应用认证、权限、限流和 WAF 控制访问。测试环境若必须使用 IP 白名单，应维护允许访问的出口 IP列表。

### 4.3 宿主机防火墙

```bash
sudo ufw status verbose
sudo iptables -L INPUT -n -v
sudo nft list ruleset
```

如果 UFW 已启用，并且服务应公开：

```bash
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
```

### 4.4 从服务器外部测试

使用另一台机器或手机移动网络执行：

```bash
curl -vk --noproxy '*' \
  --connect-timeout 5 \
  --max-time 10 \
  https://test-api.tusz.online/health
```

不要只用服务器访问自身公网 IP判断公网是否正常；某些云网络不支持实例访问自身公网 IP的 NAT 回环。

---

## 5. 第 3 层：确认宿主机 Nginx 监听

```bash
sudo ss -lntp | grep -E ':(80|443)\b'
```

对公网服务，正常应包含：

```text
0.0.0.0:80
0.0.0.0:443
```

需要 IPv6 时还应包含：

```text
[::]:80
[::]:443
```

如果只看到 `127.0.0.1:443`，外部网络无法连接该监听地址。

检查服务状态：

```bash
sudo systemctl status nginx --no-pager
```

检查配置语法：

```bash
sudo nginx -t
```

确认运行中的 Nginx 已加载目标虚拟主机：

```bash
sudo nginx -T 2>&1 \
  | grep -n -A30 -B5 'server_name test-api.tusz.online'
```

修改宿主机 Nginx 配置后执行：

```bash
sudo nginx -t && sudo systemctl reload nginx
```

只修改 Nginx 配置时不需要重启 Docker 容器。

---

## 6. 第 4 层：确认 TLS 和证书

### 6.1 在服务器本机强制测试 443

```bash
curl -vk --noproxy '*' \
  --resolve test-api.tusz.online:443:127.0.0.1 \
  --connect-timeout 5 \
  --max-time 10 \
  https://test-api.tusz.online/health
```

该命令同时验证：

- 本机 Nginx 443；
- 域名对应的 SNI 虚拟主机；
- TLS 证书；
- 宿主机 Nginx 的反向代理；
- Docker Nginx 和 API。

如果这条命令返回 `200`，但外部仍无法访问，服务器内部链路已经正常，应回到公网安全组、IP 白名单、云防火墙和客户端出口网络排查。

### 6.2 只测试 TLS

```bash
openssl s_client \
  -connect 127.0.0.1:443 \
  -servername test-api.tusz.online \
  -brief </dev/null
```

正常输出应包括：

```text
CONNECTION ESTABLISHED
Protocol version: TLSv1.2 或 TLSv1.3
Peer certificate: CN = test-api.tusz.online
Verification: OK
```

### 6.3 查看证书

```bash
sudo openssl x509 \
  -in /etc/letsencrypt/live/test-api.tusz.online/fullchain.pem \
  -noout -subject -issuer -dates
```

检查 Certbot：

```bash
sudo certbot certificates
sudo certbot renew --dry-run
systemctl status certbot.timer --no-pager
```

---

## 7. 第 5 层：确认宿主机 Nginx 到 Docker Nginx

### 7.1 核对端口映射

```bash
docker ps --format \
  'table {{.Names}}\t{{.Status}}\t{{.Ports}}'
```

或者对 Docker Nginx 容器执行：

```bash
docker port <DOCKER_NGINX_CONTAINER_NAME>
```

`NGINX_PORT=18080` 时，预期包含：

```text
80/tcp -> 0.0.0.0:18080
```

更安全的绑定方式是只监听宿主机回环地址：

```text
80/tcp -> 127.0.0.1:18080
```

### 7.2 直接测试 Docker Nginx

```bash
curl -v \
  --connect-timeout 5 \
  --max-time 10 \
  http://127.0.0.1:18080/health
```

判断：

- `200`：Docker Nginx 到 API 正常；
- `Connection refused`：端口不匹配、容器未运行或没有发布端口；
- `502`：Docker Nginx 已运行，但无法连接 API。

### 7.3 核对宿主机 `proxy_pass`

Docker Nginx 发布在 `18080` 时，宿主机配置必须是：

```nginx
location / {
    proxy_pass http://127.0.0.1:18080;
    proxy_http_version 1.1;

    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Request-ID $request_id;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

不要把 `APP_PORT=18000` 填入这里。推荐链路通过 Docker Nginx，而不是绕过 Docker Nginx 直接访问 API。

### 7.4 查看宿主机 Nginx 日志

```bash
sudo tail -n 100 /var/log/nginx/error.log
sudo tail -n 100 /var/log/nginx/access.log
```

常见错误：

```text
connect() failed (111: Connection refused) while connecting to upstream
```

表示 `proxy_pass` 指向的端口没有服务监听。

```text
upstream timed out
```

表示上游存在，但没有及时响应。

---

## 8. 第 6 层：确认 Docker Nginx 到 API

### 8.1 查看容器状态

进入部署 runtime 目录，并使用 GitHub Environment 中相同的 Compose project name：

```bash
cd /opt/test/tsuz-api-main/runtime

COMPOSE_PROJECT_NAME=<test环境实际项目名>
compose=(
  docker compose
  -p "$COMPOSE_PROJECT_NAME"
  --env-file .env
  -f docker-compose.deploy.yml
)

"${compose[@]}" ps
"${compose[@]}" logs --tail=100 nginx api
```

不要执行完整的 `docker compose config` 并把输出复制到公共日志，因为解析后的配置可能包含运行时 Secret。

### 8.2 从 Docker Nginx 容器访问 API

```bash
"${compose[@]}" exec -T nginx \
  wget -S -O- http://api:8000/health
```

### 8.3 从 API 容器访问自身

```bash
"${compose[@]}" exec -T api python -c \
  "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5).read().decode())"
```

判断：

- API 自检失败：查看 API 启动日志和应用配置；
- API 自检成功，但 Docker Nginx 访问失败：检查 Docker 网络和 `api` DNS；
- 两者均成功，但宿主机访问 `NGINX_PORT` 失败：检查 Docker Nginx 的端口发布。

### 8.4 检查两个容器的网络

```bash
docker inspect <API_CONTAINER_NAME> \
  --format '{{range $name, $_ := .NetworkSettings.Networks}}{{println $name}}{{end}}'

docker inspect <DOCKER_NGINX_CONTAINER_NAME> \
  --format '{{range $name, $_ := .NetworkSettings.Networks}}{{println $name}}{{end}}'
```

API 和 Docker Nginx 必须加入同一个外部 Docker 网络。

---

## 9. 精确定位公网包是否到达服务器

当本机 HTTPS 返回 `200`，但外部仍然超时时，可以在服务器抓包：

```bash
sudo timeout 30 tcpdump -ni any 'tcp port 443' -vv
```

同时从外部网络发起请求。

判断：

- 看不到数据包：请求被 DNS、云安全组、云防火墙或上游网络拦截；
- 能看到客户端 SYN，但握手不完整：检查安全策略和回程路径；
- 能看到 TLS ClientHello，但没有服务器 TLS 响应：同步检查 Nginx 日志、防火墙和云安全产品；
- 能看到服务器响应包，但客户端收不到：检查云公网回程、运营商或客户端网络。

抓包可能包含客户端 IP和连接元数据，不要将未经处理的完整抓包发布到公共位置。

---

## 10. 修改后的重载或重启方式

### 10.1 只修改宿主机 Nginx

```bash
sudo nginx -t && sudo systemctl reload nginx
```

不需要重启 Docker。

### 10.2 修改 Compose 端口或 Docker Nginx 配置

```bash
cd /opt/test/tsuz-api-main/runtime

COMPOSE_PROJECT_NAME=<环境实际项目名>
docker compose \
  -p "$COMPOSE_PROJECT_NAME" \
  --env-file .env \
  -f docker-compose.deploy.yml \
  up -d --no-build --force-recreate nginx
```

如果 API 配置也发生变化，再同时重新创建：

```bash
docker compose \
  -p "$COMPOSE_PROJECT_NAME" \
  --env-file .env \
  -f docker-compose.deploy.yml \
  up -d --no-build --force-recreate api nginx
```

不要为了排查 HTTPS 执行：

```text
docker compose down -v
```

该命令可能删除 PostgreSQL、Redis 等持久数据卷，与 HTTPS 排查无关。

### 10.3 只修改安全组

无需重启 Nginx、Docker 或服务器，等待规则生效后从外部网络重新测试。

---

## 11. 推荐的固定排查顺序

每次都按从内向外或从底层到上层的固定顺序执行，避免同时修改多个环节：

```text
1. API 容器自身 /health
2. Docker Nginx 容器访问 api:8000/health
3. 宿主机访问 127.0.0.1:${NGINX_PORT}/health
4. 宿主机通过 127.0.0.1:443 + 域名 SNI 访问 HTTPS
5. 确认 Nginx 监听 0.0.0.0:443
6. 确认 DNS 指向真实公网 IP
7. 确认安全组 / 云防火墙 / UFW / IP 白名单
8. 从服务器外部访问公网域名
9. 必要时用 tcpdump 定位数据包边界
```

对应命令速查：

```bash
# 1. API 自检
"${compose[@]}" exec -T api python -c \
  "import urllib.request; print(urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5).read().decode())"

# 2. Docker Nginx → API
"${compose[@]}" exec -T nginx wget -S -O- http://api:8000/health

# 3. 宿主机 → Docker Nginx
curl -v --max-time 10 http://127.0.0.1:18080/health

# 4. 本机完整 HTTPS 链路
curl -vk --noproxy '*' \
  --resolve test-api.tusz.online:443:127.0.0.1 \
  --max-time 10 \
  https://test-api.tusz.online/health

# 5. 宿主机监听
sudo ss -lntp | grep -E ':(80|443)\b'

# 6. DNS
dig +short A test-api.tusz.online

# 7. 防火墙
sudo ufw status verbose

# 8. 外部公网测试
curl -vk --noproxy '*' --max-time 10 \
  https://test-api.tusz.online/health
```

---

## 12. 完成标准

以下检查同时成立，才表示公网 HTTPS 链路完整：

1. API 容器自身 `/health` 返回成功；
2. Docker Nginx 能访问 `api:8000/health`；
3. `http://127.0.0.1:${NGINX_PORT}/health` 返回 `200`；
4. 本机通过 `127.0.0.1:443` 和域名 SNI 访问返回 `200`；
5. Nginx 监听 `0.0.0.0:443`；
6. 公共 DNS 指向当前服务器真实公网 IP；
7. 安全组和防火墙允许当前客户端的真实公网出口 IP访问 `443`；
8. 从服务器外部访问 `https://test-api.tusz.online/health` 返回 `200`。

本次已经验证第 1～5 项链路正常。公网访问是否成功最终取决于 `443` 来源 IP白名单是否包含当前客户端真实公网出口 IP，或者是否按公开 API需求放开为公网访问。
