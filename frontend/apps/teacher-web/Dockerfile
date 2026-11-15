# 正式入口镜像：nginx 静态托管 teacher-web 构建产物。
#
# 本轮改动（SEC-01，2026-10-28）：配置文件改为**模板**而不是直接落到 conf.d。
#
# 原因：CSP 里必须写明 MinIO 的 origin（证据帧与原视频都由预签名 URL 直接加载），
# 而该 origin 随部署变化（默认 http://localhost:9000，容量栈是 127.0.0.1:19000）。
# 写死一个值意味着换个端口部署就要改源码；留空又会让 CSP 里出现空的来源项。
#
# nginx 官方镜像自带 20-envsubst-on-templates.sh + envsubst，启动时把
# /etc/nginx/templates/**.template 渲染到 /etc/nginx/conf.d/（默认值）。
# 本机镜像里已实测确认两者都存在：
#   $ docker run --rm --entrypoint sh <image> -c 'command -v envsubst'
#   /usr/bin/envsubst
# 它只替换在容器环境里**存在**的 ${VAR}（替换清单从 ENVIRON 取），
# nginx 自己的 $host/$uri 不受影响。
#
# 未在本轮改动的 P2 项（SEC-07）：非 root USER、镜像 digest 固定、HEALTHCHECK。
# 它们需要一次真实构建才能验证（容器以非 root 起 nginx 时要处理 pid 与 cache
# 目录权限），而当前 Docker 数据根仍在 C 盘、本任务书要求先阻塞 Docker 构建。
# 留作开放项写进交付报告。
FROM nginx:alpine

COPY apps/teacher-web/dist /usr/share/nginx/html

# 渲染源：.template 后缀是 envsubst 脚本的默认匹配规则。
COPY apps/teacher-web/nginx.conf /etc/nginx/templates/default.conf.template

#
# 兜底默认值：即使 compose 没传该变量，CSP 也不会出现空的来源项
# （空项会被浏览器当作非法来源，导致整条 img-src/connect-src 被忽略）。
# 生产部署请用 docker-compose.yml 的 MACT_CSP_MEDIA_ORIGIN 覆盖。
ENV MACT_CSP_MEDIA_ORIGIN=http://localhost:9000

EXPOSE 80
CMD ["nginx", "-g", "daemon off;"]
