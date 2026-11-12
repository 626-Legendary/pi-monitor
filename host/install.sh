#!/bin/sh
set -eu
if [ "$(id -u)" -ne 0 ]; then
    echo '请使用 sudo sh host/install.sh 安装宿主机 SSH 只读采集服务。' >&2
    exit 1
fi
for command in systemctl loginctl journalctl getent install useradd; do
    command -v "$command" >/dev/null 2>&1 || { echo "缺少命令：$command" >&2; exit 1; }
done
[ -x /usr/bin/python3 ] || { echo '请先安装 python3。' >&2; exit 1; }
getent group systemd-journal >/dev/null || { echo '缺少 systemd-journal 日志读取组。' >&2; exit 1; }
exporter_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if ! getent passwd pi-monitor-ssh >/dev/null; then
    useradd --system --user-group --no-create-home --shell /usr/sbin/nologin pi-monitor-ssh
fi
install -d -m 0755 /usr/local/lib/pi-monitor
install -m 0644 "$exporter_dir/ssh_exporter.py" /usr/local/lib/pi-monitor/ssh_exporter.py
install -m 0644 "$exporter_dir/pi-monitor-ssh.service" /etc/systemd/system/pi-monitor-ssh.service
systemctl daemon-reload
systemctl enable pi-monitor-ssh.service
systemctl restart pi-monitor-ssh.service
echo 'SSH 只读采集服务已安装。用 systemctl status pi-monitor-ssh 查看状态。'
