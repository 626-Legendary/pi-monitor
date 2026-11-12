# SSH 登录信息采集

在树莓派宿主机（使用 systemd 的 Raspberry Pi OS / Debian）执行：

```sh
sudo sh host/install.sh
docker compose up -d --build
```

安装脚本创建专用系统用户 `pi-monitor-ssh`，复制采集脚本和 systemd 单元，并启用服务。
升级采集脚本后重新运行安装脚本。监控网页的升级仍使用 `docker compose up -d --build`。

- 当前 SSH 会话：每 10 秒读取 systemd-logind，展示用户、来源 IP、终端和登录时间。只显示 SSH 远程登录会话，排除本地登录、桌面会话和正在关闭的会话。纯端口转发连接不一定会创建登录会话。
- 最近成功登录：每分钟查询一次保留在 journal 中的 sshd / sshd-session 日志。范围为最近 30 天、最多扫描最近 2000 条日志、展示最新 20 条成功认证记录。不是失败登录或全部网络连接。
- 日志已清理、SSH 日志未写入 journal 或 SSH 未启用 PAM/logind 时，可能没有记录。页面区分读取失败、数据过期和正常空结果。
- 采集输出是 `/run/pi-monitor-ssh/ssh.json`，位于运行时目录；默认 `/run` 为内存文件系统，不向 SD 卡写入采集快照。原有系统认证日志是否持久化由宿主机决定。
- 容器只读挂载该目录，不挂载宿主机认证日志、系统总线或根目录。导出内容只含登录元数据，不含完整日志、密钥指纹或命令行。
- 服务使用独立非 root 用户，通过 `systemd-journal` 组读取日志，不监听网络端口，也不提供重启/关机或命令执行接口。
- 关闭采集服务后保留运行目录，使容器挂载在服务重启后仍然有效；旧快照超过 45 秒会显示过期提示。

检查服务：

```sh
systemctl status pi-monitor-ssh.service
sudo journalctl -u pi-monitor-ssh.service -n 30 --no-pager
```

没有安装宿主机服务时，监控面板仍可使用，SSH 区域会显示未连接提示。

## 卸载采集服务

在宿主机执行；这些命令只移除 SSH 采集服务，监控面板仍可继续运行：

```sh
sudo systemctl disable --now pi-monitor-ssh.service
sudo rm -f /etc/systemd/system/pi-monitor-ssh.service
sudo rm -f /usr/local/lib/pi-monitor/ssh_exporter.py
sudo rm -f /run/pi-monitor-ssh/ssh.json /run/pi-monitor-ssh/ssh.tmp
sudo systemctl daemon-reload
```

专用系统用户保留，方便重新安装。删除快照后，页面将在下一次采集时显示 SSH 服务未连接。
